"""Persistent sharing authorization, distinct from subscriptions and routing IDs."""
import hashlib
import json
from dataclasses import replace

import pytest

from jiuwenswarm.governance.contracts import TrustedIdentity
from jiuwenswarm.governance.session_sharing import (
    SHARING_ACTIONS, SessionHistoryRange, SessionSharingAuthority,
    SessionSharingConflict, SessionSharingDenied,
)
from jiuwenswarm.server.runtime.session import project_store
from jiuwenswarm.server.runtime.session.project_access import ProjectAccessStore
from jiuwenswarm.server.runtime.session.session_sharing import SessionSharingStore

OWNER = TrustedIdentity('alice', 'human-alice', 'org')
READER = TrustedIdentity('bob', 'human-bob', 'org')
CHILD = TrustedIdentity('carol', 'human-carol', 'org')


def scope(session='session', start=0, end=100, snapshot_end=100, ino=20):
    return SessionHistoryRange(session, hashlib.sha256(f'{session}\0'.encode()).hexdigest(),
                               10, ino, snapshot_end, start, end)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(project_store, 'get_agent_root_dir', lambda: tmp_path)
    authority = [SessionSharingAuthority('session', OWNER, 1, SHARING_ACTIONS, scope())]
    clock = [100.0]
    store = SessionSharingStore(lambda _: authority[0], clock=lambda: clock[0])
    store.register_owner('session', OWNER)
    return store, authority, clock


def grant(store, **kwargs):
    options = dict(actions={'view'}, history=scope(), expires_at=200)
    options.update(kwargs)
    return store.grant('session', OWNER, READER, **options)


def check(store, record, *, actor=READER, action='view', history=None, session='session'):
    return store.authorize(session, actor, action, history=history or scope(), share_id=record['share_id']).allowed


def test_unknown_owner_cannot_be_claimed_by_routing_or_project_defaults(tmp_path, monkeypatch):
    monkeypatch.setattr(project_store, 'get_agent_root_dir', lambda: tmp_path)
    authority = SessionSharingAuthority('session', OWNER, 1, SHARING_ACTIONS, scope())
    store = SessionSharingStore(lambda _: authority)
    assert not store.authorize('session', OWNER, 'view', history=scope()).allowed
    with pytest.raises(SessionSharingDenied):
        grant(store)
    with pytest.raises(SessionSharingDenied):
        store.register_owner('session', {'actor_id': 'alice'})
    store.register_owner('session', OWNER)
    assert store.authorize('session', OWNER, 'view', history=scope()).allowed
    with pytest.raises(SessionSharingConflict):
        store.register_owner('session', READER, expected_revision=1)


@pytest.mark.parametrize('action', sorted(SHARING_ACTIONS))
def test_actions_are_independent_and_target_is_full_trusted_identity(setup, action):
    store, _, _ = setup
    record = grant(store, actions={action})
    for other in SHARING_ACTIONS:
        assert check(store, record, action=other) is (action == other)
    for wrong in (OWNER, replace(READER, authority='another-org'), replace(READER, subject_id='another-human')):
        assert not check(store, record, actor=wrong, action=action)
    assert not check(store, record, actor={'actor_id': 'bob'}, action=action)
    assert not check(store, record, action='read')


def test_exact_range_snapshot_and_session_never_expand(setup):
    store, source, _ = setup
    record = grant(store, history=scope(start=20, end=50))
    assert check(store, record, history=scope(start=20, end=50))
    assert check(store, record, history=scope(start=30, end=40))
    for outside in (scope(start=19, end=50), scope(start=20, end=51), scope(ino=21), scope('other')):
        assert not check(store, record, history=outside)
    # Source history can append without mutating this share's pinned end.
    source[0] = replace(source[0], history=scope(end=200, snapshot_end=200))
    assert check(store, record, history=scope(start=20, end=50))
    assert not check(store, record, history=scope(start=20, end=150, snapshot_end=200))
    assert not check(store, record, session='other', history=scope('other'))
    with pytest.raises(SessionSharingDenied):
        grant(store, history=scope('other'))


def test_range_contains_intersection_and_subagent_rejection():
    left, right = scope(start=10, end=60), scope(start=40, end=80)
    assert left.intersection(right) == scope(start=40, end=60)
    assert left.contains(scope(start=20, end=50))
    assert left.intersection(scope(start=70, end=90)) is None
    assert left.intersection(scope(ino=21)) is None
    assert not left.contains(scope(end=200, snapshot_end=200))
    with pytest.raises(ValueError):
        replace(left, stream=hashlib.sha256(b'session\0child').hexdigest())
    for bad in ({'start': -1}, {'end': 101}, {'dev': True}, {'start': 70}):
        with pytest.raises(ValueError):
            replace(left, **bad)


def test_owner_and_current_host_mapping_rechecked_each_use(setup):
    store, source, _ = setup
    record = grant(store)
    original = source[0]
    for unavailable in (None, replace(original, owner=replace(OWNER, authority='other')),
                        replace(original, owner=READER), replace(original, actions=frozenset({'manage'})),
                        replace(original, session_id='other', history=scope('other'))):
        source[0] = unavailable
        assert not check(store, record)
    source[0] = replace(original, revision=2)
    assert not check(store, record)  # Restoring permissions with a new epoch cannot revive old grants.
    assert check(store, grant(store))


def test_delegation_is_subset_and_parent_revocation_does_not_revive(setup):
    store, _, _ = setup
    parent = grant(store, actions={'view', 'manage'}, history=scope(end=80), expires_at=180)
    child = store.grant('session', READER, CHILD, actions={'view'}, history=scope(end=60),
                        expires_at=170, parent_share_id=parent['share_id'])
    assert check(store, child, actor=CHILD, history=scope(end=60))
    for overrides in ({'actions': {'execute'}}, {'expires_at': 181}, {'expires_at': None}, {'history': scope(end=90)}):
        options = dict(actions={'view'}, history=scope(end=60), expires_at=170, parent_share_id=parent['share_id'])
        options.update(overrides)
        with pytest.raises(SessionSharingDenied):
            store.grant('session', READER, CHILD, **options)
    store.revoke(parent['share_id'], OWNER, expected_revision=1)
    assert not check(store, child, actor=CHILD, history=scope(end=60))
    grant(store, actions={'view', 'manage'}, history=scope(end=80), expires_at=180)
    assert not check(store, child, actor=CHILD, history=scope(end=60))
    with pytest.raises(SessionSharingDenied):
        store.revise(parent['share_id'], OWNER, actions={'view'}, history=scope(end=80), expires_at=180, expected_revision=2)


def test_parent_revision_change_invalidates_children_even_if_bounds_match(setup):
    store, _, _ = setup
    parent = grant(store, actions={'view', 'manage'})
    child = store.grant('session', READER, CHILD, actions={'view'}, history=scope(),
                        expires_at=180, parent_share_id=parent['share_id'])
    updated = store.revise(parent['share_id'], OWNER, actions={'view', 'manage'}, history=scope(), expires_at=200, expected_revision=1)
    assert updated['revision'] == 2 and updated['created_at'] == parent['created_at']
    assert check(store, updated)
    assert not check(store, child, actor=CHILD)
    with pytest.raises(SessionSharingConflict):
        store.revise(parent['share_id'], OWNER, actions={'view'}, history=scope(), expires_at=200, expected_revision=1)


def test_manage_never_implied_and_wrong_grantor_cannot_revise_revoke(setup):
    store, source, _ = setup
    record = grant(store)
    with pytest.raises(SessionSharingDenied):
        store.grant('session', READER, CHILD, actions={'view'}, history=scope(), expires_at=180, parent_share_id=record['share_id'])
    with pytest.raises(SessionSharingDenied):
        store.revoke(record['share_id'], READER, expected_revision=1)
    with pytest.raises(SessionSharingDenied):
        store.revise(record['share_id'], READER, actions={'view'}, history=scope(), expires_at=180, expected_revision=1)
    source[0] = replace(source[0], actions=frozenset({'view'}))
    with pytest.raises(SessionSharingDenied):
        grant(store)


def test_expiry_is_current_and_cannot_exceed_host_authority(setup):
    store, source, clock = setup
    source[0] = replace(source[0], expires_at=180)
    for expiry in (None, 181, float('inf'), float('nan'), True, 100):
        with pytest.raises(SessionSharingDenied):
            grant(store, expires_at=expiry)
    record = grant(store, expires_at=170)
    assert check(store, record)
    clock[0] = 170
    assert not check(store, record)


def test_retired_and_recreated_session_id_cannot_restore_old_share(setup):
    store, _, _ = setup
    record = grant(store)
    assert store.retire_owner('session', expected_revision=1) == 2
    assert not check(store, record)
    with pytest.raises(SessionSharingConflict):
        store.register_owner('session', OWNER)
    assert store.register_owner('session', OWNER, expected_revision=2) == 3
    assert not check(store, record)
    assert check(store, grant(store))
    persisted = store._storage._load()['session_sharing']
    assert persisted['shares'][record['share_id']]['owner_revision'] == 1


def test_fresh_store_sees_revoke_and_preserves_other_sidecar_sections(setup):
    store, source, clock = setup
    with store._storage._locked():
        data = store._storage._load()
        data['projects']['existing'] = {'kept': ['value']}
        data['future'] = {'kept': True}
        store._storage._save(data)
    record = grant(store)
    fresh = SessionSharingStore(lambda _: source[0], storage=ProjectAccessStore(), clock=lambda: clock[0])
    assert check(fresh, record)
    assert fresh.revoke(record['share_id'], OWNER, expected_revision=1) == 2
    assert not check(store, record)
    data = store._storage._load()
    assert data['projects']['existing'] == {'kept': ['value']}
    assert data['future'] == {'kept': True}
    assert data['session_sharing']['shares'][record['share_id']]['revoked_by'] == {'actor_id': 'alice', 'subject_id': 'human-alice', 'authority': 'org'}


def test_corrupt_storage_and_resolver_errors_fail_closed(setup):
    store, _, _ = setup
    record = grant(store)
    original = store._storage.path.read_text()
    for raw in ('{bad', json.dumps({'schema_version': 1, 'projects': {}, 'session_sharing': []})):
        store._storage.path.write_text(raw)
        assert not check(store, record)
    store._storage.path.write_text(original)
    def unavailable(_):
        raise RuntimeError('host unavailable')
    store._resolve = unavailable
    assert not check(store, record)


def test_cycles_and_corrupt_parent_revision_fail_closed(setup):
    store, _, _ = setup
    parent = grant(store, actions={'view', 'manage'})
    child = store.grant('session', READER, CHILD, actions={'view'}, history=scope(), expires_at=180,
                        parent_share_id=parent['share_id'])
    with store._storage._locked():
        data = store._storage._load()
        records = data['session_sharing']['shares']
        records[parent['share_id']]['parent_share_id'] = child['share_id']
        store._storage._save(data)
    assert not check(store, child, actor=CHILD)


def test_registered_owner_is_explicit_host_mapping_and_retirement_removes_it(setup):
    store, _, _ = setup
    assert store.registered_owner('unknown') is None
    assert store.registered_owner('session') == (OWNER, 1)
    store.retire_owner('session', expected_revision=1)
    assert store.registered_owner('session') is None


def test_cross_process_revoke_and_host_source_independent(setup):
    import os
    import subprocess
    import sys
    store, _, _ = setup
    record = grant(store)
    script = '''
import hashlib, sys
from pathlib import Path
from jiuwenswarm.governance.contracts import TrustedIdentity
from jiuwenswarm.governance.session_sharing import SessionHistoryRange, SessionSharingAuthority, SHARING_ACTIONS
from jiuwenswarm.server.runtime.session import project_store
from jiuwenswarm.server.runtime.session.session_sharing import SessionSharingStore
project_store.get_agent_root_dir = lambda: Path(sys.argv[1])
owner = TrustedIdentity('alice', 'human-alice', 'org')
history = SessionHistoryRange('session', hashlib.sha256(b'session\\0').hexdigest(), 10, 20, 100, 0, 100)
source = SessionSharingAuthority('session', owner, 1, SHARING_ACTIONS, history)
SessionSharingStore(lambda _: source, clock=lambda: 100).revoke(sys.argv[2], owner, expected_revision=1)
'''
    result = subprocess.run([sys.executable, '-c', script, str(store._storage.path.parent), record['share_id']],
                            capture_output=True, text=True, timeout=20, env=os.environ.copy())
    assert result.returncode == 0, result.stderr
    assert not check(store, record)


def test_concurrent_revisions_have_one_winner_and_no_lost_update(setup):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    store, _, _ = setup
    record = grant(store)
    barrier = threading.Barrier(2)
    def revise(end):
        barrier.wait(timeout=5)
        try:
            return store.revise(record['share_id'], OWNER, actions={'view'}, history=scope(end=end),
                                expires_at=180, expected_revision=1)
        except SessionSharingConflict:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(revise, [50, 70]))
    assert sum(result is not None for result in results) == 1
    winner = next(result for result in results if result)
    assert winner['revision'] == 2
    assert check(store, winner, history=SessionHistoryRange(**winner['history']))
    assert not check(store, winner, history=scope())


def test_synchronous_guard_uses_existing_sidecar_lock_and_rechecks(setup):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    store, _, _ = setup
    record = grant(store)
    entered = threading.Event()
    def revoke():
        entered.set()
        return store.revoke(record['share_id'], OWNER, expected_revision=1)
    with ThreadPoolExecutor(max_workers=1) as pool:
        with store.guard('session', READER, 'view', history=scope(), share_id=record['share_id']):
            future = pool.submit(revoke)
            assert entered.wait(5)
            assert not future.done()
            assert check(store, record)
        assert future.result(timeout=5) == 2
    with pytest.raises(SessionSharingDenied):
        with store.guard('session', READER, 'view', history=scope(), share_id=record['share_id']):
            pytest.fail('revoked authorization entered guarded operation')


def test_actor_listing_filters_identity_and_current_authority(setup):
    store, source, _ = setup
    record = grant(store)
    assert [item['share_id'] for item in store.list_for_actor(READER)] == [record['share_id']]
    assert store.list_for_actor(replace(READER, authority='another')) == []
    assert store.list_for_actor(replace(READER, subject_id='another')) == []
    source[0] = replace(source[0], revision=2)
    assert store.list_for_actor(READER) == []
    obsolete = store.list_for_actor(OWNER)
    assert obsolete == [{'share_id': record['share_id'], 'session_id': 'session', 'revision': 1, 'state': 'unavailable'}]
    assert 'history' not in obsolete[0] and 'target' not in obsolete[0]
    source[0] = replace(source[0], actions=frozenset({'view'}))
    assert store.list_for_actor(OWNER) == []


def test_session_listing_current_owner_manage_and_revoked_redaction(setup):
    store, source, _ = setup
    record = grant(store)
    assert store.list_for_session('session', OWNER)[0]['history'] == record['history']
    for identity in (READER, replace(OWNER, authority='other'), replace(OWNER, subject_id='other')):
        with pytest.raises(SessionSharingDenied):
            store.list_for_session('session', identity)
    store.revoke(record['share_id'], OWNER, expected_revision=1)
    assert store.list_for_actor(READER) == []
    assert store.list_for_session('session', OWNER) == [
        {'share_id': record['share_id'], 'session_id': 'session', 'revision': 2, 'state': 'unavailable'}]
    source[0] = replace(source[0], actions=frozenset({'view'}))
    with pytest.raises(SessionSharingDenied):
        store.list_for_session('session', OWNER)


def test_delegated_grantor_listing_needs_current_parent_manage(setup):
    store, _, _ = setup
    parent = grant(store, actions={'view', 'manage'})
    child = store.grant('session', READER, CHILD, actions={'view'}, history=scope(),
                        expires_at=180, parent_share_id=parent['share_id'])
    assert {item['share_id'] for item in store.list_for_actor(READER)} == {parent['share_id'], child['share_id']}
    store.revoke(parent['share_id'], OWNER, expected_revision=1)
    assert store.list_for_actor(READER) == []
    assert store.list_for_actor(CHILD) == []


def test_corrupt_owner_cannot_reset_incarnation_and_revive_old_grants(setup):
    store, _, _ = setup
    record = grant(store)
    data = store._storage._load()
    data['session_sharing']['owners']['session'] = {}
    store._storage._save(data)
    with pytest.raises(SessionSharingConflict):
        store.register_owner('session', OWNER)
    assert not check(store, record)


def test_malformed_actions_and_boolean_revisions_are_not_valid_storage(setup):
    store, _, _ = setup
    record = grant(store)
    original = store._storage._load()
    import copy
    for field, value in (('actions', {'view': True}), ('owner_revision', True), ('source_revision', True)):
        data = copy.deepcopy(original)
        data['session_sharing']['shares'][record['share_id']][field] = value
        store._storage._save(data)
        assert not check(store, record)


def test_incoming_listing_rechecks_before_returning_each_item(setup):
    store, source, _ = setup
    grant(store)
    calls = [0]
    def changing(_):
        calls[0] += 1
        return source[0] if calls[0] == 1 else replace(source[0], revision=2)
    store._resolve = changing
    assert store.list_for_actor(READER) == []
    assert calls[0] >= 2
