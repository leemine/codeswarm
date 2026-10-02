# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""R1-03B4 product composition tests for External subagents."""

from __future__ import annotations

import asyncio
import copy
import dataclasses
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from openjiuwen.core.session.stream.base import OutputSchema
from openjiuwen.core.single_agent.schema.agent_result import Artifact, Part
from openjiuwen.harness.subagent_runtime import SubagentTurnResult
from openjiuwen.harness_protocol import (
    AgentExecutionSpec,
    ProviderEvent,
    ToolInvocation,
)

from jiuwenswarm.common.runtime_workspace import RuntimeWorkspacePaths
from jiuwenswarm.runtime.harness.binding_store import ExecutionBindingStore
from jiuwenswarm.runtime.harness.config_source import ExecutionConfigSource
from jiuwenswarm.runtime.harness.external_subagents import (
    ExternalSubagentParentSession,
    ExternalSubagentRuntime,
)
from jiuwenswarm.runtime.harness.request_binding import AdmittedExecutionRoute


def _route(
    tmp_path: Path,
    *,
    session_id: str = "parent-a",
    provider_id: str = "codex",
) -> AdmittedExecutionRoute:
    root = (tmp_path / session_id).resolve()
    root.mkdir()
    source = ExecutionConfigSource(explicit=AgentExecutionSpec(provider_id, "r1-b4"))
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
    def __init__(
        self,
        *,
        gate: asyncio.Event | None = None,
        restorable: bool = False,
    ) -> None:
        self.gate = gate
        self.active = 0
        self.peak = 0
        self.created: list[tuple[Any, Any]] = []
        self.closed: list[tuple[str, str]] = []
        self.close_failures = 0
        self.restorable = restorable

    async def create(self, request, context):
        self.created.append((request, context))
        return _Execution(self, request.subagent_id)

    async def can_restore(self, request, context) -> bool:
        return self.restorable


def _install_factory(monkeypatch: pytest.MonkeyPatch, factory: _Factory) -> None:
    from jiuwenswarm.runtime.harness import external_subagents as module

    monkeypatch.setattr(
        module,
        "ExternalSubagentExecutionFactory",
        lambda _route, **_kwargs: factory,
    )


async def _invoke(runtime: ExternalSubagentRuntime, name: str, arguments: dict):
    return await runtime.gateway.invoke(ToolInvocation(f"call-{name}", name, arguments))


@pytest.mark.asyncio
async def test_code_surface_mounts_code_profiles_and_rejects_work_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.unit_tests.runtime.harness.test_external_subagent_execution import (
        _surface_route,
    )

    factory = _Factory()
    _install_factory(monkeypatch, factory)

    async def write_output(_chunk: OutputSchema) -> None:
        return None

    route = _surface_route(tmp_path)
    persisted_catalog = route.surface.capability_catalog
    runtime = ExternalSubagentRuntime(route, write_output=write_output)
    assert runtime.surface.capability_catalog is not None
    # Recovery input may carry an older audit record, but each provider cycle
    # rebuilds the effective catalog from the admitted spec and mounted tools.
    assert runtime.surface.capability_catalog is not persisted_catalog
    assert (
        runtime.surface.capability_catalog.fingerprint != persisted_catalog.fingerprint
    )
    subagents = {
        entry.name
        for entry in runtime.surface.capability_catalog.entries
        if entry.kind.value == "subagent"
    }
    assert "code_agent" in subagents
    assert "research_agent" not in subagents

    rejected = await _invoke(
        runtime,
        "subagent_spawn",
        {
            "subagent_type": "research_agent",
            "task_description": "must not run",
            "display_name": "Researcher",
            "role": "research",
        },
    )
    assert rejected.is_error is True
    assert factory.created == []

    admitted = await _invoke(
        runtime,
        "subagent_spawn",
        {
            "subagent_type": "code_agent",
            "task_description": "implement",
            "display_name": "Coder",
            "role": "code",
        },
    )
    assert admitted.is_error is False
    await runtime.close("test_complete")


def test_parent_subagent_state_is_restored_and_checkpointed() -> None:
    saved: list[dict[str, Any]] = []
    recovery = type(
        "Recovery",
        (),
        {
            "load_host_state": lambda self: {"subagents": {"revision": 3}},
            "save_host_state": lambda self, state: saved.append(dict(state)),
        },
    )()

    async def write_output(_chunk: OutputSchema) -> None:
        return None

    session = ExternalSubagentParentSession(
        "parent-a",
        write_output=write_output,
        recovery=recovery,
    )
    assert session.get_state("subagents") == {"revision": 3}
    session.update_state({"subagents": {"revision": 4}})
    assert saved == [{"subagents": {"revision": 4}}]


@pytest.mark.asyncio
async def test_public_factory_restores_pre_migration_codex_child_archive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Recovery:
        def __init__(self) -> None:
            self.state: dict[str, Any] = {}

        def load_host_state(self) -> dict[str, Any]:
            return copy.deepcopy(self.state)

        def save_host_state(self, state: dict[str, Any]) -> None:
            self.state = copy.deepcopy(state)

    recovery = Recovery()
    route = dataclasses.replace(_route(tmp_path), recovery=recovery)
    first_factory = _Factory()
    _install_factory(monkeypatch, first_factory)

    async def write_output(_chunk: OutputSchema) -> None:
        return None

    first = ExternalSubagentRuntime(route, write_output=write_output)
    spawned = await _invoke(
        first,
        "subagent_spawn",
        {
            "subagent_type": "general-purpose",
            "task_description": "first task",
            "display_name": "Worker",
            "role": "worker",
        },
    )
    control = first._parent_host._subagent_controls["parent-a"]
    child_id = control.list_live()[0].subagent_id
    assert spawned.is_error is False
    await _invoke(first, "subagent_wait", {"subagent_ids": [child_id]})
    closed = await _invoke(first, "subagent_close", {"subagent_id": child_id})
    assert closed.is_error is False
    await first.close("process_exit")
    serialized = repr(recovery.state)
    assert "CodexSubagentExecution" not in serialized
    assert "codex_subagent" not in serialized

    second_factory = _Factory(restorable=True)
    _install_factory(monkeypatch, second_factory)
    second = ExternalSubagentRuntime(route, write_output=write_output)
    resumed = await _invoke(second, "subagent_resume", {"subagent_id": child_id})
    assert resumed.is_error is False
    assert (
        child_id
        in second._parent_host._subagent_controls["parent-a"]._manager.list_ids()
    )
    await second.close("test_complete")


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_id", ["codex", "opencode"])
async def test_six_tools_run_parallel_turns_and_keep_parent_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider_id: str,
) -> None:
    factory = _Factory()
    _install_factory(monkeypatch, factory)
    events: list[OutputSchema] = []

    async def write_output(chunk: OutputSchema) -> None:
        events.append(chunk)

    runtime = ExternalSubagentRuntime(
        _route(tmp_path, provider_id=provider_id),
        write_output=write_output,
    )
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
    # This fake factory has no durable checkpoint, so resume must fail closed.
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
async def test_parent_close_denies_browser_admission_before_child_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class Admission:
        def __call__(self, _identity, _invocation) -> bool:
            return True

        async def close(self) -> None:
            events.append("admission")

    factory = _Factory()
    _install_factory(monkeypatch, factory)
    runtime = ExternalSubagentRuntime(
        _route(tmp_path),
        write_output=lambda _chunk: asyncio.sleep(0),
        browser_admit=Admission(),
    )
    original_release = runtime._parent_host

    await runtime.close("parent_ended")

    assert original_release is runtime._parent_host
    assert events == ["admission"]


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


@pytest.mark.asyncio
async def test_codex_internal_subagents_reuse_read_only_product_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenswarm.runtime.harness import event_projection as module

    chunks: list[OutputSchema] = []
    projection = module.ExternalEventProjection("parent-a")

    async def project(chunk: OutputSchema) -> None:
        chunks.append(chunk)

    monkeypatch.setattr(projection, "project_product_chunk", project)
    await projection.observe(
        SimpleNamespace(
            turn_id="turn-1",
            event=ProviderEvent(
                provider="codex",
                event_type="internal_subagent/status",
                schema_version="1",
                payload={
                    "subagent_id": "native-child",
                    "status": "running",
                    "prompt": "Inspect the repository",
                    "controllable": False,
                },
            ),
        )
    )
    await projection.observe(
        SimpleNamespace(
            turn_id="turn-1",
            event=ProviderEvent(
                provider="codex",
                event_type="internal_subagent/activity",
                schema_version="1",
                payload={
                    "subagent_id": "native-child",
                    "activity_id": "activity-1",
                    "activity_kind": "interacted",
                    "controllable": False,
                },
            ),
        )
    )

    roster = chunks[0].payload["subagent_updated"]
    assert chunks[0].type == "subagent_updated"
    assert roster["subagent_id"] == "codex:native-child"
    assert roster["subagent_type"] == "codex_internal"
    assert roster["role"] == "Codex internal subagent"
    assert roster["task_description"] == "Inspect the repository"
    assert roster["can_send_input"] is False
    assert roster["needs_resume"] is False
    assert roster["controllable"] is False
    activity = chunks[1].payload["subagent_activity"]
    assert chunks[1].type == "subagent_activity"
    assert activity["subagent_id"] == "codex:native-child"
    assert activity["kind"] == "thinking"
    assert activity["summary"] == "Codex internal agent interacted"


@pytest.mark.asyncio
async def test_product_interaction_reuses_durable_projection_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenswarm.runtime.harness import event_projection as module

    observed: list[tuple[dict[str, Any], str]] = []
    projection = module.ExternalEventProjection("parent-a")

    async def publish(_state, payload, *, delivery_id):
        observed.append((payload, delivery_id))

    monkeypatch.setattr(projection, "_publish", publish)
    payload = {
        "event_type": "chat.ask_user_question",
        "request_id": "browser-permission-1",
        "questions": [{"question": "Allow?", "options": []}],
    }

    await projection.publish_product_interaction(payload, "browser:permission:1")

    assert observed == [(payload, "browser:permission:1")]
    with pytest.raises(ValueError, match="request_id"):
        await projection.publish_product_interaction(
            {"event_type": "chat.ask_user_question", "questions": []},
            "browser:permission:2",
        )


@pytest.mark.asyncio
async def test_product_artifact_reuses_existing_file_delivery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenswarm.agents.harness.common.tools.send_file_to_user import (
        SendFileToolkit,
    )
    from jiuwenswarm.runtime.harness import event_projection as module

    workspace = (tmp_path / "workspace").resolve()
    file_path = workspace / "outputs" / "browser" / "result.pdf"
    file_path.parent.mkdir(parents=True)
    file_path.write_bytes(b"pdf")
    relative_path = file_path.relative_to(workspace).as_posix()
    artifact = Artifact(
        artifactId="artifact-1",
        name=file_path.name,
        parts=[Part(url=relative_path, filename=file_path.name)],
        metadata={"workspace_relative_path": relative_path},
    )
    delivered: list[tuple[str, str, str, Path, dict[str, Any]]] = []

    async def deliver(toolkit, path, metadata) -> None:
        delivered.append(
            (
                toolkit.routing_request_id,
                toolkit.session_id,
                toolkit.channel_id,
                path,
                metadata,
            )
        )

    monkeypatch.setattr(SendFileToolkit, "deliver_projected_artifact", deliver)
    monkeypatch.setattr(
        module,
        "get_session_delivery_context",
        lambda _session_id: {"route_metadata": {"route": "web"}},
    )
    monkeypatch.setattr(
        module,
        "get_session_metadata",
        lambda _session_id, *, enable_writeback: {"user_id": "alice"},
    )
    projection = module.ExternalEventProjection(
        "parent-a",
        workspace_root=workspace,
    )
    projection.register_turn(
        "turn-1",
        request_id="request-1",
        channel_id="web",
        mode="task",
    )

    await projection.publish_product_artifact(artifact, file_path)

    assert delivered == [
        (
            "request-1",
            "parent-a",
            "web",
            file_path,
            artifact.model_dump(mode="json", exclude_none=True),
        )
    ]


@pytest.mark.asyncio
async def test_product_artifact_rejects_mismatched_file_identity(
    tmp_path: Path,
) -> None:
    workspace = (tmp_path / "workspace").resolve()
    file_path = workspace / "outputs" / "result.pdf"
    file_path.parent.mkdir(parents=True)
    file_path.write_bytes(b"pdf")
    artifact = Artifact(
        artifactId="artifact-1",
        parts=[Part(url="outputs/other.pdf")],
        metadata={"workspace_relative_path": "outputs/other.pdf"},
    )
    from jiuwenswarm.runtime.harness.event_projection import (
        ExternalEventProjection,
    )

    projection = ExternalEventProjection("parent-a", workspace_root=workspace)

    with pytest.raises(ValueError, match="identity does not match"):
        await projection.publish_product_artifact(artifact, file_path)
