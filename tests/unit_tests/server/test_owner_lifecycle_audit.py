"""Original sidecar append/query, with synthetic host exit observations only.

The Runtime owns actual resource exit and is not connected by this foundation
slice. Facts are captured from a real original owner registration, never wire.
"""
import copy
import json
from dataclasses import FrozenInstanceError, replace

import pytest

from jiuwenswarm.server.runtime.session.sharing_audit import (
    LIFECYCLE_AUDIT_COVERAGE, OwnerLifecycleAuditFacts, SharingAuditContext,
    SharingAuditError, append_sharing_audit, validated_sharing_audit,
)
from tests.unit_tests.governance.test_workspace_download import credentials, setup  # noqa: F401


def facts(s, phase='exit_requested', *, action='cancel'):
    data = s.host._storage._load()
    owner = data['session_sharing']['owners']['alice-session']
    return OwnerLifecycleAuditFacts('alice-session', owner['source']['project_id'],
        owner['revision'], owner['source']['epoch'], action, phase,
        {'exit_requested': 'requested', 'cleanup_retry': 'retrying',
         'exit_unconfirmed': 'unconfirmed', 'exit_confirmed': 'confirmed'}[phase],
        'original-close-operation', 1)


def context(s, attempt='attempt-1', request='request-1'):
    return SharingAuditContext(s.alice.identity(), request, 'chat.cancel', attempt)


def save(s, ctx, fact):
    with s.host._storage._locked():
        data = s.host._storage._load()
        result = append_sharing_audit(data, ctx, fact)
        s.host._storage._save(data)
    return result


def events(s):
    return validated_sharing_audit(s.host._storage._load())['events']


def test_attempts_retries_and_confirmation_have_distinct_idempotence(setup):  # noqa: F811
    s = setup
    ctx = context(s)
    a = save(s, ctx, facts(s))
    assert save(s, ctx, facts(s)) == a
    save(s, ctx, facts(s, 'exit_unconfirmed'))
    retry = context(s, 'attempt-2', 'request-2')
    save(s, retry, facts(s, 'cleanup_retry'))
    save(s, retry, facts(s, 'exit_unconfirmed'))
    confirmed = save(s, retry, facts(s, 'exit_confirmed'))
    third = context(s, 'attempt-3', 'request-3')
    assert save(s, third, facts(s, 'exit_confirmed')) == confirmed
    rows = events(s)
    assert len(rows) == 5
    assert [r['facts']['result'] for r in rows] == ['requested', 'unconfirmed', 'retrying', 'unconfirmed', 'confirmed']
    assert rows[-1]['context']['request_id'] == 'request-2'
    assert rows[-1]['context']['attempt_id'] == 'attempt-2'
    with pytest.raises(FrozenInstanceError):
        facts(s).result = 'confirmed'


@pytest.mark.parametrize('change', [
    {'owner_revision': 2}, {'source_revision': 2}, {'source_project_id': 'another-project'},
])
def test_same_original_operation_cannot_be_rewritten_under_new_authority(setup, change):  # noqa: F811
    s = setup
    old = facts(s, 'exit_confirmed')
    save(s, context(s), old)
    before = s.host._storage.path.read_bytes()
    with pytest.raises(SharingAuditError):
        save(s, context(s, 'retry', 'request-retry'), replace(old, **change))
    assert s.host._storage.path.read_bytes() == before


@pytest.mark.parametrize('identity_change', [{'actor_id': 'bob'}, {'subject_id': 'other'}, {'authority': 'other'}])
def test_confirmation_never_borrows_a_new_actor(setup, identity_change):  # noqa: F811
    s = setup
    save(s, context(s), facts(s, 'exit_confirmed'))
    ctx = replace(context(s, 'retry'), actor=replace(s.alice.identity(), **identity_change))
    with pytest.raises(SharingAuditError):
        save(s, ctx, facts(s, 'exit_confirmed'))
    assert len(events(s)) == 1


@pytest.mark.parametrize('change', [
    {'phase': 'terminal'}, {'phase': 'exit_unconfirmed', 'result': 'confirmed'},
    {'generation': True}, {'operation_id': ''}, {'operation_id': 'x' * 1025},
    {'owner_revision': True}, {'source_project_id': '/bad\npath'}, {'phase': []},
    {'action': 'delete', 'generation': None},
])
def test_invalid_or_ambiguous_observations_rejected(setup, change):  # noqa: F811
    with pytest.raises((SharingAuditError, TypeError)):
        replace(facts(setup), **change)


def test_methods_are_action_specific_and_wire_dict_cannot_append(setup):  # noqa: F811
    s = setup
    for method in ('session.share.create', 'session.delete'):
        with pytest.raises(SharingAuditError):
            save(s, replace(context(s), method=method), facts(s))
    with pytest.raises(SharingAuditError):
        save(s, context(s), {'action': 'cancel', 'phase': 'exit_confirmed'})
    save(s, replace(context(s), method='session.delete'), facts(s, action='delete'))
    assert len(events(s)) == 1


@pytest.mark.parametrize('after_save', [False, True])
def test_failed_or_uncertain_save_can_retry_original_receipt_without_duplicate(setup, monkeypatch, after_save):  # noqa: F811
    s = setup
    original = s.host._storage._save
    ctx, fact = context(s), facts(s, 'exit_confirmed')
    original_owner = copy.deepcopy(s.host._storage._load()['session_sharing'])
    def fail(data):
        if after_save:
            original(data)
        raise OSError('synthetic save failure')
    monkeypatch.setattr(s.host._storage, '_save', fail)
    with pytest.raises(OSError):
        save(s, ctx, fact)
    monkeypatch.setattr(s.host._storage, '_save', original)
    save(s, ctx, fact)
    assert len(events(s)) == 1
    assert s.host._storage._load()['session_sharing'] == original_owner


def test_corrupt_history_is_not_repaired_and_original_receipt_is_unchanged(setup):  # noqa: F811
    s = setup
    data = s.host._storage._load()
    data['sharing_audit'] = {'schema_version': 9}
    s.host._storage._save(data)
    before = s.host._storage.path.read_bytes()
    with pytest.raises(SharingAuditError):
        save(s, context(s), facts(s, 'exit_confirmed'))
    assert s.host._storage.path.read_bytes() == before


def test_current_owner_query_projects_only_safe_fields_and_limited_coverage(setup):  # noqa: F811
    s = setup
    save(s, context(s), facts(s, 'exit_unconfirmed'))
    page = s.host.store.query_audit('alice-session', s.alice.identity(),
        owner_guard=lambda: s.host.owner_revision('alice-session', s.alice.identity()))
    assert page['coverage'] == LIFECYCLE_AUDIT_COVERAGE
    row = page['events'][0]
    assert row['phase'] == 'exit_unconfirmed' and row['result'] == 'unconfirmed'
    assert all(row[name] is None for name in ('share_id', 'share_revision', 'target_actor_id', 'before_revision', 'after_revision'))
    assert not set(row) & {'operation_id', 'generation', 'source_project_id', 'owner_revision', 'source_revision'}
    encoded = json.dumps(page)
    assert 'original-close-operation' not in encoded and str(s.root) not in encoded
    with pytest.raises(Exception):
        s.host.store.query_audit('alice-session', s.bob.identity(), owner_guard=lambda: 1)


def test_concurrent_confirmations_share_one_original_event(setup):  # noqa: F811
    from concurrent.futures import ThreadPoolExecutor
    s = setup
    fact = facts(s, 'exit_confirmed')
    def append(index):
        return save(s, context(s, f'attempt-{index}', f'request-{index}'), fact)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(append, range(4)))
    assert len({r.event_id for r in results}) == 1
    assert len(events(s)) == 1
