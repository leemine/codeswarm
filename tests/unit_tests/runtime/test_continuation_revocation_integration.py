"""Deterministic original-watch cache replacement review; no Provider."""

import pytest
from tests.unit_tests.runtime import test_continuation_revocation as fixtures

setup, transaction, watched = fixtures.setup, fixtures.transaction, fixtures.watched


@pytest.mark.asyncio
async def test_retry_pending_exit_does_not_depend_on_authority_remaining_revoked(
    watched,
):
    from jiuwenswarm.runtime.session.model import RuntimeSessionState

    w = watched
    known = w.tx.setup.host._known_actor
    w.tx.setup.host._known_actor = lambda _: False
    w.tx.manager.stop_existing_session_runtime.side_effect = RuntimeError(
        "still exiting"
    )
    with pytest.raises(RuntimeError, match="still exiting"):
        await w.tx.runtime._session_coordinator.revalidate_session_authorities()
    assert w.record.state is RuntimeSessionState.QUIESCING
    w.tx.setup.host._known_actor = known
    w.tx.manager.stop_existing_session_runtime.side_effect = None
    try:
        await w.tx.runtime._session_coordinator.revalidate_session_authorities()
        assert w.record.state is RuntimeSessionState.CLOSED
    finally:
        await w.tx.runtime._session_coordinator.close_session(
            w.sid,
            generation=w.record.generation,
            release_resources=w.record.authority_watch.release,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("replace_cache", [False, True])
async def test_actual_prepare_binds_manager_owner_then_stops_only_original(
    watched, monkeypatch, replace_cache
):
    from jiuwenswarm.server.runtime.agent_manager import AgentManager

    w = watched

    class CachedOwner:
        active = True

        def has_session_runtime(self, sid):
            return sid == w.sid and self.active

        async def stop_existing_session_runtime(self, sid):
            assert sid == w.sid
            self.active = False

    owner = CachedOwner()
    w.tx.manager.agents = {"web": {"root": owner}}
    w.tx.manager._borrow_agent = lambda value: value
    w.tx.manager.get_agent_for_session_nowait = (
        AgentManager.get_agent_for_session_nowait.__get__(w.tx.manager)
    )
    w.tx.manager.stop_existing_session_runtime = (
        AgentManager.stop_existing_session_runtime.__get__(w.tx.manager)
    )
    from unittest.mock import AsyncMock
    from jiuwenswarm.runtime import request as runtime_request

    monkeypatch.setattr(
        runtime_request,
        "prepare_chat_turn",
        AsyncMock(return_value=(w.sid, None, owner)),
    )
    prepared = await w.tx.runtime._prepare_chat_turn(w.request, "web")
    assert prepared[2] is owner
    assert w.record.authority_watch.owner is owner
    if replace_cache:
        w.tx.manager.agents["web"]["root"] = CachedOwner()
    try:
        response = await w.revoke()
        if replace_cache:
            assert not response.ok
            assert owner.active and w.tx.manager.agents["web"]["root"].active
            w.tx.manager.cleanup_session_runtime.assert_not_awaited()
        else:
            assert not owner.active
            assert response.ok, response.payload
            w.tx.manager.cleanup_session_runtime.assert_awaited_once()
    finally:
        w.tx.manager.agents["web"]["root"] = owner
        await w.tx.runtime._session_coordinator.close_session(
            w.sid,
            generation=w.record.generation,
            release_resources=w.record.authority_watch.release,
        )
