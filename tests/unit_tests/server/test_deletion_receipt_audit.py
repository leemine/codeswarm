"""Real receipt/owner/lifecycle files; no Runtime exit or Provider inference."""
import copy
from dataclasses import replace

import pytest

from jiuwenswarm.governance.session_sharing import SessionSharingDenied
from jiuwenswarm.server.runtime.session import lifecycle
from jiuwenswarm.server.runtime.session.deletion_receipt import DeletionAuditPending
from jiuwenswarm.server.runtime.session.sharing_audit import SharingAuditContext, SharingAuditError
from jiuwenswarm.server.runtime.session.sharing_host import SharingHostService
from tests.unit_tests.server import test_session_deletion_receipt as original

setup, deletion = original.setup, original.deletion
SID = original.SID


def context(attempt='attempt-original', request='request-original'):
    return SharingAuditContext(original.source.ALICE, request, 'session.delete', attempt)


def rows(d):
    return [e for e in d.access._load()['sharing_audit']['events'] if e['facts']['action'] == 'delete']


def start(d, **kwargs):
    op = lifecycle.begin('session', SID, 'delete')
    return d.host.begin_deletion(d.capture, op, audit_context=context(), **kwargs)


def corrupt(d):
    data = d.access._load()
    previous = copy.deepcopy(data.get('sharing_audit'))
    data['sharing_audit'] = {'schema_version': 'damaged'}
    d.access._save(data)
    return previous


def repair(d, history):
    data = d.access._load()
    if history is None:
        data.pop('sharing_audit', None)
    else:
        data['sharing_audit'] = history
    d.access._save(data)


def restarted(d):
    host = SharingHostService(d.host._directory, known_actor=d.host._known_actor, storage=d.access)
    receipt = host.resume_deletion(SID, original.source.ALICE, identity_resolver=d.resolve)
    return host, receipt


def test_begin_and_retire_append_in_exact_original_save(deletion, monkeypatch):  # noqa: F811
    d = deletion
    saved, notified = [], []
    real = d.access._save
    def save(data):
        saved.append(copy.deepcopy(data))
        real(data)
    monkeypatch.setattr(d.access, '_save', save)
    receipt = start(d, audit_result=notified.append)
    assert len(saved) == 1
    first = saved[0]
    assert first['session_sharing']['owners'][SID]['source']['active'] is False
    assert first['sharing_audit']['events'][-1]['facts']['phase'] == 'exit_requested'
    assert original.owner(d)['retired'] is False
    assert notified[-1].persisted
    original.erase(d)
    d.host.commit_deletion(receipt, audit_context=context(), audit_result=notified.append)
    assert len(saved) == 2
    assert saved[-1]['session_sharing']['owners'][SID]['retired'] is True
    assert saved[-1]['sharing_audit']['events'][-1]['facts']['phase'] == 'exit_confirmed'
    assert rows(d)[-1]['context']['request_id'] == 'request-original'
    assert not d.host.deletion_audit_pending(receipt)
    d.host.commit_deletion(receipt, audit_context=context('another-attempt', 'another-request'))
    assert len(saved) == 2 and len(rows(d)) == 2


@pytest.mark.parametrize('phase', ['begin', 'adopt'])
def test_bad_audit_blocks_new_destructive_admission_without_mutating_receipt(deletion, phase):  # noqa: F811
    d = deletion
    receipt = start(d) if phase == 'adopt' else None
    corrupt(d)
    before = d.access.path.read_bytes()
    with pytest.raises(SharingAuditError):
        if phase == 'begin':
            start(d)
        else:
            d.host.adopt_deletion(receipt, lifecycle.state('session', SID)['operation'], audit_context=context('retry'))
    assert d.access.path.read_bytes() == before
    assert original.owner(d)['retired'] is False
    assert d.setup.path.exists()


def test_retired_pending_survives_restart_and_only_original_context_is_supplemented(deletion):  # noqa: F811
    d = deletion
    receipt = start(d)
    previous = corrupt(d)
    original.erase(d)
    with pytest.raises(DeletionAuditPending) as error:
        d.host.commit_deletion(receipt, audit_context=context())
    assert error.value.receipt is receipt and error.value.audit.persisted is False
    assert d.host.confirms_deletion(receipt) and d.host.deletion_audit_pending(receipt)
    assert original.owner(d)['retired'] is True
    pending = copy.deepcopy(original.owner(d)['deletion']['audit_pending'])
    assert pending['context']['request_id'] == 'request-original'
    host, recovered = restarted(d)
    before = d.access.path.read_bytes()
    with pytest.raises(DeletionAuditPending):
        host.supplement_deletion_audit(recovered)
    assert d.access.path.read_bytes() == before
    repair(d, previous)  # Simulated operator storage recovery, never automatic repair.
    host.commit_deletion(recovered, audit_context=context('new-attempt', 'new-request'))
    assert not host.deletion_audit_pending(recovered)
    assert 'audit_pending' not in original.owner(d)['deletion']
    event = rows(d)[-1]
    assert event['context'] == pending['context'] and event['facts'] == pending['facts']
    assert host.supplement_deletion_audit(recovered) is None
    assert len(rows(d)) == 2


@pytest.mark.parametrize('after_save', [False, True])
def test_pending_save_unknown_outcome_reconciles_original_receipt(deletion, monkeypatch, after_save):  # noqa: F811
    d = deletion
    receipt = start(d)
    previous = corrupt(d)
    original.erase(d)
    real = d.access._save
    def failure(data):
        if after_save:
            real(data)
        raise OSError('synthetic save failure')
    monkeypatch.setattr(d.access, '_save', failure)
    with pytest.raises(OSError):
        d.host.commit_deletion(receipt, audit_context=context())
    monkeypatch.setattr(d.access, '_save', real)
    assert original.owner(d)['retired'] is after_save
    if not after_save:
        with pytest.raises(DeletionAuditPending):
            d.host.commit_deletion(receipt, audit_context=context())
    host, recovered = restarted(d)
    assert host.deletion_audit_pending(recovered)
    repair(d, previous)
    host.supplement_deletion_audit(recovered)
    assert not host.deletion_audit_pending(recovered) and len(rows(d)) == 2


@pytest.mark.parametrize('after_save', [False, True])
def test_supplement_atomic_save_is_retryable_without_duplicate_confirmation(deletion, monkeypatch, after_save):  # noqa: F811
    d = deletion
    receipt = start(d)
    previous = corrupt(d)
    original.erase(d)
    with pytest.raises(DeletionAuditPending):
        d.host.commit_deletion(receipt, audit_context=context())
    repair(d, previous)
    real = d.access._save
    def failure(data):
        if after_save:
            real(data)
        raise OSError('synthetic supplement failure')
    monkeypatch.setattr(d.access, '_save', failure)
    with pytest.raises(OSError):
        d.host.supplement_deletion_audit(receipt)
    monkeypatch.setattr(d.access, '_save', real)
    assert d.host.deletion_audit_pending(receipt) is (not after_save)
    host, recovered = restarted(d)
    host.supplement_deletion_audit(recovered)
    assert len(rows(d)) == 2 and original.owner(d)['retired'] is True


@pytest.mark.parametrize('damage', ['unknown', 'nonce', 'identity', 'phase', 'generation', 'owner_revision'])
def test_unknown_or_corrupt_pending_never_reset_or_consumed(deletion, damage):  # noqa: F811
    d = deletion
    receipt = start(d)
    previous = corrupt(d)
    original.erase(d)
    with pytest.raises(DeletionAuditPending):
        d.host.commit_deletion(receipt, audit_context=context())
    def mutate(record):
        pending = record['deletion']['audit_pending']
        if damage == 'unknown':
            pending['unknown'] = True
        elif damage == 'nonce':
            pending['deletion_id'] = 'other'
        elif damage == 'identity':
            pending['context']['actor']['subject_id'] = 'other'
        else:
            pending['facts'][damage] = {'phase': 'exit_requested', 'generation': 999, 'owner_revision': 999}[damage]
    original.change(d, mutate)
    repair(d, previous)
    before = d.access.path.read_bytes()
    with pytest.raises(SessionSharingDenied):
        d.host.supplement_deletion_audit(receipt)
    assert d.access.path.read_bytes() == before
    with pytest.raises(SessionSharingDenied):
        d.host.deletion_audit_pending(receipt)


def test_new_generation_never_relabels_original_pending_confirmation(deletion):  # noqa: F811
    d = deletion
    receipt = start(d)
    previous = corrupt(d)
    original.erase(d)
    with pytest.raises(DeletionAuditPending):
        d.host.commit_deletion(receipt, audit_context=context())
    pending = copy.deepcopy(original.owner(d)['deletion']['audit_pending'])
    lifecycle.claim_operation('session', SID, 'first')
    op = lifecycle.claim_operation('session', SID, 'second')
    host, recovered = restarted(d)
    assert recovered.generation > receipt.generation
    with pytest.raises(DeletionAuditPending):
        host.adopt_deletion(recovered, op, audit_context=context('retry', 'retry-request'))
    assert original.owner(d)['deletion']['audit_pending'] == pending
    repair(d, previous)
    host.supplement_deletion_audit(recovered)
    assert rows(d)[-1]['facts']['generation'] == receipt.generation
    host.adopt_deletion(recovered, op, audit_context=context('retry', 'retry-request'))
    assert rows(d)[-1]['facts']['phase'] == 'cleanup_retry'


def test_legacy_retired_without_pending_is_not_reconstructed_as_history(deletion):  # noqa: F811
    d = deletion
    receipt = start(d)
    original.erase(d)
    d.host.commit_deletion(receipt)
    data = d.access._load()
    data.pop('sharing_audit')
    d.access._save(data)
    before = d.access.path.read_bytes()
    d.host.commit_deletion(receipt, audit_context=context())
    assert d.host.supplement_deletion_audit(receipt) is None
    assert d.access.path.read_bytes() == before


def test_pending_status_requires_original_live_identity_and_readable_store(deletion, monkeypatch):  # noqa: F811
    d = deletion
    receipt = start(d)
    original.erase(d)
    d.host.commit_deletion(receipt)
    d.setup.identities[0] = replace(original.source.ALICE, subject_id='wrong')
    with pytest.raises(SessionSharingDenied):
        d.host.deletion_audit_pending(receipt)
    d.setup.identities[0] = original.source.ALICE
    monkeypatch.setattr(d.access, '_load', lambda: (_ for _ in ()).throw(OSError('read unavailable')))
    with pytest.raises(OSError):
        d.host.deletion_audit_pending(receipt)
