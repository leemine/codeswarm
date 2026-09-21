# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Detached Native output uses product history and push without a second reader."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from openjiuwen.core.session.stream.base import OutputSchema
from openjiuwen.harness_protocol import TurnEventKind
from openjiuwen.harness_providers.io_adapter import ProjectedOutput

from jiuwenswarm.server.runtime.agent_adapter import native_detached_projection as mod
from jiuwenswarm.server.runtime.session import history_io


async def _direct_history(fn, *args, **kwargs):
    return fn(*args, **kwargs)


@pytest.mark.asyncio
async def test_detached_text_persists_before_push_and_finishes_once(monkeypatch):
    operations = []
    history = MagicMock(side_effect=lambda **kwargs: operations.append(("history", kwargs)))

    async def push(message):
        operations.append(("push", message))
        return True

    monkeypatch.setattr(mod, "append_history_record", history)
    monkeypatch.setattr(history_io, "run_history_io", _direct_history)
    monkeypatch.setattr(mod, "send_runtime_push", push)
    monkeypatch.setattr(mod, "build_server_push_message", lambda **kwargs: kwargs)
    monkeypatch.setattr(mod, "get_session_delivery_context", lambda _sid: {"channel_id": "web"})
    monkeypatch.setattr(mod, "get_session_metadata", lambda *_args, **_kwargs: {"mode": "code"})
    parser = MagicMock(
        side_effect=[
            {"event_type": "chat.delta", "content": "Hello"},
            {"event_type": "chat.final", "content": "Hello"},
        ]
    )
    projection = mod.NativeDetachedProjection("s", SimpleNamespace(_parse_stream_chunk=parser))
    await projection(
        ProjectedOutput("turn", chunk=OutputSchema(
            type="llm_output", index=1, payload={"content": "Hello"}
        ))
    )
    await projection(
        ProjectedOutput("turn", chunk=OutputSchema(
            type="answer", index=2, payload={"output": "Hello"}
        ))
    )
    await projection(ProjectedOutput("turn", terminal=TurnEventKind.FINISHED))

    assert [name for name, _ in operations] == ["history", "push", "history", "push"]
    assert [call.kwargs["event_type"] for call in history.call_args_list] == [
        "chat.delta", "chat.final"
    ]
    assert all(call.kwargs["request_id"] == "native-turn-turn" for call in history.call_args_list)
    assert projection._turns == {}


@pytest.mark.asyncio
async def test_detached_terminal_persists_unfinished_text(monkeypatch):
    history = MagicMock()
    monkeypatch.setattr(mod, "append_history_record", history)
    monkeypatch.setattr(history_io, "run_history_io", _direct_history)
    monkeypatch.setattr(mod, "send_runtime_push", AsyncMock(return_value=False))
    monkeypatch.setattr(mod, "build_server_push_message", lambda **kwargs: kwargs)
    monkeypatch.setattr(mod, "get_session_delivery_context", lambda _sid: {})
    monkeypatch.setattr(mod, "get_session_metadata", lambda *_args, **_kwargs: {})
    projection = mod.NativeDetachedProjection(
        "s", SimpleNamespace(_parse_stream_chunk=lambda *_args, **_kwargs: {
            "event_type": "chat.delta", "content": "part"
        })
    )
    await projection(ProjectedOutput("turn", chunk=OutputSchema(
        type="llm_output", index=1, payload={"content": "part"}
    )))
    await projection(ProjectedOutput("turn", terminal=TurnEventKind.FINISHED))
    assert [call.kwargs["event_type"] for call in history.call_args_list] == [
        "chat.delta", "chat.final"
    ]


@pytest.mark.asyncio
async def test_failed_detached_turn_does_not_write_success_final(monkeypatch):
    history = MagicMock()
    monkeypatch.setattr(mod, "append_history_record", history)
    monkeypatch.setattr(history_io, "run_history_io", _direct_history)
    monkeypatch.setattr(mod, "send_runtime_push", AsyncMock(return_value=False))
    monkeypatch.setattr(mod, "build_server_push_message", lambda **kwargs: kwargs)
    monkeypatch.setattr(mod, "get_session_delivery_context", lambda _sid: {})
    monkeypatch.setattr(mod, "get_session_metadata", lambda *_args, **_kwargs: {})
    projection = mod.NativeDetachedProjection(
        "s", SimpleNamespace(_parse_stream_chunk=lambda *_args, **_kwargs: {
            "event_type": "chat.delta", "content": "part"
        })
    )
    await projection(ProjectedOutput("turn", chunk=OutputSchema(
        type="llm_output", index=1, payload={"content": "part"}
    )))
    await projection(ProjectedOutput("turn", terminal=TurnEventKind.FAILED))
    assert [call.kwargs["event_type"] for call in history.call_args_list] == [
        "chat.delta", "chat.error"
    ]


@pytest.mark.asyncio
async def test_detached_goal_completion_uses_existing_card_writer(monkeypatch):
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
        JiuWenSwarmDeepAdapter,
    )

    card_writer = AsyncMock()
    monkeypatch.setattr(
        JiuWenSwarmDeepAdapter,
        "_record_goal_completed_history_if_needed",
        card_writer,
    )
    push = AsyncMock(return_value=True)
    monkeypatch.setattr(mod, "send_runtime_push", push)
    monkeypatch.setattr(mod, "build_server_push_message", lambda **kwargs: kwargs)
    monkeypatch.setattr(
        mod, "get_session_delivery_context",
        lambda _sid: {"channel_id": "web", "route_metadata": {"topic": "goal"}},
    )
    monkeypatch.setattr(mod, "get_session_metadata", lambda *_args, **_kwargs: {"mode": "code"})
    goal = {"goal_id": "g", "status": "completed"}
    projection = mod.NativeDetachedProjection(
        "s", SimpleNamespace(_parse_stream_chunk=lambda *_args, **_kwargs: {
            "event_type": "goal.updated", "goal": goal
        })
    )
    await projection(ProjectedOutput("turn", chunk=OutputSchema(
        type="goal.updated", index=1, payload={"goal": goal}
    )))
    card_writer.assert_awaited_once_with(
        session_id="s", channel_id="web",
        channel_metadata={"topic": "goal"}, mode="code", goal_payload=goal,
    )
    assert push.await_args.args[0]["payload"]["goal"] == goal


@pytest.mark.asyncio
async def test_detached_question_is_runtime_control_target_before_push(monkeypatch):
    from jiuwenswarm.runtime.service import AgentRuntime
    from jiuwenswarm.runtime.session import RuntimeSessionCoordinator

    runtime = object.__new__(AgentRuntime)
    runtime._session_coordinator = RuntimeSessionCoordinator()
    runtime._admission_controller = SimpleNamespace(
        mark_interaction_pending=AsyncMock()
    )
    await runtime._session_coordinator.register_session("s", "web")
    monkeypatch.setattr(history_io, "run_history_io", _direct_history)
    monkeypatch.setattr(mod, "append_history_record", MagicMock())
    monkeypatch.setattr(mod, "get_session_delivery_context", lambda _sid: {"channel_id": "web"})
    monkeypatch.setattr(mod, "get_session_metadata", lambda *_args, **_kwargs: {"mode": "code"})
    monkeypatch.setattr(mod, "build_server_push_message", lambda **kwargs: kwargs)
    pushed = []

    async def push(message):
        assert runtime._session_coordinator.has_control_target("s", "question-id")
        pushed.append(message)
        return True

    monkeypatch.setattr(mod, "send_runtime_push", push)
    question = {
        "event_type": "chat.ask_user_question",
        "request_id": "question-id",
        "questions": [{"question": "Continue?"}],
    }
    projection = mod.NativeDetachedProjection(
        "s", SimpleNamespace(_parse_stream_chunk=lambda *_args, **_kwargs: question),
        runtime=runtime,
    )
    await projection(ProjectedOutput("goal-turn", chunk=OutputSchema(
        type="__interaction__", index=1, payload={"id": "question-id"}
    )))
    assert pushed[0]["payload"] == question
    runtime._admission_controller.mark_interaction_pending.assert_awaited_once_with(
        "s", "question-id"
    )
    await projection(ProjectedOutput("goal-turn", terminal=TurnEventKind.FINISHED))
    assert not runtime._session_coordinator.has_control_target("s", "question-id")
    await projection(ProjectedOutput("next-turn", chunk=OutputSchema(
        type="__interaction__", index=1, payload={"id": "question-id"}
    )))
    assert runtime._session_coordinator.has_control_target("s", "question-id")
    await projection.close()
    assert not runtime._session_coordinator.has_control_target("s", "question-id")
    await runtime._session_coordinator.close()


@pytest.mark.asyncio
async def test_detached_turn_keeps_original_request_id_for_targeted_cancel(monkeypatch):
    from jiuwenswarm.runtime.service import AgentRuntime
    from jiuwenswarm.runtime.session import RuntimeSessionCoordinator

    runtime = object.__new__(AgentRuntime)
    runtime._session_coordinator = RuntimeSessionCoordinator()
    runtime._admission_controller = None
    await runtime._session_coordinator.register_session("s", "web")
    monkeypatch.setattr(mod, "get_session_delivery_context", lambda _sid: {"channel_id": "web"})
    monkeypatch.setattr(mod, "get_session_metadata", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(mod, "build_server_push_message", lambda **kwargs: kwargs)
    monkeypatch.setattr(mod, "send_runtime_push", AsyncMock(return_value=True))
    projection = mod.NativeDetachedProjection(
        "s", SimpleNamespace(_parse_stream_chunk=lambda *_args, **_kwargs: {
            "event_type": "goal.updated", "goal": {"status": "running"}
        }),
        runtime=runtime,
        request_id_for_turn=lambda _turn_id: "original-request",
    )
    await projection(ProjectedOutput("turn", chunk=OutputSchema(
        type="goal.updated", index=1, payload={"goal": {"status": "running"}}
    )))
    active = runtime._session_coordinator._registry.select(
        session_id="s", request_id="original-request", active_only=True
    )
    assert len(active) == 1
    cancelled = await runtime._session_coordinator.cancel_execution(
        "s", request_id="original-request"
    )
    assert cancelled.matched == 1 and cancelled.cancelled == 1
    await projection(ProjectedOutput("turn", terminal=TurnEventKind.ABORTED))
    await runtime._session_coordinator.close()
