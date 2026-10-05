"""Original owner deletion receipts on real sidecar/metadata/lifecycle files."""
import asyncio
import copy
import json
import shutil
from dataclasses import replace
from types import SimpleNamespace

import pytest

from jiuwenswarm.governance.session_boundary import admit_session_request
from jiuwenswarm.governance.session_sharing import SessionSharingDenied, SessionSharingConflict
from jiuwenswarm.server.runtime.session import lifecycle
from jiuwenswarm.server.runtime.session.sharing_host import SharingHostService
from tests.unit_tests.server import test_continuation_source as source

setup = source.setup
SID = 'source-session'


@pytest.fixture
def deletion(setup):
    setup.identities[0] = source.ALICE
    def resolve():
        return setup.identities[0]
    permit = admit_session_request('session.delete', {'session_id': SID},
        host=setup.host, identity_resolver=resolve)
    capture = setup.host.capture_deletion(SID, source.ALICE, permit)
    return SimpleNamespace(**locals(), host=setup.host, access=setup.access)


def begin(value):
    operation = lifecycle.begin('session', SID, 'delete')
    return value.host.begin_deletion(value.capture, operation)


def erase(value):
    lifecycle.update('session', SID, phase='delete_directory')
    shutil.rmtree(value.setup.path.parent)


def owner(value):
    return value.access._load()['session_sharing']['owners'][SID]


def change(value, fn):
    with value.access._locked():
        data = value.access._load()
        fn(data['session_sharing']['owners'][SID])
        value.access._save(data)


def test_begin_one_save_invalidates_source_and_original_permit(deletion, monkeypatch):
    assert deletion.capture.descriptor['project_id'] == deletion.setup.source.project_id
    deletion.capture.descriptor['project_id'] = 'forged'
    assert deletion.capture.descriptor['project_id'] != 'forged'
    calls = []
    save = deletion.access._save
    monkeypatch.setattr(deletion.access, '_save', lambda data: (calls.append(copy.deepcopy(data)), save(data))[1])
    receipt = begin(deletion)
    assert len(calls) == 1 and owner(deletion)['source']['epoch'] == 2
    assert owner(deletion)['source']['active'] is False
    assert not deletion.permit.revalidate()
    assert not deletion.host.owner_current(SID, source.ALICE)
    deletion.host.check_deletion(receipt)
    assert deletion.host.begin_deletion(deletion.capture, lifecycle.state('session', SID)['operation']) == receipt
    assert len(calls) == 1


def test_metadata_deleted_retry_rebuilds_original_descriptor_then_retire(deletion):
    receipt = begin(deletion)
    erase(deletion)
    rebuilt = SharingHostService(deletion.host._directory, known_actor=deletion.host._known_actor, storage=deletion.access)
    resumed = rebuilt.resume_deletion(SID, source.ALICE, identity_resolver=deletion.resolve)
    assert resumed.descriptor['project_id'] == deletion.setup.source.project_id
    resumed.descriptor['project_id'] = 'forged'
    assert resumed.descriptor['project_id'] != 'forged'
    rebuilt.check_deletion(resumed, for_admission=True)
    resumed = rebuilt.adopt_deletion(resumed, lifecycle.state('session', SID)['operation'])
    rebuilt.check_deletion(resumed)
    rebuilt.commit_deletion(resumed)
    assert rebuilt.confirms_deletion(resumed)
    rebuilt.commit_deletion(resumed)
    assert owner(deletion)['revision'] == 2 and owner(deletion)['source']['epoch'] == 2
    assert owner(deletion)['deletion'] == json.loads(receipt._record_json)
    assert not rebuilt.owner_current(SID, source.ALICE)
    with pytest.raises(SessionSharingDenied):
        rebuilt.check_deletion(resumed)
    with pytest.raises(SessionSharingDenied):
        rebuilt.cleanup_owner_stamp(SID, source.ALICE)


def test_begin_save_after_commit_exception_can_resume_same_nonce(deletion, monkeypatch):
    save = deletion.access._save
    def saved_then_failed(data):
        save(data)
        raise OSError('synthetic post-save failure')
    monkeypatch.setattr(deletion.access, '_save', saved_then_failed)
    with pytest.raises(OSError):
        begin(deletion)
    monkeypatch.setattr(deletion.access, '_save', save)
    receipt = begin(deletion)
    assert json.loads(receipt._record_json)['deletion_id'] == deletion.capture._nonce
    assert owner(deletion)['source']['epoch'] == 2


def test_retire_unknown_save_and_temporary_read_outage_reconcile(deletion, monkeypatch):
    receipt = begin(deletion)
    erase(deletion)
    save, load = deletion.access._save, deletion.access._load
    def saved_then_failed(data):
        save(data)
        raise OSError('synthetic post-save failure')
    monkeypatch.setattr(deletion.access, '_save', saved_then_failed)
    with pytest.raises(OSError):
        deletion.host.commit_deletion(receipt)
    monkeypatch.setattr(deletion.access, '_load', lambda: (_ for _ in ()).throw(OSError('read outage')))
    assert not deletion.host.confirms_deletion(receipt)
    monkeypatch.setattr(deletion.access, '_load', load)
    monkeypatch.setattr(deletion.access, '_save', save)
    assert deletion.host.confirms_deletion(receipt)
    resumed = deletion.host.resume_deletion(SID, source.ALICE, identity_resolver=deletion.resolve)
    deletion.host.commit_deletion(resumed)
    assert owner(deletion)['revision'] == 2


@pytest.mark.parametrize('wrong', [source.BOB, replace(source.ALICE, authority='other'), replace(source.ALICE, subject_id='other')])
def test_retry_full_identity_required(deletion, wrong):
    begin(deletion)
    erase(deletion)
    with pytest.raises(SessionSharingDenied):
        deletion.host.resume_deletion(SID, wrong, identity_resolver=lambda: wrong)


@pytest.mark.parametrize('field', ['identity', 'revision', 'source_epoch', 'deletion_id', 'binding', 'operation', 'project_generation'])
def test_changed_authority_or_binding_cannot_become_new_permission(deletion, field):
    receipt = begin(deletion)
    if field == 'binding':
        path = deletion.setup.path.parent / 'metadata.json'
        value = json.loads(path.read_text())
        value['mode'] = 'changed'
        path.write_text(json.dumps(value))
    elif field == 'operation':
        with lifecycle.resource_lock('session', SID):
            value = lifecycle.state('session', SID)
            value['operation']['operation_id'] = 'replacement'
            lifecycle.save_locked('session', SID, value)
    elif field == 'project_generation':
        lifecycle.begin('project', deletion.setup.source.project_id, 'delete')
    else:
        def mutate(record):
            if field == 'identity':
                record['identity']['subject_id'] = 'changed'
            elif field == 'revision':
                record['revision'] += 1
            elif field == 'source_epoch':
                record['source']['epoch'] += 1
            else:
                record['deletion']['deletion_id'] = 'replacement'
        change(deletion, mutate)
    with pytest.raises(SessionSharingDenied):
        deletion.host.check_deletion(receipt)
    assert not deletion.host.confirms_deletion(receipt)


def test_live_identity_revoked_but_committed_fact_is_not_access(deletion):
    receipt = begin(deletion)
    erase(deletion)
    deletion.host.commit_deletion(receipt)
    deletion.setup.identities[0] = None
    assert deletion.host.confirms_deletion(receipt)
    with pytest.raises(SessionSharingDenied):
        deletion.host.resume_deletion(SID, source.ALICE, identity_resolver=deletion.resolve)


def test_other_concurrent_capture_cannot_reinvalidate_same_owner(deletion):
    other = deletion.host.capture_deletion(SID, source.ALICE, deletion.permit)
    first = begin(deletion)
    with pytest.raises(SessionSharingDenied):
        deletion.host.begin_deletion(other, lifecycle.state('session', SID)['operation'])
    assert owner(deletion)['source']['epoch'] == 2
    deletion.host.check_deletion(first)


def test_explicit_same_operation_takeover_invalidates_old_receipt(deletion):
    op = lifecycle.begin('session', SID, 'delete')
    op = lifecycle.claim_operation('session', SID, 'first-owner')
    receipt = deletion.host.begin_deletion(deletion.capture, op)
    op = lifecycle.claim_operation('session', SID, 'second-owner')
    with pytest.raises(SessionSharingDenied):
        deletion.host.check_deletion(receipt)
    adopted = deletion.host.adopt_deletion(receipt, op)
    deletion.host.check_deletion(adopted)
    assert adopted.generation == receipt.generation + 1
    assert owner(deletion)['source']['epoch'] == 2
    with pytest.raises(SessionSharingDenied):
        deletion.host.check_deletion(receipt)


def test_only_real_delete_permit_can_capture_and_unknown_fields_deny(deletion):
    for method in ('session.stop', 'chat.cancel'):
        value = admit_session_request(method, {'session_id': SID}, host=deletion.host, identity_resolver=deletion.resolve)
        with pytest.raises(SessionSharingDenied):
            deletion.host.capture_deletion(SID, source.ALICE, value)
    with pytest.raises(SessionSharingDenied):
        deletion.host.begin_deletion({}, {})
    receipt = begin(deletion)
    change(deletion, lambda record: record['deletion'].update(untrusted='field'))
    with pytest.raises(SessionSharingDenied):
        deletion.host.check_deletion(receipt)


def test_acl_revoke_does_not_prevent_original_delete_only_cleanup(deletion):
    receipt = begin(deletion)
    deletion.access.replace_acl(deletion.setup.source.project_id, 'admin', acl={}, expected_revision=2)
    deletion.host.check_deletion(receipt)
    assert not deletion.host.owner_current(SID, source.ALICE)
    erase(deletion)
    deletion.host.commit_deletion(receipt)


def test_partial_files_require_original_destructive_phase(deletion):
    receipt = begin(deletion)
    (deletion.setup.path.parent / 'metadata.json').unlink()
    with pytest.raises(SessionSharingDenied):
        deletion.host.check_deletion(receipt)
    lifecycle.update('session', SID, phase='delete_directory')
    deletion.host.check_deletion(receipt)
    with pytest.raises(SessionSharingDenied):
        deletion.host.commit_deletion(receipt)


@pytest.mark.asyncio
async def test_worker_and_other_task_check_same_private_receipt(deletion):
    receipt = begin(deletion)
    await asyncio.to_thread(deletion.host.check_deletion, receipt)
    await asyncio.create_task(asyncio.to_thread(deletion.host.check_deletion, receipt))
    deletion.setup.identities[0] = source.BOB
    with pytest.raises(SessionSharingDenied):
        await asyncio.to_thread(deletion.host.check_deletion, receipt)


def test_retired_receipt_session_id_cannot_be_reused(deletion):
    receipt = begin(deletion)
    erase(deletion)
    deletion.host.commit_deletion(receipt)
    with pytest.raises(SessionSharingConflict):
        deletion.host.register_owner_and_source(SID, source.ALICE, deletion.setup.source.project_id, expected_owner_revision=2)
    with pytest.raises(SessionSharingConflict):
        deletion.host.store.register_owner(SID, source.ALICE, expected_revision=2)


def test_claim_saved_before_receipt_adoption_crash_can_resume_read_only(deletion):
    receipt = begin(deletion)
    lifecycle.claim_operation('session', SID, 'first')
    lifecycle.claim_operation('session', SID, 'second')
    lifecycle.claim_operation('session', SID, 'third')
    resumed = deletion.host.resume_deletion(SID, source.ALICE, identity_resolver=deletion.resolve)
    assert resumed.generation == receipt.generation + 2
    deletion.host.check_deletion(resumed, for_admission=True)
    with pytest.raises(SessionSharingDenied):
        deletion.host.check_deletion(receipt, for_admission=True)
    with pytest.raises(SessionSharingDenied):
        deletion.host.check_deletion(resumed)
    erase(deletion)
    with pytest.raises(SessionSharingDenied):
        deletion.host.commit_deletion(resumed)
    adopted = deletion.host.adopt_deletion(resumed, lifecycle.state('session', SID)['operation'])
    deletion.host.check_deletion(adopted)
    deletion.host.commit_deletion(adopted)
    assert deletion.host.confirms_deletion(adopted)
    deletion.host.check_deletion(adopted, for_admission=True)
    with pytest.raises(SessionSharingDenied):
        deletion.host.check_deletion(adopted)


def test_sidecar_begin_save_failure_leaves_original_epoch(deletion, monkeypatch):
    save = deletion.access._save
    monkeypatch.setattr(deletion.access, '_save', lambda _: (_ for _ in ()).throw(OSError('before save')))
    with pytest.raises(OSError):
        begin(deletion)
    assert owner(deletion)['source']['epoch'] == 1 and 'deletion' not in owner(deletion)
    monkeypatch.setattr(deletion.access, '_save', save)
    receipt = begin(deletion)
    deletion.host.check_deletion(receipt)


@pytest.mark.asyncio
async def test_real_sidecar_parallel_cas_cannot_publish_two_deletion_receipts(deletion):
    import threading
    other = deletion.host.capture_deletion(SID, source.ALICE, deletion.permit)
    op = lifecycle.begin('session', SID, 'delete')
    barrier = threading.Barrier(2)
    def enter(capture):
        barrier.wait(timeout=5)
        try:
            return deletion.host.begin_deletion(capture, op)
        except SessionSharingDenied:
            return None
    results = await asyncio.gather(asyncio.to_thread(enter, deletion.capture), asyncio.to_thread(enter, other))
    assert sum(item is not None for item in results) == 1
    assert owner(deletion)['source']['epoch'] == 2


def test_retired_before_lifecycle_complete_new_owner_claim_only_finishes_fact(deletion):
    op = lifecycle.begin('session', SID, 'delete')
    op = lifecycle.claim_operation('session', SID, 'old-process')
    receipt = deletion.host.begin_deletion(deletion.capture, op)
    erase(deletion)
    deletion.host.commit_deletion(receipt)
    assert lifecycle.state('session', SID)['operation']['status'] != 'completed'
    rebuilt = SharingHostService(deletion.host._directory, known_actor=deletion.host._known_actor, storage=deletion.access)
    resumed = rebuilt.resume_deletion(SID, source.ALICE, identity_resolver=deletion.resolve)
    op = lifecycle.claim_operation('session', SID, 'new-process')
    adopted = rebuilt.adopt_deletion(resumed, op)
    assert rebuilt.confirms_deletion(adopted)
    assert owner(deletion)['revision'] == 2 and owner(deletion)['source']['epoch'] == 2
    with pytest.raises(SessionSharingDenied):
        rebuilt.check_deletion(adopted)
    lifecycle.complete('session', SID, deleted=True, result={'session_id': SID, 'ok': True})
    rebuilt.check_deletion(adopted, for_admission=True)
    assert rebuilt.confirms_deletion(adopted)
    assert not rebuilt.owner_current(SID, source.ALICE)


def test_exact_original_permit_ack_requires_retire_and_lifecycle_commit(deletion):
    receipt = begin(deletion)
    assert not deletion.host.confirm_deletion_for_permit(deletion.permit)
    erase(deletion)
    deletion.host.commit_deletion(receipt)
    assert not deletion.host.confirm_deletion_for_permit(deletion.permit)
    lifecycle.complete('session', SID, deleted=True, result={'ok': True, 'session_id': SID})
    assert deletion.host.confirm_deletion_for_permit(deletion.permit)
    for forged in (replace(deletion.permit, method='session.stop'),
                   replace(deletion.permit, cleanup=(SID, (1, 1, 'wrong', 0, 0, 0, 0, False, False))),
                   replace(deletion.permit, cleanup_params=json.dumps({'session_id': SID, 'mode': 'changed'}))):
        assert not deletion.host.confirm_deletion_for_permit(forged)
    deletion.setup.identities[0] = None
    assert not deletion.host.confirm_deletion_for_permit(deletion.permit)


def test_old_permit_ack_cannot_follow_new_claim_generation(deletion):
    op = lifecycle.begin('session', SID, 'delete')
    op = lifecycle.claim_operation('session', SID, 'first')
    receipt = deletion.host.begin_deletion(deletion.capture, op)
    erase(deletion)
    deletion.host.commit_deletion(receipt)
    op = lifecycle.claim_operation('session', SID, 'second')
    adopted = deletion.host.adopt_deletion(receipt, op)
    assert deletion.host.confirms_deletion(adopted)
    lifecycle.complete('session', SID, deleted=True, result={'ok': True, 'session_id': SID})
    assert not deletion.host.confirm_deletion_for_permit(deletion.permit)
    retry = replace(deletion.permit)
    # The main integration adds this optional private permit field; emulate
    # that field without altering the shared SessionBoundary in this slice.
    fresh = deletion.host.resume_deletion(SID, source.ALICE, identity_resolver=deletion.resolve)
    deletion.host.check_deletion(fresh, for_admission=True)
    object.__setattr__(retry, 'deletion_receipt', fresh)
    assert deletion.host.confirm_deletion_for_permit(retry)
    object.__setattr__(retry, 'deletion_receipt', receipt)
    assert not deletion.host.confirm_deletion_for_permit(retry)
