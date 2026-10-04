"""Actual deletion/store/AgentServer/Gateway; only Provider exit is synthetic."""
import copy

import pytest

from jiuwenswarm.common.e2a.wire_codec import parse_agent_server_wire_unary

from tests.unit_tests.runtime import test_owned_session_delete_delivery as fixtures

delivery, deletion, setup, transaction = fixtures.delivery, fixtures.deletion, fixtures.setup, fixtures.transaction


def corrupt_at_commit(d, monkeypatch):
    host = d.tx.setup.host
    original = host.commit_deletion
    before = []
    def commit(receipt, **kwargs):
        data = host._storage._load()
        before.append(copy.deepcopy(data['sharing_audit']))
        data['sharing_audit'] = {'schema_version': 'broken'}
        host._storage._save(data)
        return original(receipt, **kwargs)
    monkeypatch.setattr(host, 'commit_deletion', commit)
    return before, original


def repair_storage(d, before):
    host = d.tx.setup.host
    data = host._storage._load()
    data['sharing_audit'] = before[0]
    host._storage._save(data)


@pytest.mark.asyncio
async def test_actual_pending_ack_then_same_sid_repairs_only_original_audit(delivery, monkeypatch):
    x = delivery
    before, original = corrupt_at_commit(x.d, monkeypatch)
    await x.dispatch()
    frames = await x.drain()
    assert len(frames) == 2
    assert all(f['payload'] == {'session_id': x.d.sid, 'deleted': True,
                              'exit_confirmed': True, 'audit_pending': True} for f in frames)
    assert parse_agent_server_wire_unary(x.agent_wires[0]).payload['audit_pending'] is True
    host = x.host
    data = host._storage._load()
    pending = copy.deepcopy(data['session_sharing']['owners'][x.d.sid]['deletion']['audit_pending'])
    assert pending['context']['request_id'] == 'delete-wire-1'
    assert before[0]['events'][-1]['context']['attempt_id'] == pending['context']['attempt_id']
    assert not (x.d.tx.root / x.d.sid).exists()
    monkeypatch.setattr(host, 'commit_deletion', original)
    repair_storage(x.d, before)
    await x.dispatch(2)
    frames = await x.drain()
    assert len(frames) == 2 and all(f['payload']['audit_pending'] is False for f in frames)
    events = host._storage._load()['sharing_audit']['events']
    confirmed = [e for e in events if e['facts'].get('action') == 'delete' and e['facts']['phase'] == 'exit_confirmed']
    assert len(confirmed) == 1 and confirmed[0]['context'] == pending['context']
    x.d.release.assert_awaited_once()
    x.d.tx.manager.stop_existing_session_runtime.assert_awaited_once()


@pytest.mark.asyncio
async def test_queued_ack_rederives_repaired_status_from_same_receipt(delivery, monkeypatch):
    x = delivery
    before, _ = corrupt_at_commit(x.d, monkeypatch)
    await x.dispatch()
    repair_storage(x.d, before)
    authority = x.requests[0]._deletion_authority
    x.host.supplement_deletion_audit(authority.receipt)
    frames = await x.drain()
    assert len(frames) == 2 and all(f['payload']['audit_pending'] is False for f in frames)


@pytest.mark.asyncio
async def test_unknown_audit_status_after_actual_delete_cannot_ack(delivery, monkeypatch):
    x = delivery
    original = x.host.deletion_audit_pending
    def unreadable(receipt):
        raise OSError('private storage details')
    monkeypatch.setattr(x.host, 'deletion_audit_pending', unreadable)
    await x.dispatch()
    frames = await x.drain()
    assert frames and all(f.get('ok') is False for f in frames)
    assert all('private storage' not in str(f) for f in frames)
    monkeypatch.setattr(x.host, 'deletion_audit_pending', original)
    await x.dispatch(2)
    frames = await x.drain()
    assert len(frames) == 2 and frames[0]['payload']['audit_pending'] is False
    x.d.release.assert_awaited_once()


@pytest.mark.asyncio
async def test_replaced_pending_receipt_io_error_reconciles_without_hiding_pending(delivery, monkeypatch):
    x = delivery
    corrupt_at_commit(x.d, monkeypatch)
    original = x.host._storage._save
    failed = []
    def save(data):
        original(data)
        owner = data.get('session_sharing', {}).get('owners', {}).get(x.d.sid, {})
        if owner.get('retired') and not failed:
            failed.append(True)
            raise OSError('post-replace synthetic failure')
    monkeypatch.setattr(x.host._storage, '_save', save)
    await x.dispatch()
    frames = await x.drain()
    assert len(frames) == 2 and all(f['payload']['audit_pending'] is True for f in frames)
    assert failed and x.host.confirms_deletion(x.requests[0]._deletion_authority.receipt)


@pytest.mark.asyncio
async def test_pending_retry_before_storage_repair_stays_deleted_without_repeating_exit(delivery, monkeypatch):
    x = delivery
    corrupt_at_commit(x.d, monkeypatch)
    await x.dispatch()
    await x.drain()
    await x.dispatch(2)
    frames = await x.drain()
    assert len(frames) == 2 and all(f['payload']['audit_pending'] is True for f in frames)
    x.d.release.assert_awaited_once()
    x.d.tx.manager.stop_existing_session_runtime.assert_awaited_once()


@pytest.mark.asyncio
async def test_pending_ack_drops_on_queue_identity_change(delivery, monkeypatch):
    from tests.unit_tests.runtime import test_continuation_transaction as txns
    x = delivery
    corrupt_at_commit(x.d, monkeypatch)
    await x.dispatch()
    x.identity[0] = txns.ALICE
    assert await x.drain() == []


@pytest.mark.asyncio
async def test_pending_retirement_with_incomplete_lifecycle_can_retry_without_new_exit(deletion, monkeypatch):
    from jiuwenswarm.server.runtime.session import lifecycle as lc
    d = deletion
    before, original_commit = corrupt_at_commit(d, monkeypatch)
    real_complete = lc.complete
    def fail_complete(*args, **kwargs):
        raise OSError('synthetic lifecycle completion failure')
    monkeypatch.setattr(lc, 'complete', fail_complete)
    with pytest.raises(Exception):
        await d.run()
    monkeypatch.setattr(lc, 'complete', real_complete)
    monkeypatch.setattr(d.tx.setup.host, 'commit_deletion', original_commit)
    repair_storage(d, before)
    # The original receipt retains the completed exit; the retry only completes
    # the same lifecycle and repairs its original persisted observation.
    result = await d.run()
    assert result['deleted'] and result['audit_pending'] is False
    d.release.assert_awaited_once()
