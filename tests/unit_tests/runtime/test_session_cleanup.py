"""Cleanup uses original authority and waits for actual owned resource release."""
import asyncio
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.governance.session_sharing import SessionSharingDenied
from jiuwenswarm.runtime.session import RuntimeSessionCoordinator, RuntimeSessionState, SessionWorkKind
from tests.unit_tests.runtime import test_continuation_transaction as transactions

setup = transactions.setup
transaction = transactions.transaction


@pytest.mark.asyncio
async def test_resource_release_fences_admission_and_concurrent_closers():
    coordinator = RuntimeSessionCoordinator()
    await coordinator.register_session('target', 'web')
    entered, finish = asyncio.Event(), asyncio.Event()
    async def release():
        entered.set()
        await finish.wait()
    close = asyncio.create_task(coordinator.close_session('target', generation=1, release_resources=release))
    await entered.wait()
    assert (await coordinator.register_session('target', 'web')).generation == 1
    with pytest.raises(RuntimeError, match='not accepting'):
        coordinator.submit_unary('target', 'new', SessionWorkKind.CHAT_UNARY, AsyncMock())
    other = asyncio.create_task(coordinator.close_session('target'))
    await asyncio.sleep(0)
    assert not other.done()
    assert coordinator.snapshot_session('target').state is RuntimeSessionState.QUIESCING
    finish.set()
    assert (await close).generation == (await other).generation == 1
    assert (await coordinator.register_session('target', 'web')).generation == 2
    await coordinator.close()


@pytest.mark.asyncio
async def test_caller_cancel_does_not_abandon_owned_resource_release():
    coordinator = RuntimeSessionCoordinator()
    await coordinator.register_session('target', 'web')
    entered, finish = asyncio.Event(), asyncio.Event()
    async def release():
        entered.set()
        await finish.wait()
    close = asyncio.create_task(coordinator.close_session('target', release_resources=release))
    await entered.wait()
    close.cancel()
    with pytest.raises(asyncio.CancelledError):
        await close
    assert coordinator.snapshot_session('target').state is RuntimeSessionState.QUIESCING
    finish.set()
    await coordinator.close_session('target')
    assert coordinator.snapshot_session('target').state is RuntimeSessionState.CLOSED
    await coordinator.close()


@pytest.mark.asyncio
async def test_release_error_keeps_generation_fenced_until_explicit_release_retry():
    coordinator = RuntimeSessionCoordinator()
    await coordinator.register_session('target', 'web')
    failed = AsyncMock(side_effect=RuntimeError('exit unconfirmed'))
    with pytest.raises(RuntimeError, match='unconfirmed'):
        await coordinator.close_session('target', release_resources=failed)
    assert coordinator.snapshot_session('target').state is RuntimeSessionState.QUIESCING
    with pytest.raises(RuntimeError, match='unconfirmed'):
        await coordinator.close_session('target')
    good = AsyncMock()
    await coordinator.close_session('target', generation=1, release_resources=good)
    good.assert_awaited_once()
    assert coordinator.snapshot_session('target').state is RuntimeSessionState.CLOSED
    await coordinator.close()


@pytest.mark.asyncio
async def test_old_generation_cannot_release_new_resources():
    coordinator = RuntimeSessionCoordinator()
    await coordinator.register_session('target', 'web')
    await coordinator.close_session('target')
    await coordinator.register_session('target', 'web')
    release = AsyncMock()
    result = await coordinator.close_session('target', generation=1, release_resources=release)
    assert not result.existed
    release.assert_not_awaited()
    assert coordinator.snapshot_session('target').state is RuntimeSessionState.READY
    await coordinator.close()


async def cleanup_request(tx):
    result = await transactions.create(tx)
    tx.runtime.begin_detached_native_turn(result.session_id, 'native-turn', 'original-turn')
    tx.manager.stop_existing_session_runtime = AsyncMock()
    tx.manager.release_subagent_runtime_for_session = AsyncMock()
    tx.manager.cleanup_session_runtime = AsyncMock(return_value=True)
    tx.runtime._forget_agent_execution_owner = AsyncMock()
    tx.runtime._clear_pending_interaction = AsyncMock()
    request = AgentRequest(request_id='cancel-original', session_id=result.session_id, channel_id='web',
        req_method=ReqMethod.CHAT_CANCEL, params={'session_id': result.session_id, 'intent': 'cancel'})
    return request


@pytest.mark.asyncio
async def test_revoked_source_still_allows_exact_owner_confirmed_cleanup(transaction):
    tx = transaction
    request = await cleanup_request(tx)
    tx.setup.host.store.revoke(tx.request.share_id, transactions.ALICE, expected_revision=1)
    assert not tx.setup.host.owner_current(request.session_id, transactions.BOB)
    response = await tx.runtime.cancel_request(request)
    assert response.ok and response.payload['exit_confirmed'] is True
    tx.manager.stop_existing_session_runtime.assert_awaited_once_with(channel_id='web', session_id=request.session_id)
    assert tx.runtime._session_coordinator.snapshot_session(request.session_id).state is RuntimeSessionState.CLOSED
    assert not tx.setup.host.owner_current(request.session_id, transactions.BOB)


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['new_turn', 'new_generation', 'identity', 'parameters', 'epoch'])
async def test_late_cleanup_cannot_select_changed_execution_or_authority(transaction, change):
    tx = transaction
    request = await cleanup_request(tx)
    tx.runtime.prepare_session_cleanup(request)
    if change == 'new_turn':
        tx.runtime.begin_detached_native_turn(request.session_id, 'new-native', 'new-turn')
    elif change == 'new_generation':
        await tx.runtime._session_coordinator.close_session(request.session_id)
        await tx.runtime._register_session(channel_id='web', session_id=request.session_id)
    elif change == 'identity':
        tx.setup.identities[0] = transactions.ALICE
    elif change == 'parameters':
        request.params['project_dir'] = '/another-workspace'
    else:
        epoch = tx.setup.host.source_epoch(request.session_id)
        tx.setup.host.invalidate_source(request.session_id, expected_epoch=epoch)
    with pytest.raises(SessionSharingDenied):
        await tx.runtime.cancel_request(request)
    tx.manager.stop_existing_session_runtime.assert_not_awaited()
    tx.manager.cleanup_session_runtime.assert_not_awaited()


@pytest.mark.asyncio
async def test_unconfirmed_provider_exit_does_not_report_success_or_evict(transaction):
    tx = transaction
    request = await cleanup_request(tx)
    tx.manager.stop_existing_session_runtime.side_effect = RuntimeError('owned task still active')
    with pytest.raises(RuntimeError, match='still active'):
        await tx.runtime.cancel_request(request)
    assert tx.runtime._session_coordinator.snapshot_session(request.session_id).state is RuntimeSessionState.QUIESCING
    tx.manager.cleanup_session_runtime.assert_not_awaited()
    tx.runtime._clear_pending_interaction.assert_not_awaited()
    tx.manager.stop_existing_session_runtime.side_effect = None
    assert (await tx.runtime.cancel_request(request)).payload['exit_confirmed'] is True
