"""Wire identity, range and revision attacks against the sharing adapter."""
import hashlib
from dataclasses import replace
from types import SimpleNamespace

import pytest

from jiuwenswarm.governance.contracts import TrustedIdentity
from jiuwenswarm.governance.session_sharing import SessionHistoryRange, SessionSharingAuthority, SHARING_ACTIONS
from jiuwenswarm.server.runtime.session import project_store
from jiuwenswarm.server.runtime.session.session_sharing import SessionSharingStore
from jiuwenswarm.server.runtime.gateway_adapter.session_sharing_adapter import SessionSharingAdapter

OWNER = TrustedIdentity('alice', 'alice-human', 'org')
TARGET = TrustedIdentity('bob', 'bob-human', 'org')


def request(method, **params):
    return SimpleNamespace(req_method='session.share.' + method, params=params, request_id='req',
                           channel_id='web', metadata={}, user_id='alice', session_id=params.get('session_id'))


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(project_store, 'get_agent_root_dir', lambda: tmp_path)
    history = SessionHistoryRange('session', hashlib.sha256(b'session\0').hexdigest(), 1, 2, 100, 0, 100)
    source = [SessionSharingAuthority('session', OWNER, 1, SHARING_ACTIONS, history)]
    identity = [OWNER]
    store = SessionSharingStore(lambda _: source[0], clock=lambda: 100)
    store.register_owner('session', OWNER)
    compile_calls = []
    def compile_history(*args):
        compile_calls.append(args)
        return history
    adapter = SessionSharingAdapter(store, identity_resolver=lambda _: identity[0],
                                    target_resolver=lambda _identity, actor: TARGET if actor == 'bob' else None,
                                    compile_history=compile_history)
    return adapter, store, identity, source, compile_calls


def create(**kwargs):
    params = dict(session_id='session', target_actor='bob', actions=['view'], history_scope='current_snapshot', expires_at=200)
    params.update(kwargs)
    return request('create', **params)


@pytest.mark.asyncio
async def test_complete_create_list_update_revoke_and_no_internal_authority_leak(setup):
    adapter, store, identity, _, compiled = setup
    response = await adapter.handle(create())
    assert response.ok
    item = response.payload['share']
    assert item['target_actor'] == 'bob' and item['can_update'] and item['can_revoke']
    assert not {'history', 'dev', 'ino', 'authority', 'subject', 'target', 'grantor'} & item.keys()
    assert compiled == [('session', OWNER, None)]
    identity[0] = TARGET
    listed = await adapter.handle(request('list'))
    assert listed.ok and listed.payload['shares'][0]['share_id'] == item['share_id']
    assert not listed.payload['shares'][0]['can_revoke']
    identity[0] = OWNER
    updated = await adapter.handle(request('update', session_id='session', share_id=item['share_id'],
                                          actions=['view', 'download'], expires_at=180, expected_revision=1))
    assert updated.ok and updated.payload['share']['revision'] == 2
    raw = store.list_for_session('session', OWNER)[0]
    assert raw['history']['end'] == 100 and len(compiled) == 1
    stale = await adapter.handle(request('revoke', session_id='session', share_id=item['share_id'], expected_revision=1))
    assert not stale.ok and stale.payload['code'] == 'CONFLICT'
    revoked = await adapter.handle(request('revoke', session_id='session', share_id=item['share_id'], expected_revision=2))
    assert revoked.ok and revoked.payload['revision'] == 3
    identity[0] = TARGET
    assert (await adapter.handle(request('list'))).payload['shares'] == []


@pytest.mark.asyncio
@pytest.mark.parametrize('field,value', [('actor_id','alice'), ('identity',{'actor_id':'alice'}),
                                        ('authority','org'), ('subject_id','alice-human'),
                                        ('history',{'end':1000}), ('cursor','forged'), ('owner','alice')])
async def test_unknown_wire_fields_rejected_before_compile(setup, field, value):
    adapter, _, _, _, calls = setup
    response = await adapter.handle(create(**{field:value}))
    assert not response.ok and response.payload['code'] == 'BAD_REQUEST'
    assert calls == []


@pytest.mark.asyncio
async def test_no_identity_no_compile_and_no_owner_claim(setup):
    adapter, store, identity, _, calls = setup
    identity[0] = None
    assert (await adapter.handle(create())).payload['code'] == 'FORBIDDEN'
    identity[0] = TARGET
    assert (await adapter.handle(create())).payload['code'] == 'FORBIDDEN'
    assert calls == [] and store.registered_owner('session') == (OWNER, 1)


@pytest.mark.asyncio
@pytest.mark.parametrize('target', [None, replace(TARGET, authority='another'), replace(TARGET, actor_id='mallory')])
async def test_target_directory_is_mandatory_and_authority_bound(setup, target):
    adapter, _, _, _, calls = setup
    adapter.target_resolver = lambda *_: target
    assert (await adapter.handle(create())).payload['code'] == 'FORBIDDEN'
    assert calls == []


@pytest.mark.asyncio
async def test_current_identity_and_source_rechecked_after_compile(setup):
    adapter, store, identity, source, _ = setup
    def expire(*_):
        identity[0] = None
        return source[0].history
    adapter.compile_history = expire
    assert (await adapter.handle(create())).payload['code'] == 'FORBIDDEN'
    assert store.list_for_session('session', OWNER) == []
    identity[0] = OWNER
    def revoke(*_):
        source[0] = replace(source[0], actions=frozenset({'view'}), revision=2)
        return source[0].history
    adapter.compile_history = revoke
    assert (await adapter.handle(create())).payload['code'] == 'FORBIDDEN'


@pytest.mark.asyncio
async def test_session_binding_update_scope_and_revision_validation(setup):
    adapter, _, _, _, calls = setup
    item = (await adapter.handle(create())).payload['share']
    base = dict(session_id='session', share_id=item['share_id'], actions=['view'], expires_at=190, expected_revision=1)
    for invalid in ({'history_scope':'current_snapshot'}, {'expected_revision':True}, {'target_actor':'mallory'}, {'actions':['view','view']}):
        assert (await adapter.handle(request('update', **(base|invalid)))).payload['code'] == 'BAD_REQUEST'
    assert (await adapter.handle(request('update', **(base|{'session_id':'other'})))).payload['code'] == 'FORBIDDEN'
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_context_is_not_shared_between_concurrent_requests(setup):
    import asyncio
    from contextvars import ContextVar
    adapter, _, _, _, _ = setup
    actor = ContextVar('sharing_test_actor', default=None)
    adapter.identity_resolver = lambda _: actor.get()
    async def run(identity):
        token = actor.set(identity)
        try:
            return await adapter.handle(create())
        finally:
            actor.reset(token)
    owner, target = await asyncio.gather(run(OWNER), run(TARGET))
    assert owner.ok and not target.ok and target.payload['code'] == 'FORBIDDEN'


@pytest.mark.asyncio
async def test_redacted_expired_delegated_grant_remains_revocable_by_current_manager(setup):
    adapter, store, identity, source, _ = setup
    parent = store.grant('session', OWNER, TARGET, actions={'view', 'manage'}, history=source[0].history, expires_at=200)
    child = store.grant('session', TARGET, TrustedIdentity('carol', 'carol-human', 'org'), actions={'view'},
                        history=source[0].history, expires_at=120, parent_share_id=parent['share_id'])
    store._clock = lambda: 130
    identity[0] = TARGET
    listed = (await adapter.handle(request('list'))).payload['shares']
    obsolete = next(item for item in listed if item['share_id'] == child['share_id'])
    assert obsolete['can_revoke'] and not obsolete['can_update'] and 'target_actor' not in obsolete
    revoked = await adapter.handle(request('revoke', session_id='session', share_id=child['share_id'], expected_revision=1))
    assert revoked.ok


@pytest.mark.asyncio
async def test_cancelled_rpc_drains_accepted_mutation_without_replaying(setup):
    import asyncio
    import threading
    adapter, store, _, _, _ = setup
    entered, release = threading.Event(), threading.Event()
    original = store.grant
    calls = []
    def slow_grant(*args, **kwargs):
        calls.append(1)
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)
    store.grant = slow_grant
    task = asyncio.create_task(adapter.handle(create()))
    assert await asyncio.to_thread(entered.wait, 5)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert calls == [1] and len(store.list_for_session('session', OWNER)) == 1
