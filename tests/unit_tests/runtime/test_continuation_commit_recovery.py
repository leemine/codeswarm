"""Real sidecar save + lost acknowledgment; no Provider or network."""
import copy
import asyncio
import json

import pytest

from jiuwenswarm.runtime.service import RuntimeStateError
from jiuwenswarm.runtime.session_provisioner import SessionProvisionStateError
from tests.unit_tests.runtime import test_continuation_transaction as cases

setup = cases.setup
transaction = cases.transaction


def install_lost_ack(tx, monkeypatch, *, outage):
    save, load = tx.setup.access._save, tx.setup.access._load
    state = {'fired': False, 'outage': False}
    def ambiguous_save(data):
        save(data)
        if not state['fired'] and any(r.get('continuation', {}).get('state') == 'committed'
                for r in data.get('session_sharing', {}).get('owners', {}).values()):
            state.update(fired=True, outage=outage)
            raise OSError('synthetic acknowledgement lost after completed sidecar replace')
    def temporary_load():
        if state['outage']:
            raise OSError('synthetic temporary sidecar read outage')
        return load()
    monkeypatch.setattr(tx.setup.access, '_save', ambiguous_save)
    monkeypatch.setattr(tx.setup.access, '_load', temporary_load)
    return state, save, load


@pytest.mark.asyncio
@pytest.mark.parametrize('outage', [False, True])
async def test_saved_commit_retry_reconciles_original_receipt_then_closes(transaction, monkeypatch, outage):
    tx = transaction
    state, _, load = install_lost_ack(tx, monkeypatch, outage=outage)
    with pytest.raises(OSError, match='acknowledgement lost'):
        await cases.create(tx)
    receipt = tx.receipts[-1]
    assert load()['session_sharing']['owners'][tx.allocated[0]]['continuation']['state'] == 'committed'
    if outage:
        assert receipt.state.value == 'aborting'
        assert receipt in tx.runtime._pending_session_provisions
        with pytest.raises(RuntimeStateError, match='unfinished'):
            await tx.runtime.close()
        state['outage'] = False
    else:
        assert receipt.state.value == 'committed'
    retried = await cases.create(tx)
    retried.revalidate()
    assert retried.session_id == tx.allocated[0]
    assert tx.allocated == [retried.session_id]
    assert receipt.state.value == 'committed'
    assert not tx.runtime._pending_session_provisions
    assert tx.runtime._owns_session(retried.session_id)
    await tx.runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('field', ['publication_id', 'owner_revision', 'source_epoch', 'proof', 'fingerprint'])
async def test_original_receipt_never_settles_changed_disk_fact(transaction, monkeypatch, field):
    tx = transaction
    state, save, load = install_lost_ack(tx, monkeypatch, outage=True)
    with pytest.raises(OSError, match='acknowledgement lost'):
        await cases.create(tx)
    receipt = tx.receipts[-1]
    state['outage'] = False
    original = load()
    changed = copy.deepcopy(original)
    record = changed['session_sharing']['owners'][tx.allocated[0]]
    if field == 'publication_id':
        record['continuation']['publication_id'] = 'f' * 64
    elif field == 'owner_revision':
        record['revision'] += 1
    elif field == 'source_epoch':
        record['source']['epoch'] += 1
    elif field == 'proof':
        record['continuation']['proof']['request']['title'] = 'different original request'
    else:
        record['continuation']['config_fingerprint'] = 'f' * 64
    save(changed)
    try:
        assert not await tx.runtime._session_provisioner.reconcile_committed_provision(receipt)
        assert receipt.state.value == 'aborting'
        assert receipt in tx.runtime._pending_session_provisions
        with pytest.raises(RuntimeStateError, match='unfinished'):
            await tx.runtime.close()
    finally:
        # Restore this test's corruption only, then prove the original receipt
        # still recovers. This is not production recovery of a newer epoch.
        save(original)
    await cases.create(tx)
    await tx.runtime.close()


@pytest.mark.asyncio
async def test_receipt_reconciliation_requires_owning_provisioner(transaction, monkeypatch):
    tx = transaction
    state, _, _ = install_lost_ack(tx, monkeypatch, outage=True)
    with pytest.raises(OSError, match='acknowledgement lost'):
        await cases.create(tx)
    receipt = tx.receipts[-1]
    state['outage'] = False
    with pytest.raises(SessionProvisionStateError):
        await receipt.reconcile_commit_for_owner(object())
    assert receipt.state.value == 'aborting'
    await cases.create(tx)
    await tx.runtime.close()


@pytest.mark.asyncio
async def test_source_revoke_blocks_delivery_but_not_exact_commit_reconciliation_on_close(transaction, monkeypatch):
    tx = transaction
    state, _, _ = install_lost_ack(tx, monkeypatch, outage=True)
    with pytest.raises(OSError, match='acknowledgement lost'):
        await cases.create(tx)
    receipt = tx.receipts[-1]
    state['outage'] = False
    tx.setup.host.store.revoke(tx.request.share_id, cases.ALICE, expected_revision=tx.request.expected_revision)
    with pytest.raises(PermissionError):
        await cases.create(tx)
    await tx.runtime.close()
    assert receipt.state.value == 'committed'
    assert not tx.runtime._pending_session_provisions
    assert not tx.setup.host.owner_current(tx.allocated[0], cases.BOB)


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['seed', 'metadata'])
async def test_commit_probe_checks_files_before_settling(transaction, monkeypatch, kind):
    tx = transaction
    state, _, _ = install_lost_ack(tx, monkeypatch, outage=True)
    with pytest.raises(OSError, match='acknowledgement lost'):
        await cases.create(tx)
    receipt = tx.receipts[-1]
    state['outage'] = False
    path = tx.root / tx.allocated[0] / ('continuation.json' if kind == 'seed' else 'metadata.json')
    original = path.read_bytes()
    data = json.loads(original)
    if kind == 'seed':
        data['messages'][0]['content'] = 'different frozen text'
    else:
        data['model'] = 'different-model#0'
    path.write_text(json.dumps(data))
    try:
        with pytest.raises(PermissionError):
            await tx.runtime._session_provisioner.reconcile_committed_provision(receipt)
        assert receipt.state.value == 'aborting'
        with pytest.raises(RuntimeStateError, match='unfinished'):
            await tx.runtime.close()
    finally:
        path.write_bytes(original)
    await cases.create(tx)
    await tx.runtime.close()


@pytest.mark.asyncio
async def test_close_does_not_wait_for_active_receipt_lock(transaction, monkeypatch):
    tx = transaction
    state, _, _ = install_lost_ack(tx, monkeypatch, outage=True)
    with pytest.raises(OSError, match='acknowledgement lost'):
        await cases.create(tx)
    receipt = tx.receipts[-1]
    state['outage'] = False
    async with receipt._finalize_lock:
        with pytest.raises(RuntimeStateError, match='unfinished'):
            await asyncio.wait_for(tx.runtime.close(), timeout=0.5)
        assert receipt.state.value == 'aborting'
    await cases.create(tx)
    await tx.runtime.close()
