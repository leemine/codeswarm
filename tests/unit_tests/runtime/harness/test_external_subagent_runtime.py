# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""R1-03B4 product composition tests for External subagents."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from openjiuwen.core.session.stream.base import OutputSchema
from openjiuwen.harness.subagent_runtime import SubagentTurnResult
from openjiuwen.harness_protocol import AgentExecutionSpec, ToolInvocation

from jiuwenswarm.common.runtime_workspace import RuntimeWorkspacePaths
from jiuwenswarm.runtime.harness.binding_store import ExecutionBindingStore
from jiuwenswarm.runtime.harness.config_source import ExecutionConfigSource
from jiuwenswarm.runtime.harness.external_subagents import ExternalSubagentRuntime
from jiuwenswarm.runtime.harness.request_binding import AdmittedExecutionRoute


def _route(tmp_path: Path, *, session_id: str = "parent-a") -> AdmittedExecutionRoute:
    root = (tmp_path / session_id).resolve()
    root.mkdir()
    source = ExecutionConfigSource(
        explicit=AgentExecutionSpec("codex", "r1-b4")
    )
    bindings = ExecutionBindingStore()
    bound = bindings.bind(
        source,
        subject_id=f"owner-{session_id}",
        host_session_id=session_id,
        workspace=str(root),
    )
    paths = RuntimeWorkspacePaths(root, root, root, root)
    return AdmittedExecutionRoute("web", source, bindings, bound, paths)


class _Execution:
    def __init__(self, factory: "_Factory", subagent_id: str) -> None:
        self._factory = factory
        self.subagent_id = subagent_id
        self.closed = False

    async def run_turn(self, request, *, on_chunk=None, on_result) -> None:
        self._factory.active += 1
        self._factory.peak = max(self._factory.peak, self._factory.active)
        try:
            if self._factory.gate is not None:
                await self._factory.gate.wait()
            else:
                await asyncio.sleep(0.01)
            if on_chunk is not None:
                await on_chunk(
                    OutputSchema(
                        type="llm_reasoning",
                        index=0,
                        payload={"content": f"reason:{request.query}"},
                    )
                )
                await on_chunk(
                    OutputSchema(
                        type="llm_output",
                        index=1,
                        payload={"content": f"result:{request.query}"},
                    )
                )
            await on_result(SubagentTurnResult(output=f"result:{request.query}"))
        finally:
            self._factory.active -= 1

    async def close(self, reason: str) -> None:
        if self._factory.close_failures:
            self._factory.close_failures -= 1
            raise RuntimeError("child exit unconfirmed")
        self.closed = True
        self._factory.closed.append((self.subagent_id, reason))


class _Factory:
    def __init__(self, *, gate: asyncio.Event | None = None) -> None:
        self.gate = gate
        self.active = 0
        self.peak = 0
        self.created: list[tuple[Any, Any]] = []
        self.closed: list[tuple[str, str]] = []
        self.close_failures = 0

    async def create(self, request, context):
        self.created.append((request, context))
        return _Execution(self, request.subagent_id)

    async def can_restore(self, request, context) -> bool:
        return False


def _install_factory(monkeypatch: pytest.MonkeyPatch, factory: _Factory) -> None:
    from jiuwenswarm.runtime.harness import external_subagents as module

    monkeypatch.setattr(
        module,
        "CodexSubagentExecutionFactory",
        lambda _route: factory,
    )


async def _invoke(runtime: ExternalSubagentRuntime, name: str, arguments: dict):
    return await runtime.gateway.invoke(ToolInvocation(f"call-{name}", name, arguments))


@pytest.mark.asyncio
async def test_six_tools_run_parallel_turns_and_keep_parent_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = _Factory()
    _install_factory(monkeypatch, factory)
    events: list[OutputSchema] = []

    async def write_output(chunk: OutputSchema) -> None:
        events.append(chunk)

    runtime = ExternalSubagentRuntime(_route(tmp_path), write_output=write_output)
    definitions = await runtime.gateway.definitions()
    assert {item.name for item in definitions} == {
        "subagent_spawn",
        "subagent_wait",
        "subagent_list",
        "subagent_send_input",
        "subagent_close",
        "subagent_resume",
    }

    for index in range(2):
        result = await _invoke(
            runtime,
            "subagent_spawn",
            {
                "subagent_type": "explore_agent",
                "task_description": f"inspect-{index}",
                "display_name": f"Explorer {index}",
                "role": "Inspect the delegated scope",
            },
        )
        assert result.is_error is False

    control = runtime._parent_host._subagent_controls["parent-a"]
    subagent_ids = [item.subagent_id for item in control.list_live()]
    waited = await _invoke(
        runtime,
        "subagent_wait",
        {"subagent_ids": subagent_ids, "timeout_ms": 10_000},
    )
    assert waited.is_error is False
    assert "result:inspect-0" in waited.content
    assert "result:inspect-1" in waited.content
    assert factory.peak == 2
    assert all(
        context.parent_session_id == "parent-a"
        and context.parent_subject_id == "owner-parent-a"
        for _request, context in factory.created
    )

    listed = await _invoke(runtime, "subagent_list", {})
    assert listed.is_error is False
    assert all(subagent_id in listed.content for subagent_id in subagent_ids)

    sent = await _invoke(
        runtime,
        "subagent_send_input",
        {"subagent_id": subagent_ids[0], "query": "follow-up"},
    )
    assert sent.is_error is False
    followed = await _invoke(
        runtime,
        "subagent_wait",
        {"subagent_ids": [subagent_ids[0]], "timeout_ms": 10_000},
    )
    assert followed.is_error is False
    assert "result:follow-up" in followed.content

    closed = await _invoke(
        runtime,
        "subagent_close",
        {"subagent_id": subagent_ids[0]},
    )
    assert closed.is_error is False
    # B3 explicitly leaves durable child restore to R1-04; the sixth tool must
    # fail closed instead of rebuilding through another Provider.
    resumed = await _invoke(
        runtime,
        "subagent_resume",
        {"subagent_id": subagent_ids[0]},
    )
    assert resumed.is_error is True

    await asyncio.sleep(0)
    event_types = {event.type for event in events}
    assert "subagent_updated" in event_types
    assert "subagent_activity" in event_types
    assert "subagent_message" in event_types
    await runtime.close("test_complete")


@pytest.mark.asyncio
async def test_cross_parent_control_is_rejected_and_cleanup_cancels_children(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = asyncio.Event()
    factory_a = _Factory(gate=gate)
    _install_factory(monkeypatch, factory_a)
    runtime_a = ExternalSubagentRuntime(
        _route(tmp_path, session_id="parent-a"),
        write_output=lambda _chunk: asyncio.sleep(0),
    )
    spawned = await _invoke(
        runtime_a,
        "subagent_spawn",
        {
            "subagent_type": "general-purpose",
            "task_description": "block",
            "display_name": "Worker A",
            "role": "Wait for cleanup",
        },
    )
    assert spawned.is_error is False
    control_a = runtime_a._parent_host._subagent_controls["parent-a"]
    child_id = control_a.list_live()[0].subagent_id

    factory_b = _Factory()
    _install_factory(monkeypatch, factory_b)
    runtime_b = ExternalSubagentRuntime(
        _route(tmp_path, session_id="parent-b"),
        write_output=lambda _chunk: asyncio.sleep(0),
    )
    forged = await _invoke(
        runtime_b,
        "subagent_send_input",
        {"subagent_id": child_id, "query": "cross-parent"},
    )
    assert forged.is_error is True
    assert factory_b.created == []

    await runtime_a.close("session_deleted")
    assert runtime_a.has_control() is False
    assert factory_a.closed == [(child_id, "session_deleted")]
    await runtime_b.close("test_complete")


@pytest.mark.asyncio
async def test_parent_close_retains_failed_child_and_retries_same_control(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = _Factory()
    factory.close_failures = 1
    _install_factory(monkeypatch, factory)
    runtime = ExternalSubagentRuntime(
        _route(tmp_path, session_id="parent-a"),
        write_output=lambda _chunk: asyncio.sleep(0),
    )
    spawned = await _invoke(
        runtime,
        "subagent_spawn",
        {
            "subagent_type": "general-purpose",
            "task_description": "finish then close",
            "display_name": "Worker A",
            "role": "Verify cleanup",
        },
    )
    assert spawned.is_error is False
    await asyncio.sleep(0.02)

    with pytest.raises(ExceptionGroup, match="exits could not be confirmed"):
        await runtime.close("parent_ended")
    assert runtime.has_control() is True
    assert factory.closed == []

    await runtime.close("retry")
    assert runtime.has_control() is False
    assert len(factory.closed) == 1


@pytest.mark.asyncio
async def test_product_chunks_reuse_history_parser_and_runtime_push(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenswarm.runtime.harness import event_projection as module
    from jiuwenswarm.server.runtime.agent_adapter import (
        subagent_projection,
    )

    parser_calls: list[tuple[Any, str | None]] = []
    pushes: list[dict[str, Any]] = []

    def parse(chunk, *, parent_session_id=None, **_kwargs):
        parser_calls.append((chunk, parent_session_id))
        return {
            "event_type": "chat.subtask_update",
            "parent_session_id": parent_session_id,
            "subagent_id": "child-1",
        }

    async def push(message: dict[str, Any]) -> None:
        pushes.append(message)

    async def direct_history(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    monkeypatch.setattr(subagent_projection, "parse_subagent_stream_chunk", parse)
    monkeypatch.setattr(module, "send_runtime_push", push)
    monkeypatch.setattr(module, "run_history_io", direct_history)
    projection = module.ExternalEventProjection("parent-a")
    chunk = OutputSchema(type="subagent_updated", index=0, payload={})

    await projection.project_product_chunk(chunk)

    assert parser_calls == [(chunk, "parent-a")]
    assert len(pushes) == 1
    assert pushes[0]["payload"]["event_type"] == "chat.subtask_update"
    assert pushes[0]["session_id"] == "parent-a"
