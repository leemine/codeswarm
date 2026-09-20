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
