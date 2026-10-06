"""Real sidecar and credential boundaries of the organization resource RPCs."""
import asyncio
import hashlib
import json
import threading
import time
from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace

import pytest

from jiuwenswarm.governance.organization_auth import OrganizationAuthenticator, authenticated_scope, current_principal
from jiuwenswarm.governance.resources import ResourceDefinition
from jiuwenswarm.server.runtime.gateway_adapter.project_resource_adapter import ProjectResourceAdapter
from jiuwenswarm.server.runtime.session import project_store
from jiuwenswarm.server.runtime.session.project_access import ProjectAccessStore


@pytest.fixture
def case(tmp_path, monkeypatch):
    monkeypatch.setattr(project_store, 'get_agent_root_dir', lambda: tmp_path)
    project_store.invalidate_cache()
    workspace = tmp_path / 'SENTINEL_PRIVATE_ABSOLUTE_PATH'
    workspace.mkdir()
    project = project_store.create_project('test', str(workspace))
    store = ProjectAccessStore()
    store.initialize(project.project_id, 'alice')
    store.replace_acl(project.project_id, 'alice', acl={
        'bob': ['read', 'execute'], 'viewer': ['read'], 'admin': ['admin'],
        '测试+user@example.com': ['read', 'execute'],
    }, expected_revision=1)
    actors = ['alice', 'bob', 'viewer', 'admin', '测试+user@example.com']
    tokens = {actor: 'synthetic-test-' + actor + '-credential-' * 4 for actor in actors}
    auth_path = tmp_path / 'organization.json'
    auth_path.write_text(json.dumps({'authority': 'test-org', 'signing_key': 'a1' * 32,
        'credentials': [{'actor_id': actor, 'sha256': hashlib.sha256(token.encode()).hexdigest(),
                         'expires_at': time.time() + 100000} for actor, token in tokens.items()]}))
    auth_path.chmod(0o600)
    auth = OrganizationAuthenticator(auth_path)
    calls = []

    async def after():
        calls.append('drain')

    adapter = ProjectResourceAdapter(store,
        identity_resolver=lambda _: current_principal().identity(),
        target_resolver=auth.resolve_actor, after_mutation=after)
    revision = 0
    for definition, actions in [
        (ResourceDefinition('workspace', 'workspace', str(workspace)), ('read', 'write')),
        (ResourceDefinition('model', 'credential', 'SENTINEL_CREDENTIAL_REFERENCE'), ('use',)),
        (ResourceDefinition('native:read_file', 'tool', 'native:read_file'), ('invoke',)),
    ]:
        revision = store.register_resource(project.project_id, definition, owner_subject_id='alice',
            actions=actions, expected_revision=revision, delegable=True, expires_at=time.time() + 1000)

    @contextmanager
    def actor(name):
        principal = auth.principal({'Authorization': 'Bearer ' + tokens[name]})
        with authenticated_scope(principal):
            yield principal

    def request(method, **extra):
        params = {'project_id': project.project_id}
        if method != 'list':
            raw = store._load()['projects'][project.project_id]
            params.update(resource_id='workspace', target_actor='bob',
                          expected_acl_revision=raw['acl_revision'],
                          expected_resource_revision=raw['resource_access']['revision'])
        if method == 'grant':
            params.update(actions=['read'], expires_at=None)
        params.update(extra)
        return SimpleNamespace(req_method='project.resources.' + method, params=params,
            request_id='request-' + method, channel_id='web', session_id=None, metadata={}, user_id='forged-ignored')

    value = SimpleNamespace(store=store, pid=project.project_id, workspace=workspace, auth=auth,
        auth_path=auth_path, actor=actor, request=request, adapter=adapter, calls=calls)
    yield value
    project_store.invalidate_cache()


def grants(case, rid='workspace'):
    return case.store._load()['projects'][case.pid]['resource_access']['grants'][rid]


@pytest.mark.asyncio
async def test_real_grant_list_replace_revoke_and_safe_projection(case):
    with case.actor('alice'):
        original = await case.adapter.handle(case.request('list'))
        assert original.ok
        original._delivery_guard()
        text = json.dumps(original.payload)
        assert 'SENTINEL' not in text and str(case.workspace) not in text
        assert not any(key in text for key in ['reference', 'scope', 'signing_key', 'subject_id'])
        assert len(original.payload['resources']) == 3
        response = await case.adapter.handle(case.request('grant'))
        assert response.ok and response.payload['mutation']['resource_revision'] == 4
        response._delivery_guard()
        assert grants(case)['bob']['scope'] == str(case.workspace)
        assert grants(case)['bob']['expires_at'] == grants(case)['alice']['expires_at']
        assert grants(case)['bob']['delegable'] is False
        with pytest.raises(PermissionError):
            original._delivery_guard()
        changed = await case.adapter.handle(case.request('grant', actions=['read', 'write']))
        assert changed.ok and case.calls == ['drain', 'drain']
    with case.actor('bob'):
        listing = await case.adapter.handle(case.request('list'))
        assert listing.ok and len(listing.payload['resources']) == 1
        row = listing.payload['resources'][0]
        assert row['actions'] == ['read', 'write'] and row['can_grant'] is False
        assert [item['target_actor'] for item in row['grants']] == ['bob']
    with case.actor('alice'):
        revoked = await case.adapter.handle(case.request('revoke'))
        assert revoked.ok and 'bob' not in grants(case)
        revoked._delivery_guard()
        assert len(case.calls) == 3


@pytest.mark.asyncio
async def test_unicode_email_actor_and_colon_resource_id(case):
    with case.actor('alice'):
        result = await case.adapter.handle(case.request('grant', resource_id='native:read_file',
            target_actor='测试+user@example.com', actions=['invoke']))
        assert result.ok
    with case.actor('测试+user@example.com'):
        result = await case.adapter.handle(case.request('list'))
        assert result.ok and result.payload['resources'][0]['resource_id'] == 'native:read_file'


@pytest.mark.asyncio
@pytest.mark.parametrize('actor', ['admin', 'bob'])
async def test_revocation_cleanup_needs_neither_read_nor_execute(case, actor):
    with case.actor('alice'):
        assert (await case.adapter.handle(case.request('grant'))).ok
    raw = case.store._load()['projects'][case.pid]
    case.store.replace_acl(case.pid, 'alice', acl={'admin': ['admin']}, expected_revision=raw['acl_revision'])
    with case.actor(actor):
        listing = await case.adapter.handle(case.request('list'))
        assert listing.ok
        row = next(item for item in listing.payload['resources'] if item['resource_id'] == 'workspace')
        assert row['can_grant'] is False and row['actions'] == []
        target = next(item for item in row['grants'] if item['target_actor'] == 'bob')
        assert target['state'] == 'unavailable' and target['can_revoke']
        assert (await case.adapter.handle(case.request('revoke'))).ok


@pytest.mark.asyncio
async def test_read_does_not_reveal_other_subject_resources(case):
    with case.actor('viewer'):
        listed = await case.adapter.handle(case.request('list'))
        assert listed.ok and listed.payload['resources'] == []
        grant = await case.adapter.handle(case.request('grant'))
        assert not grant.ok and grant.payload['code'] == 'FORBIDDEN'
    assert not case.calls and 'bob' not in grants(case)


@pytest.mark.asyncio
async def test_revoked_recipient_credential_does_not_block_cleanup(case):
    with case.actor('alice'):
        assert (await case.adapter.handle(case.request('grant'))).ok
    with case.actor('bob') as principal:
        case.auth.revoke(principal)
    with case.actor('alice'):
        assert (await case.adapter.handle(case.request('revoke'))).ok


@pytest.mark.asyncio
@pytest.mark.parametrize('field,value', [
    ('scope', '/private'), ('reference', 'secret'), ('owner', 'alice'),
    ('subject_id', 'bob'), ('authority', 'test-org'), ('delegable', True),
    ('target_actor', '../bob'), ('resource_id', 'path\\secret'),
    ('actions', ['read', 'read']), ('actions', ['execute']), ('actions', [True]),
    ('expires_at', True), ('expires_at', -1), ('expires_at', float('inf')),
    ('expected_acl_revision', True), ('expected_acl_revision', 2.0),
    ('expected_acl_revision', 2**53), ('expected_resource_revision', 2**53-1),
])
async def test_strict_wire_bounds_never_mutate(case, field, value):
    before = case.store.path.read_bytes()
    with case.actor('alice'):
        response = await case.adapter.handle(case.request('grant', **{field: value}))
    assert not response.ok and response.payload['code'] in {'BAD_REQUEST', 'FORBIDDEN'}
    assert case.store.path.read_bytes() == before and not case.calls


@pytest.mark.asyncio
@pytest.mark.parametrize('target', ['foreign', 'wrong_subject', 'missing', 'self'])
async def test_trusted_target_directory_cannot_be_forged(case, target):
    original = case.adapter.target_resolver
    def resolve(identity, actor):
        value = original(identity, actor)
        return {'foreign': replace(value, authority='other'),
                'wrong_subject': replace(value, subject_id='alice'),
                'missing': None, 'self': identity}[target]
    case.adapter.target_resolver = resolve
    with case.actor('alice'):
        response = await case.adapter.handle(case.request('grant'))
        assert not response.ok and response.payload['code'] == 'FORBIDDEN'
    assert 'bob' not in grants(case)


@pytest.mark.asyncio
@pytest.mark.parametrize('callback', [None, lambda: None])
async def test_unproven_exit_callback_denies_before_write_but_read_works(case, callback):
    case.adapter.after_mutation = callback
    with case.actor('alice'):
        assert (await case.adapter.handle(case.request('list'))).ok
        response = await case.adapter.handle(case.request('grant'))
        assert not response.ok and response.payload['code'] == 'FORBIDDEN'
    assert 'bob' not in grants(case)


@pytest.mark.asyncio
async def test_dual_cas_and_concurrent_writers(case):
    with case.actor('alice'):
        a, b = case.request('grant'), case.request('grant')
        responses = await asyncio.gather(case.adapter.handle(a), case.adapter.handle(b))
        assert sum(row.ok for row in responses) == 1
        assert [row.payload['code'] for row in responses if not row.ok] == ['CONFLICT']
        stale = case.request('grant')
        raw = case.store._load()['projects'][case.pid]
        case.store.replace_acl(case.pid, 'alice', acl=raw['acl'], expected_revision=raw['acl_revision'])
        assert (await case.adapter.handle(stale)).payload['code'] == 'CONFLICT'
    assert len(case.calls) == 1


@pytest.mark.asyncio
async def test_final_list_guard_rejects_expiry_credential_and_payload_drift(case, monkeypatch):
    import jiuwenswarm.server.runtime.session.project_resource_delegation as implementation
    with case.actor('alice'):
        result = await case.adapter.handle(case.request('list'))
        assert result.ok
        now = time.time()
        with monkeypatch.context() as clock:
            clock.setattr(implementation.time, 'time', lambda: now + 1500)
            with pytest.raises(PermissionError):
                result._delivery_guard()
        result.payload['resources'].clear()
        with pytest.raises(PermissionError):
            result._delivery_guard()
    # Equal actor is not the original authenticated principal object.
    with case.actor('alice'):
        with pytest.raises(PermissionError):
            result._delivery_guard()


@pytest.mark.asyncio
async def test_final_mutation_guard_denies_regrant_and_credential_revocation(case):
    with case.actor('alice') as principal:
        first = await case.adapter.handle(case.request('grant'))
        revoked = await case.adapter.handle(case.request('revoke'))
        assert first.ok and revoked.ok
        revoked._delivery_guard()
        assert (await case.adapter.handle(case.request('grant'))).ok
        with pytest.raises(PermissionError):
            revoked._delivery_guard()
        listed = await case.adapter.handle(case.request('list'))
        case.auth.revoke(principal)
        with pytest.raises(PermissionError):
            listed._delivery_guard()


@pytest.mark.asyncio
async def test_drain_failure_preserves_committed_facts_and_no_retry(case):
    async def failure():
        case.calls.append('failed-drain')
        raise TimeoutError('SENTINEL_INTERNAL_EXCEPTION')
    case.adapter.after_mutation = failure
    with case.actor('alice'):
        req = case.request('grant')
        response = await case.adapter.handle(req)
        assert not response.ok and response.payload['code'] == 'EXIT_UNCONFIRMED'
        assert response.payload['mutation']['committed'] is True and response.payload['exit_confirmed'] is False
        assert 'SENTINEL' not in json.dumps(response.payload)
        response._delivery_guard()
        assert (await case.adapter.handle(req)).payload['code'] == 'CONFLICT'
    assert grants(case)['bob']['actions'] == ['read'] and case.calls == ['failed-drain']


@pytest.mark.asyncio
async def test_after_drain_authority_change_is_unknown_not_uncommitted(case):
    async def change():
        raw = case.store._load()['projects'][case.pid]
        case.store.replace_acl(case.pid, 'alice', acl=raw['acl'], expected_revision=raw['acl_revision'])
    case.adapter.after_mutation = change
    with case.actor('alice'):
        response = await case.adapter.handle(case.request('grant'))
        assert not response.ok and response.payload['code'] == 'MUTATION_OUTCOME_UNKNOWN'
    assert 'bob' in grants(case)


@pytest.mark.asyncio
async def test_save_then_throw_is_unknown_and_still_revalidates_runtime(case, monkeypatch):
    save = case.store._save
    def save_then_throw(data):
        save(data)
        raise OSError('SENTINEL_PRIVATE_STORAGE_ERROR')
    monkeypatch.setattr(case.store, '_save', save_then_throw)
    with case.actor('alice'):
        response = await case.adapter.handle(case.request('grant'))
        assert not response.ok and response.payload['code'] == 'MUTATION_OUTCOME_UNKNOWN'
        assert 'SENTINEL' not in json.dumps(response.payload)
    assert 'bob' in grants(case) and case.calls == ['drain']


@pytest.mark.asyncio
async def test_reentrant_target_resolution_cannot_adopt_new_acl(case):
    resolve = case.adapter.target_resolver
    def replacement(identity, actor):
        target = resolve(identity, actor)
        raw = case.store._load()['projects'][case.pid]
        case.store.replace_acl(case.pid, 'alice', acl=raw['acl'], expected_revision=raw['acl_revision'])
        return target
    case.adapter.target_resolver = replacement
    with case.actor('alice'):
        response = await case.adapter.handle(case.request('grant'))
        assert not response.ok and response.payload['code'] == 'CONFLICT'
    assert 'bob' not in grants(case)


@pytest.mark.asyncio
async def test_queued_worker_checks_credential_after_original_lock(case):
    held, release = threading.Event(), threading.Event()
    def lock_owner():
        with case.store._locked():
            held.set()
            assert release.wait(5)
    holder = asyncio.create_task(asyncio.to_thread(lock_owner))
    await asyncio.to_thread(held.wait, 5)
    # Prepare the request before taking a read lock in its helper.
    req = SimpleNamespace(req_method='project.resources.list', params={'project_id': case.pid},
        request_id='queued', channel_id='web', session_id=None, metadata={})
    task = None
    try:
        with case.actor('alice') as principal:
            task = asyncio.create_task(case.adapter.handle(req))
            await asyncio.sleep(0.02)
            case.auth.revoke(principal)
            release.set()
            response = await task
            assert not response.ok and response.payload['code'] == 'FORBIDDEN'
    finally:
        release.set()
        await holder
        if task is not None:
            await task


@pytest.mark.asyncio
async def test_caller_cancel_does_not_skip_committed_mutation_exit(case):
    entered, release = asyncio.Event(), asyncio.Event()
    async def after():
        entered.set()
        await release.wait()
        case.calls.append('drained')
    case.adapter.after_mutation = after
    with case.actor('alice'):
        task = asyncio.create_task(case.adapter.handle(case.request('grant')))
        try:
            await asyncio.wait_for(entered.wait(), 3)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done() and 'bob' in grants(case)
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            release.set()
            if not task.done():
                await asyncio.gather(task, return_exceptions=True)
    assert case.calls == ['drained']


@pytest.mark.asyncio
async def test_original_parent_can_cleanup_after_source_expiry_and_acl_loss(case, monkeypatch):
    with case.actor('alice') as principal:
        case.store.grant_resource(case.pid, principal.identity(), 'workspace', subject_id='bob',
            actions=('read',), delegable=True, expected_revision=3)
    with case.actor('bob'):
        response = await case.adapter.handle(case.request('grant', target_actor='viewer'))
        assert response.ok
    raw = case.store._load()['projects'][case.pid]
    case.store.replace_acl(case.pid, 'alice', acl={}, expected_revision=raw['acl_revision'])
    now = time.time()
    with monkeypatch.context() as clock:
        clock.setattr(time, 'time', lambda: now + 1500)
        with case.actor('bob'):
            listed = await case.adapter.handle(case.request('list'))
            assert listed.ok
            row = listed.payload['resources'][0]
            assert row['actions'] == [] and not row['can_grant']
            assert all(item['state'] == 'unavailable' for item in row['grants'])
            assert (await case.adapter.handle(case.request('revoke', target_actor='viewer'))).ok
    assert 'viewer' not in grants(case)


@pytest.mark.asyncio
async def test_replacing_resource_root_does_not_revive_old_child(case):
    with case.actor('alice'):
        assert (await case.adapter.handle(case.request('grant'))).ok
        case.store.register_resource(case.pid, ResourceDefinition('workspace', 'workspace', str(case.workspace)),
            owner_subject_id='alice', actions=('read', 'write'), delegable=True, expected_revision=4)
    with case.actor('bob'):
        listed = await case.adapter.handle(case.request('list'))
        assert listed.ok and listed.payload['resources'][0]['actions'] == []
        assert listed.payload['resources'][0]['grants'][0]['state'] == 'unavailable'


@pytest.mark.asyncio
async def test_expiry_cannot_expand_source_and_missing_fields_deny(case):
    with case.actor('alice'):
        result = await case.adapter.handle(case.request('grant', expires_at=time.time() + 1000000))
        assert not result.ok and result.payload['code'] == 'FORBIDDEN'
        req = case.request('grant')
        del req.params['expires_at']
        assert (await case.adapter.handle(req)).payload['code'] == 'BAD_REQUEST'
    assert 'bob' not in grants(case) and not case.calls


@pytest.mark.asyncio
async def test_response_final_guard_rejects_original_request_retarget(case):
    with case.actor('alice'):
        req = case.request('list')
        result = await case.adapter.handle(req)
        req.params['project_id'] = 'other-project'
        with pytest.raises(PermissionError):
            result._delivery_guard()


@pytest.mark.asyncio
async def test_list_out_of_js_range_fails_closed_without_rounding(case):
    with case.store._locked():
        data = case.store._load()
        data['projects'][case.pid]['resource_access']['revision'] = 2**53
        case.store._save(data)
    with case.actor('alice'):
        result = await case.adapter.handle(case.request('list'))
        assert not result.ok and result.payload['code'] == 'FORBIDDEN'


@pytest.mark.asyncio
async def test_unknown_save_caller_cancel_still_drains_original_runtime(case, monkeypatch):
    """An uncertain committed write owns the same drain obligation as success."""
    save = case.store._save
    entered, release = asyncio.Event(), asyncio.Event()

    def save_then_throw(data):
        save(data)
        raise OSError('synthetic post-save failure')

    async def after():
        entered.set()
        await release.wait()
        case.calls.append('drained')

    monkeypatch.setattr(case.store, '_save', save_then_throw)
    case.adapter.after_mutation = after
    with case.actor('alice'):
        task = asyncio.create_task(case.adapter.handle(case.request('grant')))
        try:
            await asyncio.wait_for(entered.wait(), 3)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done(), 'uncertain-save Runtime drain was cancelled by the caller'
            assert 'bob' in grants(case)
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert case.calls == ['drained']
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_save_then_read_outage_still_runs_runtime_revalidation(case, monkeypatch):
    save, load = case.store._save, case.store._load
    failed = False

    def save_then_throw(data):
        nonlocal failed
        save(data)
        failed = True
        raise OSError('synthetic post-save failure')

    def load_or_fail():
        if failed:
            raise OSError('synthetic read outage')
        return load()

    monkeypatch.setattr(case.store, '_save', save_then_throw)
    monkeypatch.setattr(case.store, '_load', load_or_fail)
    with case.actor('alice'):
        response = await case.adapter.handle(case.request('grant'))
        assert not response.ok and response.payload['code'] == 'MUTATION_OUTCOME_UNKNOWN'
    failed = False
    assert 'bob' in grants(case) and case.calls == ['drain']


@pytest.mark.asyncio
async def test_unavailable_corrupt_actions_remain_safe_and_revocable(case):
    with case.actor('alice'):
        assert (await case.adapter.handle(case.request('grant'))).ok
    with case.store._locked():
        data = case.store._load()
        raw = data['projects'][case.pid]['resource_access']['grants']['workspace']['bob']
        raw['actions'] = ['SENTINEL_PRIVATE_UNKNOWN_ACTION']
        case.store._save(data)
    with case.actor('bob'):
        response = await case.adapter.handle(case.request('list'))
        assert response.ok
        row = response.payload['resources'][0]
        assert row['actions'] == [] and not row['can_grant']
        assert row['grants'] == [{'target_actor': 'bob', 'actions': [],
            'expires_at': raw['expires_at'], 'can_revoke': True, 'state': 'unavailable'}]
        assert 'SENTINEL' not in json.dumps(response.payload)
        response._delivery_guard()
        assert (await case.adapter.handle(case.request('revoke'))).ok
    assert 'bob' not in grants(case)


@pytest.mark.asyncio
async def test_target_directory_revokes_original_principal_under_same_lock(case):
    resolve = case.adapter.target_resolver
    with case.actor('alice') as principal:
        def revoke_during_directory(identity, actor):
            target = resolve(identity, actor)
            case.auth.revoke(principal)
            return target

        case.adapter.target_resolver = revoke_during_directory
        response = await case.adapter.handle(case.request('grant'))
        assert not response.ok and response.payload['code'] == 'FORBIDDEN'
    assert 'bob' not in grants(case) and case.calls == []


@pytest.mark.asyncio
async def test_unknown_save_preserves_cancellation_received_during_worker(case, monkeypatch):
    save = case.store._save
    entered, release = threading.Event(), threading.Event()

    def save_then_wait_and_throw(data):
        save(data)
        entered.set()
        if not release.wait(5):
            raise TimeoutError('test release missing')
        raise OSError('synthetic post-save failure')

    monkeypatch.setattr(case.store, '_save', save_then_wait_and_throw)
    with case.actor('alice'):
        task = asyncio.create_task(case.adapter.handle(case.request('grant')))
        try:
            assert await asyncio.to_thread(entered.wait, 3)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert case.calls == ['drain'] and 'bob' in grants(case)
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
