"""Real shared sidecar, project ACL and history preparation integration."""
import copy
import json
from contextlib import contextmanager
from dataclasses import replace

import pytest

from jiuwenswarm.governance.contracts import TrustedIdentity
from jiuwenswarm.governance.session_sharing import SessionSharingConflict, SessionSharingDenied
from jiuwenswarm.server.runtime.session import lifecycle, project_store, session_history
from jiuwenswarm.server.runtime.session import sharing_host
from jiuwenswarm.server.runtime.session.project_access import ProjectAccessStore

ALICE = TrustedIdentity('alice', 'subject-alice', 'organization')
BOB = TrustedIdentity('bob', 'subject-bob', 'organization')
CAROL = TrustedIdentity('carol', 'subject-carol', 'organization')


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(project_store, 'get_agent_root_dir', lambda: tmp_path)
    monkeypatch.setattr(lifecycle, 'get_agent_sessions_dir', lambda: tmp_path / 'sessions')
    monkeypatch.setattr(session_history, 'get_agent_sessions_dir', lambda: tmp_path / 'sessions')
    project_store.invalidate_cache()
    project = project_store.create_project('source', str(tmp_path / 'workspace'))
    access = ProjectAccessStore()
    access.initialize(project.project_id, 'admin')
    access.replace_acl(project.project_id, 'admin', acl={'alice': ['read', 'admin']}, expected_revision=1)
    known = {ALICE, BOB, CAROL}
    active = {i.actor_id: i for i in known}
    host = sharing_host.SharingHostService(lambda _, actor: active.get(actor),
                                          known_actor=lambda actor: actor in known, storage=access)
    session = tmp_path / 'sessions/session'
    session.mkdir(parents=True)
    (session/'metadata.json').write_text(json.dumps({'project_id': project.project_id, 'user_id': 'not-owner'}))
    (session/'history.jsonl').write_bytes(b'{"id":"original","role":"user","content":"hello"}\n')
    host.register_owner_and_source('session', ALICE, project.project_id)
    scope = host.prepare_source('session', ALICE)
    yield host, access, project.project_id, session, known, active, scope
    project_store.invalidate_cache()


def grant(host, scope, target=BOB, actions=('view',)):
    return host.store.grant('session', ALICE, target, actions=actions, history=scope, expires_at=None)


def allowed(host, record, actor=BOB, action='view'):
    from jiuwenswarm.governance.session_sharing import SessionHistoryRange
    return host.store.authorize('session', actor, action,
        history=SessionHistoryRange(**record['history']), share_id=record['share_id']).allowed


def test_real_grant_does_not_overwrite_source_or_unrelated_sidecar_data(setup, monkeypatch):
    host, access, _, _, _, _, scope = setup
    before = access._load()['session_sharing']['owners']['session']['source']
    data = access._load()
    data['host-extra'] = {'retained': True}
    access._save(data)
    saves = []
    original_save = access._save
    def save(value):
        saves.append(copy.deepcopy(value))
        original_save(value)
    monkeypatch.setattr(access, '_save', save)
    monkeypatch.setattr(sharing_host, 'compile_shared_history_range', lambda *a, **kw: pytest.fail('compile under sidecar'))
    record = grant(host, scope)
    assert allowed(host, record)
    assert len(saves) == 1  # resolver never saves under Store's older data snapshot
    assert access._load()['session_sharing']['owners']['session']['source'] == before
    assert access._load()['host-extra'] == {'retained': True}


def test_owner_token_logout_expiry_does_not_remove_persistent_sharing_authority(setup):
    host, _, _, _, _, active, scope = setup
    record = grant(host, scope)
    active.pop('alice')  # invitation directory / current credentials are distinct from known subjects
    assert host.resolve_source('session').owner == ALICE
    assert allowed(host, record)
    active.pop('bob')
    assert host.target_resolver(ALICE, 'bob') is None
    # Live recipient request authentication is still required by Adapter/main;
    # persistence alone does not authenticate a network caller.


def test_unknown_authority_subject_or_owner_mapping_never_falls_back_to_metadata(setup):
    host, access, _, session, known, _, scope = setup
    record = grant(host, scope)
    assert not host.owner_current('session', BOB)
    assert not host.owner_current('session', replace(ALICE, subject_id='another'))
    known.remove(ALICE)
    assert host.resolve_source('session') is None and not allowed(host, record)
    known.add(ALICE)
    metadata = json.loads((session/'metadata.json').read_text())
    metadata['user_id'] = 'bob'
    (session/'metadata.json').write_text(json.dumps(metadata))
    assert host.resolve_source('session').owner == ALICE
    data = access._load()
    del data['session_sharing']['owners']['session']['source']
    access._save(data)
    assert host.resolve_source('session') is None and not host.owner_current('session', ALICE)


def test_only_view_manage_no_execute_approve_discuss_download_inference(setup):
    host, _, _, _, _, _, scope = setup
    source = host.resolve_source('session')
    assert source.actions == {'view', 'manage'}
    for action in ('execute', 'approve', 'discuss', 'download'):
        with pytest.raises(SessionSharingDenied):
            grant(host, scope, actions=(action,))
        assert not host.owner_current('session', ALICE, action)
        with pytest.raises(SessionSharingDenied):
            with host.session_action_guard('session', ALICE, action):
                pytest.fail('unexpected entry')


def test_project_acl_revocation_restoration_never_revives_old_shares(setup):
    host, access, pid, _, _, _, scope = setup
    record = grant(host, scope)
    revision = host.resolve_source('session').revision
    access.replace_acl(pid, 'admin', acl={}, expected_revision=2)
    assert not allowed(host, record)
    access.replace_acl(pid, 'admin', acl={'alice': ['read', 'admin']}, expected_revision=3)
    assert host.resolve_source('session').revision > revision
    assert not allowed(host, record)
    assert allowed(host, grant(host, scope))
    fresh = sharing_host.SharingHostService(host._directory, known_actor=host._known_actor, storage=access)
    assert not allowed(fresh, record)


def test_rebind_invalidate_before_metadata_change_then_cas_activate(setup):
    host, access, pid, session, _, _, scope = setup
    record = grant(host, scope)
    second = project_store.create_project('second', str(session.parent/'second'))
    access.initialize(second.project_id, 'alice')
    old_revision = host.resolve_source('session').revision
    epoch = host.invalidate_source('session', expected_epoch=1)
    assert epoch == 2 and not allowed(host, record)
    assert not host.owner_current('session', ALICE)
    with pytest.raises(SessionSharingConflict):
        host.activate_source('session', second.project_id, expected_epoch=1)
    with pytest.raises(SessionSharingDenied):
        host.activate_source('session', second.project_id, expected_epoch=2)
    (session/'metadata.json').write_text(json.dumps({'project_id': second.project_id}))
    host.activate_source('session', second.project_id, expected_epoch=2)
    assert host.resolve_source('session') is None  # no fabricated history at activation
    host.prepare_source('session', ALICE)
    assert host.resolve_source('session').revision > old_revision
    assert not allowed(host, record)
    # Rebinding back to identical project/history still has a new persistent epoch.
    epoch = host.invalidate_source('session', expected_epoch=2)
    (session/'metadata.json').write_text(json.dumps({'project_id': pid}))
    host.activate_source('session', pid, expected_epoch=epoch)
    host.prepare_source('session', ALICE)
    assert not allowed(host, record)


def test_unhooked_different_binding_fails_closed_and_return_requires_host_epoch_hook(setup):
    host, _, _, session, _, _, scope = setup
    record = grant(host, scope)
    (session/'metadata.json').write_text(json.dumps({'project_id': 'default', 'user_id': 'alice'}))
    assert host.resolve_source('session') is None and not allowed(host, record)
    # Detecting a change-and-return without reading in between requires the
    # pre-metadata invalidation hook; a process-local rebind cache cannot do it.


def test_lifecycle_generation_blocks_until_explicit_reactivation(setup):
    host, _, pid, _, _, _, scope = setup
    record = grant(host, scope)
    lifecycle.atomic_json(lifecycle.resource_path('session', 'session'), {'generation': 1})
    assert not allowed(host, record)
    epoch = host.invalidate_source('session', expected_epoch=1)
    host.activate_source('session', pid, expected_epoch=epoch)
    host.prepare_source('session', ALICE)
    assert not allowed(host, record)
    lifecycle.atomic_json(lifecycle.resource_path('project', pid), {'generation': 1, 'deleted': True})
    assert host.resolve_source('session') is None


def test_prepublication_registration_single_save_without_history_or_metadata(setup, monkeypatch):
    host, access, pid, session, _, _, _ = setup
    saves = []
    original = access._save
    def save(data):
        saves.append(copy.deepcopy(data))
        original(data)
    monkeypatch.setattr(access, '_save', save)
    assert host.register_owner_and_source('new-session', ALICE, pid) == 1
    assert len(saves) == 1
    assert host.store.registered_owner('new-session') == (ALICE, 1)
    assert host.resolve_source('new-session') is None
    assert saves[0]['session_sharing']['owners']['new-session']['source']['history'] is None
    assert not (session.parent/'new-session').exists()


def test_preparation_no_sidecar_lock_across_compile_or_queue_drain(setup, monkeypatch):
    host, access, _, _, _, _, _ = setup
    depth = [0]
    original_lock, original_compile = access._locked, sharing_host.compile_shared_history_range
    @contextmanager
    def locked():
        with original_lock():
            depth[0] += 1
            try:
                yield
            finally:
                depth[0] -= 1
    def compile(*args, **kwargs):
        assert depth[0] == 0
        return original_compile(*args, **kwargs)
    monkeypatch.setattr(access, '_locked', locked)
    monkeypatch.setattr(sharing_host, 'compile_shared_history_range', compile)
    host.prepare_source('session', ALICE)
    assert depth[0] == 0


def test_preparation_cas_rejects_late_acl_change_without_overwriting_authority(setup, monkeypatch):
    host, access, pid, _, _, _, _ = setup
    original = sharing_host.compile_shared_history_range
    def compile(*args, **kwargs):
        result = original(*args, **kwargs)
        access.replace_acl(pid, 'admin', acl={'alice': ['read', 'admin']}, expected_revision=2)
        return result
    monkeypatch.setattr(sharing_host, 'compile_shared_history_range', compile)
    with pytest.raises(SessionSharingConflict):
        host.prepare_source('session', ALICE)
    assert access._load()['projects'][pid]['acl_revision'] == 3


def test_append_keeps_old_grant_bounded_but_replacement_advances_epoch(setup):
    host, _, _, session, _, _, scope = setup
    record = grant(host, scope)
    with (session/'history.jsonl').open('ab') as stream:
        stream.write(b'{"id":"next"}\n')
    newer = host.compile_history('session', ALICE, None)
    assert host.source_epoch('session') == 1
    assert newer.contains(scope) and allowed(host, record)
    assert not host.store.authorize('session', BOB, 'view', history=newer, share_id=record['share_id']).allowed
    replacement = session/'replacement'
    replacement.write_bytes((session/'history.jsonl').read_bytes())
    replacement.replace(session/'history.jsonl')
    host.prepare_source('session', ALICE)
    assert host.source_epoch('session') == 2 and not allowed(host, record)


def test_delegated_compile_does_not_read_or_expand_parent_history(setup, monkeypatch):
    host, _, _, session, _, _, scope = setup
    parent = grant(host, scope, actions=('view', 'manage'))
    with (session/'history.jsonl').open('ab') as stream:
        stream.write(b'{"id":"private-later"}\n')
    host.prepare_source('session', ALICE)
    monkeypatch.setattr(sharing_host, 'compile_shared_history_range', lambda *a, **kw: pytest.fail('recipient compile'))
    assert host.compile_history('session', BOB, parent['share_id']) == scope
    child = host.store.grant('session', BOB, CAROL, actions={'view'}, history=scope, expires_at=None,
                            parent_share_id=parent['share_id'])
    assert allowed(host, child, actor=CAROL)
    host.store.revoke(parent['share_id'], ALICE, expected_revision=1)
    with pytest.raises(SessionSharingDenied):
        host.compile_history('session', BOB, parent['share_id'])
    assert not allowed(host, child, actor=CAROL)


@pytest.mark.parametrize('field', ['epoch', 'session_generation', 'project_generation'])
@pytest.mark.parametrize('bad', [True, -1, 1 << 64])
def test_malformed_epoch_components_fail_closed(setup, field, bad):
    host, access, _, _, _, _, scope = setup
    record = grant(host, scope)
    data = access._load()
    data['session_sharing']['owners']['session']['source'][field] = bad
    access._save(data)
    assert host.resolve_source('session') is None and not allowed(host, record)


def test_owner_tombstone_recreation_never_reuses_old_grant(setup):
    host, _, pid, _, _, _, scope = setup
    record = grant(host, scope)
    host.invalidate_source('session', expected_epoch=1)
    assert host.store.retire_owner('session', expected_revision=1) == 2
    assert not allowed(host, record)
    with pytest.raises(SessionSharingConflict):
        host.register_owner_and_source('session', ALICE, pid)
    assert host.register_owner_and_source('session', ALICE, pid, expected_owner_revision=2) == 3
    host.prepare_source('session', ALICE)
    assert not allowed(host, record)


def test_current_project_required_no_unknown_default_or_orphan_compatibility(setup):
    host, access, pid, _, _, _, scope = setup
    for candidate in ('default', 'default_work', 'missing'):
        with pytest.raises(SessionSharingDenied):
            host.register_owner_and_source('new-session', ALICE, candidate)
    record = grant(host, scope)
    project_store._projects_file().write_text('{"projects": []}')
    assert not allowed(host, record) and host.resolve_source('session') is None


def test_sync_guard_rechecks_shared_action_and_does_not_imply_download(setup):
    host, _, _, _, _, _, scope = setup
    record = grant(host, scope)
    with host.session_action_guard('session', BOB, 'view', history=scope, share_id=record['share_id']):
        pass
    with pytest.raises(SessionSharingDenied):
        with host.session_action_guard('session', BOB, 'download', history=scope, share_id=record['share_id']):
            pass
    host.store.revoke(record['share_id'], ALICE, expected_revision=1)
    with pytest.raises(SessionSharingDenied):
        with host.session_action_guard('session', BOB, 'view', history=scope, share_id=record['share_id']):
            pass


def test_resolver_and_store_grant_never_acquire_history_lock(setup, monkeypatch):
    host, _, _, _, _, _, scope = setup
    class ForbiddenLock:
        def __enter__(self):
            pytest.fail('history lock under sidecar resolver')
        def __exit__(self, *args):
            return False
    monkeypatch.setattr(session_history, '_FILE_LOCK', ForbiddenLock())
    assert host.resolve_source('session') is not None
    assert allowed(host, grant(host, scope))


def test_actual_adapter_ports_create_and_list_shared_fixed_range(setup):
    from types import SimpleNamespace
    from jiuwenswarm.server.runtime.gateway_adapter.session_sharing_adapter import SessionSharingAdapter
    host, _, _, _, _, _, _ = setup
    actor = [ALICE]
    adapter = SessionSharingAdapter(host.store, identity_resolver=lambda _: actor[0],
        target_resolver=host.target_resolver, compile_history=host.compile_history)
    request = SimpleNamespace(req_method='session.share.create', request_id='share-request', channel_id='web',
        metadata={}, params={'session_id': 'session', 'target_actor': 'bob', 'actions': ['view'],
                             'expires_at': None, 'history_scope': 'current_snapshot'})
    result = adapter._dispatch(request)
    assert result.ok and result.payload['share']['target_actor'] == 'bob'
    actor[0] = BOB
    request.req_method, request.params = 'session.share.list', {}
    incoming = adapter._dispatch(request)
    assert incoming.payload['shares'][0]['share_id'] == result.payload['share']['share_id']
    assert incoming.payload['shares'][0]['actions'] == ['view']


def test_exhausted_epoch_cannot_wrap_or_reactivate_old_grants(setup):
    host, access, _, _, _, _, _ = setup
    data = access._load()
    data['session_sharing']['owners']['session']['source']['epoch'] = (1 << 64) - 1
    access._save(data)
    with pytest.raises(SessionSharingDenied):
        host.invalidate_source('session', expected_epoch=(1 << 64) - 1)
    assert host.source_epoch('session') == (1 << 64) - 1


def test_owner_revision_needs_no_history_and_never_writes(setup, monkeypatch):
    host, access, pid, session, _, _, _ = setup
    empty = session.parent / 'empty'
    empty.mkdir()
    (empty / 'metadata.json').write_text(json.dumps({'project_id': pid}))
    host.register_owner_and_source('empty', ALICE, pid)
    assert access._load()['session_sharing']['owners']['empty']['source']['history'] is None
    before = copy.deepcopy(access._load())
    monkeypatch.setattr(access, '_save', lambda *_: pytest.fail('revision lookup wrote sidecar'))
    monkeypatch.setattr(sharing_host, 'compile_shared_history_range', lambda *a, **kw: pytest.fail('history access'))
    revision = host.owner_revision('empty', ALICE)
    assert type(revision) is int and revision > 0
    assert host.owner_revision('empty', ALICE) == revision
    assert not (empty / 'history.jsonl').exists()
    assert access._load() == before


def test_owner_revision_changes_after_acl_revoke_restore(setup):
    host, access, pid, _, _, _, _ = setup
    old = host.owner_revision('session', ALICE)
    access.replace_acl(pid, 'admin', acl={}, expected_revision=2)
    with pytest.raises(SessionSharingDenied):
        host.owner_revision('session', ALICE)
    access.replace_acl(pid, 'admin', acl={'alice': ['read', 'admin']}, expected_revision=3)
    assert host.source_epoch('session') == 1
    assert host.owner_revision('session', ALICE) > old
    assert host.owner_revision('session', ALICE) == host.resolve_source('session').revision


@pytest.mark.parametrize('actor', [None, BOB, replace(ALICE, authority='other'),
                                  replace(ALICE, subject_id='other')])
def test_owner_revision_rejects_unknown_or_wrong_identity(setup, actor):
    host, _, _, _, _, _, _ = setup
    with pytest.raises(SessionSharingDenied):
        host.owner_revision('session', actor)


def test_owner_revision_rejects_invalidated_source_then_advances(setup):
    host, _, pid, _, _, _, _ = setup
    old = host.owner_revision('session', ALICE)
    epoch = host.invalidate_source('session', expected_epoch=1)
    with pytest.raises(SessionSharingDenied):
        host.owner_revision('session', ALICE)
    host.activate_source('session', pid, expected_epoch=epoch)
    assert host.owner_revision('session', ALICE) > old
