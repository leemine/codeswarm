"""Real Facade history consumption gates the original External Runtime owner."""

import asyncio

import pytest
from openjiuwen.harness_protocol import OutputEvent, OutputKind

from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.runtime.context import reset_runtime_context, set_runtime_context
from jiuwenswarm.runtime.harness import external_goal
from jiuwenswarm.runtime.session.model import SessionWorkKind
from jiuwenswarm.server.runtime.agent_adapter import goal_history, goal_model
from jiuwenswarm.server.runtime.agent_adapter import interface as facade_module
from jiuwenswarm.server.runtime.agent_adapter.interface import JiuWenSwarm
from jiuwenswarm.server.runtime.session import session_history
from tests.unit_tests.runtime.harness.test_external_goal_runtime import (
    chain as external_chain,
    request,
)

chain = external_chain


async def eventually(predicate):
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(0.001)


@pytest.mark.parametrize("outcome", ["complete", "clear", "history_failure"])
async def test_facade_persists_prior_final_before_goal_boundary(
    chain, monkeypatch, tmp_path, outcome,
):
    # Reuse the real Engine/Manager/SerializedTurnHarness/Runtime fixture, but
    # restore its intentionally stubbed history functions for this integration.
    for name in ("record_goal_set", "flush_goal_set", "record_goal_completed"):
        monkeypatch.setattr(external_goal, name, getattr(goal_history, name))
    goal_history.pending_goal_objective_history.clear()
    history_path = tmp_path / "history.jsonl"
    monkeypatch.setattr(session_history, "get_write_history_path", lambda _sid: history_path)
    monkeypatch.setattr(session_history, "get_read_history_path", lambda _sid, **_kw: history_path)
    monkeypatch.setattr(session_history, "use_legacy_history_json", lambda: False)
    monkeypatch.setattr(session_history, "_ensure_jsonl_bootstrap", lambda _sid: history_path)
    monkeypatch.setattr(facade_module, "get_config", lambda: {"preferred_language": "zh"})
    monkeypatch.setattr(facade_module, "get_memory_mode", lambda _config: "off")
    monkeypatch.setattr(facade_module, "build_user_prompt", lambda query, **_kw: query)
    monkeypatch.setattr(goal_model, "request_goal_assessor_factory", lambda _req: chain.adapter._goal_assessor_factory)

    facade = JiuWenSwarm()
    # External create_adapter returns this same Engine class in production.
    facade._adapter = chain.adapter
    facade._sdk_name = "harness"
    history_entered, history_release = asyncio.Event(), asyncio.Event()
    original_append = facade_module._append_request_assistant_history

    async def delayed_real_append(**kwargs):
        if kwargs["request_id"] == "old-chat" and kwargs["event_type"] == "chat.final":
            history_entered.set()
            await history_release.wait()
            if outcome == "history_failure":
                raise OSError("test durable history unavailable")
        await original_append(**kwargs)

    monkeypatch.setattr(facade_module, "_append_request_assistant_history", delayed_real_append)

    async def run(req, kind):
        req._execution_route = chain.adapter._route
        req._bound_execution = chain.adapter._route.bound
        token = set_runtime_context(chain.runtime, None)
        try:
            return [chunk async for chunk in chain.runtime.coordinator.run_stream(
                req.session_id, req.request_id, kind,
                lambda: facade.process_message_stream(req),
            )]
        finally:
            reset_runtime_context(token)

    old = request("old-chat")
    old.req_method = ReqMethod.CHAT_SEND
    old.params = {"query": "ordinary request", "mode": "agent"}
    provider = chain.providers[0]
    execute_turn = provider._execute_turn

    async def execute_with_visible_answer(turn):
        terminal = await execute_turn(turn)
        await provider._emit(
            OutputEvent("answer", OutputKind.TEXT, "visible Provider answer"), turn=turn,
        )
        return terminal

    monkeypatch.setattr(provider, "_execute_turn", execute_with_visible_answer)
    provider.release.clear()
    old_task = asyncio.create_task(run(old, SessionWorkKind.CHAT_STREAM))
    goal_task = None
    try:
        await asyncio.wait_for(provider.entered.wait(), 5)
        goal_task = asyncio.create_task(run(
            request("new-goal", objective="new objective"), SessionWorkKind.GOAL_STREAM,
        ))
        await eventually(lambda: "session-1" in goal_history.pending_goal_objective_history)
        provider.release.set()
        await asyncio.wait_for(history_entered.wait(), 5)
        # Let the real producer prefetch through its terminal while the Facade
        # consumer remains blocked at the durable history boundary.
        for _ in range(20):
            await asyncio.sleep(0)
        goal = chain.adapter._goal_runtime.manager.peek()
        assert goal.attempt_count == 0
        assert len(provider.sent) == 1
        owner_id = chain.runtime.coordinator._sessions["session-1"].external_owner
        owners = chain.runtime.coordinator._registry.select(session_id="session-1")
        assert [owner.request_id for owner in owners if owner.execution_id == owner_id] == ["old-chat"]
        rows = session_history.load_history_records("session-1")
        assert not any(row.get("is_goal_objective_message") for row in rows)

        if outcome == "clear":
            cleared = await asyncio.wait_for(run(
                request("clear-goal", action="clear"), SessionWorkKind.GOAL_CONTROL,
            ), 5)
            assert cleared[0].payload["goal"] is None
            assert chain.adapter._goal_runtime.manager.peek() is None
            assert len(provider.sent) == 1

        history_release.set()
        if outcome == "history_failure":
            with pytest.raises(OSError, match="durable history unavailable"):
                await asyncio.wait_for(old_task, 5)
            assert chain.adapter._goal_runtime.manager.peek().attempt_count == 0
            assert chain.runtime.coordinator._sessions["session-1"].external_owner == owner_id
            assert len(provider.sent) == 1
            assert "session-1" in goal_history.pending_goal_objective_history
            rows = session_history.load_history_records("session-1")
            assert not any(row.get("is_goal_objective_message") for row in rows)
            return
        await asyncio.wait_for(old_task, 5)
        await asyncio.wait_for(goal_task, 5)
        rows = session_history.load_history_records("session-1")
        finals = [i for i, row in enumerate(rows) if row.get("request_id") == "old-chat" and row.get("event_type") == "chat.final"]
        objectives = [i for i, row in enumerate(rows) if row.get("is_goal_objective_message")]
        assert len(finals) == len(objectives) == 1
        assert finals[0] < objectives[0]
        assert rows[objectives[0]]["content"] == "new objective"
        assert "session-1" not in goal_history.pending_goal_objective_history
        assert chain.runtime.coordinator._sessions["session-1"].external_owner is None
        if outcome == "clear":
            assert len(provider.sent) == 1
        else:
            assert chain.adapter._goal_runtime.manager.peek().status.value == "completed"
    finally:
        history_release.set()
        provider.release.set()
        for task in (old_task, goal_task):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(*(task for task in (old_task, goal_task) if task is not None), return_exceptions=True)
        goal_history.pending_goal_objective_history.clear()
