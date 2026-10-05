"""Real sidecar mutation/audit atomicity; no synthetic audit persistence sink."""
import asyncio
import copy
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, asdict, replace
from types import SimpleNamespace

import pytest

from jiuwenswarm.governance.contracts import TrustedIdentity
from jiuwenswarm.governance.session_boundary import _inventory_revision
from jiuwenswarm.governance.session_sharing import SessionSharingConflict
from jiuwenswarm.server.runtime.session.sharing_audit import (
    SharingAuditBounds, SharingAuditContext, SharingAuditError, SharingAuditFacts,
    append_sharing_audit,
)
from tests.unit_tests.server import test_session_sharing as sharing

setup = sharing.setup


@pytest.fixture
def auditlog(caplog):
    from jiuwenswarm.server.runtime.session.sharing_audit import logger
    logger.addHandler(caplog.handler)
    try:
        yield caplog
    finally:
        logger.removeHandler(caplog.handler)


def audit(store):
    return store._storage._load()['sharing_audit']


def test_legacy_and_request_mutations_keep_all_versions_in_same_save(setup, monkeypatch):
    store, _, _ = setup
    saves, results = [], []
    original = store._storage._save

    def save(data):
        event = data['sharing_audit']['events'][-1]
        current = data['session_sharing']['shares'][event['facts']['share_id']]
        assert current['revision'] == event['facts']['after_revision']
        saves.append(copy.deepcopy(data))
        original(data)

    monkeypatch.setattr(store._storage, '_save', save)
    record = sharing.grant(store, audit_result=results.append)
    context = SharingAuditContext(sharing.OWNER, 'request-2', 'session.share.update')
    updated = store.revise(record['share_id'], sharing.OWNER, actions={'view', 'execute'},
                           history=sharing.scope(end=80), expires_at=180, expected_revision=1,
                           audit_context=context, audit_result=results.append)
    assert type(updated) is dict and updated['revision'] == 2
    result = store.revoke(record['share_id'], sharing.OWNER, expected_revision=2, audit_result=results.append)
    assert type(result) is int and result == 3
    assert len(saves) == 3 and [r.sequence for r in results] == [1, 2, 3]
    events = audit(store)['events']
    assert [e['facts']['action'] for e in events] == ['create', 'update', 'revoke']
    assert [e['facts']['before_revision'] for e in events] == [0, 1, 2]
    assert events[0]['context']['request_id'] is None and events[0]['context']['method'] is None
    assert events[1]['context'] == asdict(context)
    assert events[1]['facts']['bounds_before'] == events[0]['facts']['bounds_after']
    assert events[2]['facts']['bounds_before'] == events[1]['facts']['bounds_after']
    assert events[0]['facts']['bounds_after']['history']['end'] == 100
    assert events[1]['facts']['bounds_after']['history']['end'] == 80
    assert all(r.persisted is True and r.degraded is False for r in results)
    with pytest.raises(FrozenInstanceError):
        results[0].persisted = False


@pytest.mark.parametrize('operation', ['create', 'update', 'revoke'])
def test_failed_atomic_replace_keeps_old_authority_and_history(setup, monkeypatch, operation):
    from jiuwenswarm.server.runtime.session import project_access
    store, _, _ = setup
    record = sharing.grant(store)
    before = store._storage.path.read_bytes()
    results = []

    def failed(*args):
        raise OSError('synthetic failure')

    monkeypatch.setattr(project_access.os, 'replace', failed)
    with pytest.raises(OSError):
        if operation == 'create':
            sharing.grant(store, audit_result=results.append)
        elif operation == 'update':
            store.revise(record['share_id'], sharing.OWNER, actions={'view'}, history=sharing.scope(),
                         expires_at=180, expected_revision=1, audit_result=results.append)
        else:
            store.revoke(record['share_id'], sharing.OWNER, expected_revision=1, audit_result=results.append)
    assert store._storage.path.read_bytes() == before
    assert results == []
    assert sharing.check(store, record)


def test_concurrent_revision_cas_commits_only_one_audit_event(setup):
    store, _, _ = setup
    record = sharing.grant(store)
    barrier = threading.Barrier(2)

    def update(expiry):
        barrier.wait(timeout=5)
        try:
            return store.revise(record['share_id'], sharing.OWNER, actions={'view'}, history=sharing.scope(),
                                expires_at=expiry, expected_revision=1)
        except SessionSharingConflict:
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(update, [180, 190]))
    assert sum(r is not None for r in results) == 1
    events = audit(store)['events']
    assert [e['sequence'] for e in events] == [1, 2]
    assert events[1]['facts']['bounds_after']['expires_at'] == next(r for r in results if r)['expires_at']
    # This slice does not invent denied/admission events.
    assert all(e['facts']['result'] == 'committed' for e in events)


@pytest.mark.parametrize('corrupt', [None, [], {'schema_version': 2},
    {'schema_version': 1, 'next_sequence': True, 'events': []},
    {'schema_version': 1, 'next_sequence': 2, 'events': []}])
def test_corrupt_audit_blocks_positive_changes_but_revoke_preserves_it(setup, corrupt, auditlog):
    store, _, _ = setup
    record = sharing.grant(store)
    data = store._storage._load()
    data['sharing_audit'] = corrupt
    store._storage._save(data)
    before = store._storage.path.read_bytes()
    with pytest.raises(SharingAuditError):
        sharing.grant(store)
    with pytest.raises(SharingAuditError):
        store.revise(record['share_id'], sharing.OWNER, actions={'view'}, history=sharing.scope(),
                     expires_at=180, expected_revision=1)
    assert store._storage.path.read_bytes() == before
    results = []
    assert store.revoke(record['share_id'], sharing.OWNER, expected_revision=1, audit_result=results.append) == 2
    assert store._storage._load()['sharing_audit'] == corrupt
    assert not sharing.check(store, record)
    assert len(results) == 1 and not results[0].persisted and results[0].degraded
    assert results[0].sequence is None and results[0].reason == 'audit_storage_invalid'
    assert 'sharing_audit_degraded: audit_storage_invalid' in auditlog.text


def test_corrupt_past_event_never_silently_replaced(setup):
    store, _, _ = setup
    record = sharing.grant(store)
    data = store._storage._load()
    data['sharing_audit']['events'][0]['facts']['body'] = 'must-preserve-corrupt-evidence'
    store._storage._save(data)
    before = copy.deepcopy(data['sharing_audit'])
    with pytest.raises(SharingAuditError):
        sharing.grant(store)
    store.revoke(record['share_id'], sharing.OWNER, expected_revision=1)
    assert store._storage._load()['sharing_audit'] == before


def test_revoke_records_original_grant_and_current_decision_revisions_separately(setup):
    store, source, _ = setup
    record = sharing.grant(store)
    source[0] = replace(source[0], revision=2)
    assert store.revoke(record['share_id'], sharing.OWNER, expected_revision=1) == 2
    facts = audit(store)['events'][-1]['facts']
    assert facts['source_revision'] == 1 and facts['decision_source_revision'] == 2


def test_conflicting_method_is_not_an_accurate_audit_context(setup):
    store, _, _ = setup
    before = store._storage.path.read_bytes()
    with pytest.raises(SharingAuditError):
        sharing.grant(store, audit_context=SharingAuditContext(sharing.OWNER, 'req', 'session.share.revoke'))
    assert store._storage.path.read_bytes() == before


@pytest.mark.parametrize('failure', [RuntimeError, asyncio.CancelledError])
def test_callback_is_postsave_outside_store_lock_failure_does_not_replay(setup, auditlog, failure):
    from jiuwenswarm.server.runtime.session.project_access import _HELD_LOCKS
    store, _, _ = setup
    calls = []

    def callback(result):
        assert str(store._storage.path) not in getattr(_HELD_LOCKS, 'paths', set())
        assert audit(store)['events'][-1]['event_id'] == result.event_id
        calls.append(result)
        raise failure('DO-NOT-LOG-CREDENTIAL')

    record = sharing.grant(store, audit_result=callback)
    store.revise(record['share_id'], sharing.OWNER, actions={'view'}, history=sharing.scope(),
                 expires_at=180, expected_revision=1, audit_result=callback)
    store.revoke(record['share_id'], sharing.OWNER, expected_revision=2, audit_result=callback)
    assert len(calls) == 3 and len(audit(store)['events']) == 3
    assert 'DO-NOT-LOG-CREDENTIAL' not in auditlog.text
    assert 'sharing_audit_result_callback_failed' in auditlog.text


def test_full_unicode_identity_and_exact_host_project_only(setup):
    store, source, _ = setup
    actor = TrustedIdentity('甲@example.test', '独立执行主体', '组织')
    source[0] = replace(source[0], owner=actor)
    data = store._storage._load()
    owner = data['session_sharing']['owners']['session']
    owner['identity'] = asdict(actor)
    owner['source'] = {'project_id': 'verified-project', 'credential': 'DO-NOT-COPY', 'path': '/private/workspace'}
    data['secret'] = 'DO-NOT-COPY'
    store._storage._save(data)
    record = store.grant('session', actor, sharing.READER, actions={'view'}, history=sharing.scope(), expires_at=180)
    event = audit(store)['events'][0]
    assert event['context']['actor'] == asdict(actor)
    assert event['facts']['target'] == asdict(sharing.READER)
    assert event['facts']['source_project_id'] == 'verified-project'
    text = json.dumps(event, ensure_ascii=False)
    assert 'DO-NOT-COPY' not in text and '/private/workspace' not in text
    assert record['share_id'] == event['facts']['share_id']
    with pytest.raises(SharingAuditError):
        store.revise(record['share_id'], actor, actions={'view'}, history=sharing.scope(), expires_at=180,
                     expected_revision=1, audit_context=SharingAuditContext(sharing.OWNER))


def test_only_top_level_audit_is_excluded_from_inventory_proof(setup):
    store, _, _ = setup
    record = sharing.grant(store)
    host = SimpleNamespace(_storage=store._storage)
    baseline = _inventory_revision(host)
    data = store._storage._load()
    # A standalone audit append is not an authority mutation.
    facts = SharingAuditFacts('update', 'session', record['share_id'], sharing.READER, 2, 1, 1,
        before_revision=1, after_revision=2, bounds_before=SharingAuditBounds.from_record(record),
        bounds_after=SharingAuditBounds.from_record(record))
    append_sharing_audit(data, SharingAuditContext(sharing.OWNER), facts)
    store._storage._save(data)
    assert _inventory_revision(host) == baseline
    for field in ('projects', 'session_sharing', 'resources', 'future_authority'):
        changed = store._storage._load()
        changed[field] = {'sharing_audit': 'still-authority'}
        original = store._storage._load()
        store._storage._save(changed)
        assert _inventory_revision(host) != baseline
        store._storage._save(original)
    for field in ('revision', 'source', 'continuation', 'deletion'):
        original = store._storage._load()
        changed = copy.deepcopy(original)
        changed['session_sharing']['owners']['session'][field] = {'changed': True}
        store._storage._save(changed)
        assert _inventory_revision(host) != baseline
        store._storage._save(original)


@pytest.mark.parametrize('bad', [True, float('nan'), float('inf'), 'now'])
def test_append_invalid_timestamp_leaves_snapshot_unchanged(setup, bad):
    store, _, _ = setup
    record = sharing.grant(store)
    data = store._storage._load()
    original = copy.deepcopy(data)
    facts = SharingAuditFacts('revoke', 'session', record['share_id'], sharing.READER, 2, 1, 1,
        before_revision=1, after_revision=2, bounds_before=SharingAuditBounds.from_record(record),
        bounds_after=SharingAuditBounds.from_record(record))
    with pytest.raises(SharingAuditError):
        append_sharing_audit(data, SharingAuditContext(sharing.OWNER), facts, recorded_at=bad)
    assert data == original
