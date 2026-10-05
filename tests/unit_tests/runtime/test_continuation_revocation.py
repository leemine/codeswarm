"""Real sidecar/Coordinator/sharing RPC; Provider stop is deterministic synthetic IO."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.runtime.session import SessionWorkKind
from jiuwenswarm.runtime.session.model import RuntimeSessionState
from jiuwenswarm.server.runtime.gateway_adapter.session_sharing_adapter import (
    SessionSharingAdapter,
)
from tests.unit_tests.runtime import test_continuation_transaction as txns

setup, transaction = txns.setup, txns.transaction


@pytest.fixture
async def watched(transaction):
    tx = transaction
    result = await txns.create(tx)
    sid = result.session_id
    tx.manager.get_agent_for_session_nowait = lambda *_: None
    tx.manager.release_subagent_runtime_for_session = AsyncMock()
    tx.manager.stop_existing_session_runtime = AsyncMock()
    tx.manager.cleanup_session_runtime = AsyncMock()
    tx.runtime._forget_agent_execution_owner = AsyncMock()
    started, finished, unblock = asyncio.Event(), asyncio.Event(), asyncio.Event()
    request = AgentRequest(
        request_id="original-active",
        session_id=sid,
        channel_id="web",
        req_method=ReqMethod.CHAT_SEND,
        params={},
    )

    async def consume():
        tx.runtime._resource_authorizers_for(request)
        started.set()
        try:
            await unblock.wait()
        finally:
            finished.set()

    handle = tx.runtime._session_coordinator.submit_unary(
        sid, request.request_id, SessionWorkKind.CHAT_UNARY, consume
    )
    await asyncio.wait_for(started.wait(), 5)
    record = tx.runtime._session_coordinator._sessions[sid]
    adapter = SessionSharingAdapter(
        tx.setup.host.store,
        identity_resolver=lambda _: txns.ALICE,
        target_resolver=tx.setup.host.target_resolver,
        compile_history=tx.setup.host.compile_history,
        after_mutation=tx.runtime._session_coordinator.revalidate_session_authorities,
    )

    async def revoke():
        return await adapter.handle(
            AgentRequest(
                request_id="revoke-original",
                channel_id="web",
                req_method=ReqMethod.SESSION_SHARE_REVOKE,
                params={
                    "session_id": "source-session",
                    "share_id": tx.request.share_id,
                    "expected_revision": 1,
                },
            )
        )

    yield SimpleNamespace(**locals())
    unblock.set()
    await finished.wait()
    if record.authority_task is not None:
        record.authority_task.cancel()
        await asyncio.gather(record.authority_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_actual_sharing_revoke_waits_for_original_active_exit(watched):
    w = watched
    assert (await w.revoke()).ok
    assert w.finished.is_set()
    assert w.record.state is RuntimeSessionState.CLOSED
    assert w.tx.runtime._session_coordinator.get_execution(
        w.handle.execution_id
    ).state.terminal
    w.tx.manager.stop_existing_session_runtime.assert_awaited_once_with(
        channel_id="web", session_id=w.sid
    )
    w.tx.manager.cleanup_session_runtime.assert_awaited_once()
    assert (
        w.tx.root / w.sid
    ).exists()  # Revoke frees execution, never deletes history.
    assert txns.owner(w.tx)["retired"] is False


@pytest.mark.asyncio
async def test_expiry_without_an_rpc_uses_same_original_close(watched):
    w = watched
    w.tx.setup.clock[0] = 201
    await asyncio.wait_for(w.finished.wait(), 3)
    async with asyncio.timeout(3):
        while w.record.state is not RuntimeSessionState.CLOSED:
            await asyncio.sleep(0.01)
    w.tx.manager.stop_existing_session_runtime.assert_awaited_once()


@pytest.mark.asyncio
async def test_unconfirmed_provider_exit_retains_fence_and_retries(watched):
    w = watched
    w.tx.manager.stop_existing_session_runtime.side_effect = RuntimeError(
        "still exiting"
    )
    response = await w.revoke()
    assert not response.ok and response.payload["code"] == "EXIT_UNCONFIRMED"
    assert w.record.state is RuntimeSessionState.QUIESCING
    w.tx.manager.cleanup_session_runtime.assert_not_awaited()
    w.tx.manager.stop_existing_session_runtime.side_effect = None
    await w.tx.runtime._session_coordinator.revalidate_session_authorities()
    assert w.record.state is RuntimeSessionState.CLOSED
    w.tx.manager.cleanup_session_runtime.assert_awaited_once()


@pytest.mark.asyncio
async def test_old_generation_cannot_stop_newer_execution(watched):
    w = watched
    w.unblock.set()
    await w.finished.wait()
    await w.tx.runtime._session_coordinator.close_session(w.sid)
    newer = await w.tx.runtime._session_coordinator.register_session(w.sid, "web")
    assert newer.generation != w.record.generation
    assert (await w.revoke()).ok
    assert (
        w.tx.runtime._session_coordinator.snapshot_session(w.sid).state
        is RuntimeSessionState.READY
    )
    w.tx.manager.stop_existing_session_runtime.assert_not_awaited()


@pytest.mark.asyncio
async def test_disabled_actor_can_only_release_previously_captured_owner(watched):
    w = watched
    w.tx.setup.host._known_actor = lambda _: False
    await w.tx.runtime._session_coordinator.revalidate_session_authorities()
    assert w.record.state is RuntimeSessionState.CLOSED
    w.tx.manager.stop_existing_session_runtime.assert_awaited_once()
    with pytest.raises(PermissionError):
        w.tx.setup.host.cleanup_owner_stamp(w.sid, txns.BOB)


@pytest.mark.asyncio
async def test_live_token_change_does_not_stop_another_connection_binding(watched):
    w = watched
    w.tx.setup.identities[0] = txns.ALICE
    await w.tx.runtime._session_coordinator.revalidate_session_authorities()
    assert w.record.state is RuntimeSessionState.ACTIVE
    w.tx.manager.stop_existing_session_runtime.assert_not_awaited()


@pytest.mark.asyncio
async def test_regrant_does_not_abandon_unconfirmed_original_exit(watched):
    w = watched
    w.tx.manager.stop_existing_session_runtime.side_effect = RuntimeError(
        "still exiting"
    )
    directory = w.tx.setup.host._known_actor
    w.tx.setup.host._known_actor = lambda _: False
    with pytest.raises(RuntimeError, match="still exiting"):
        await w.tx.runtime._session_coordinator.revalidate_session_authorities()
    assert w.record.state is RuntimeSessionState.QUIESCING
    w.tx.setup.host._known_actor = directory
    w.tx.manager.stop_existing_session_runtime.side_effect = None
    await w.tx.runtime._session_coordinator.revalidate_session_authorities()
    assert w.record.state is RuntimeSessionState.CLOSED
    w.tx.manager.cleanup_session_runtime.assert_awaited_once()


@pytest.mark.asyncio
async def test_uncaptured_replacement_is_not_selected_for_cleanup(watched):
    w = watched
    replacement = object()
    w.tx.manager.get_agent_for_session_nowait = lambda *_: replacement
    response = await w.revoke()
    assert not response.ok
    assert w.record.state is RuntimeSessionState.QUIESCING
    w.tx.manager.stop_existing_session_runtime.assert_not_awaited()
    w.tx.manager.get_agent_for_session_nowait = lambda *_: None
    await w.tx.runtime._session_coordinator.revalidate_session_authorities()
    assert w.record.state is RuntimeSessionState.CLOSED
