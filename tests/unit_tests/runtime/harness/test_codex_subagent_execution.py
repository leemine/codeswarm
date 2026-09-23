# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""R1-03B3 Codex-to-Codex child execution tests."""

from __future__ import annotations

import asyncio
import dataclasses
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from openjiuwen.core.session.stream.base import OutputSchema
from openjiuwen.harness.execution_subject import (
    ExecutionSubject,
    execution_subject_scope,
)
from openjiuwen.harness.subagent_runtime import (
    ParentExecutionContext,
    SubagentControl,
    SubagentBuildRequest,
    SubagentTurnRequest,
)
from openjiuwen.harness_protocol import AgentExecutionSpec, TurnEventKind
from openjiuwen.harness_providers.io_adapter import ProjectedOutput

from jiuwenswarm.common.runtime_workspace import RuntimeWorkspacePaths
from jiuwenswarm.runtime.harness.binding_store import ExecutionBindingStore
from jiuwenswarm.runtime.harness.codex_subagent import (
    CodexSubagentExecutionFactory,
)
from jiuwenswarm.runtime.harness.config_source import ExecutionConfigSource
from jiuwenswarm.runtime.harness.request_binding import AdmittedExecutionRoute


def _route(tmp_path: Path) -> AdmittedExecutionRoute:
    root = (tmp_path / "project").resolve()
    cwd = root / "task"
    cwd.mkdir(parents=True)
    spec = AgentExecutionSpec(
        "codex",
        "parent-r1",
        provider_config={
            "cwd": str(cwd),
            "inherit_process_env": False,
            "startup_source_roots": [str(root)],
        },
    )
    source = ExecutionConfigSource(explicit=spec)
    bindings = ExecutionBindingStore()
    bound = bindings.bind(
        source,
        subject_id="alice",
        host_session_id="parent-session",
        workspace=str(root),
    )
    paths = RuntimeWorkspacePaths(
        internal_workspace_dir=(tmp_path / "internal").resolve(),
        runtime_workspace_root=root,
        cwd=cwd,
        project_root=root,
    )
    return AdmittedExecutionRoute("web", source, bindings, bound, paths)


def _request(subagent_id: str = "parent-session_sub_explore_deadbeef") -> SubagentBuildRequest:
    return SubagentBuildRequest(
        subagent_id=subagent_id,
        subagent_type="explore_agent",
        display_name="Explorer",
        role="Inspect only the delegated scope",
    )


def _context() -> ParentExecutionContext:
    return ParentExecutionContext(
        parent_session_id="parent-session",
        parent_subject_id="alice",
    )


@pytest.mark.asyncio
async def test_child_restore_uses_its_own_parent_scoped_checkpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    child_recovery = SimpleNamespace(has_checkpoint=lambda: True)
    child_calls: list[tuple[Any, Any, dict[str, Any]]] = []

    def child(binding: Any, paths: Any, **kwargs: Any):
        child_calls.append((binding, paths, kwargs))
        return child_recovery

    route = dataclasses.replace(
        _route(tmp_path),
        recovery=SimpleNamespace(child=child),
    )
    calls = _install_session_builder(monkeypatch)
    factory = CodexSubagentExecutionFactory(route)

    assert await factory.can_restore(_request(), _context()) is True
    execution = await factory.create(_request(), _context())

    assert child_calls[0][2] == {"create_if_missing": False}
    assert child_calls[1][2] == {}
    assert calls[0][0]["recovery"] is child_recovery
    await execution.close("test")


class _FakeSession:
    def __init__(self, binding: Any) -> None:
        self.binding = binding
        self.started_context = None
        self.stopped = False
        self.aborted = False
        self.stop_failures = 0
        self.outputs_by_turn: dict[str, list[ProjectedOutput]] = {}

    async def start(self, context: Any) -> None:
        self.started_context = context

    async def send(self, content: Any, *, immediate: bool = False) -> Any:
        assert immediate is False
        self.sent = content
        return SimpleNamespace(turn_id="turn-1")

    async def outputs(self, turn_id: str):
        for item in self.outputs_by_turn.get(turn_id, []):
            yield item

    async def abort(self, *, immediate: bool = False) -> None:
        assert immediate is True
        self.aborted = True

    async def stop(self) -> None:
        if self.stop_failures:
            self.stop_failures -= 1
            raise RuntimeError("child exit unconfirmed")
        self.stopped = True


def _install_session_builder(
    monkeypatch: pytest.MonkeyPatch,
) -> list[tuple[dict[str, Any], _FakeSession]]:
    from jiuwenswarm.runtime.harness import codex_subagent as module

    calls: list[tuple[dict[str, Any], _FakeSession]] = []

    def build(source: Any, **kwargs: Any) -> _FakeSession:
        bound = kwargs["bindings"].bind(
            source,
            subject_id=kwargs["subject_id"],
            host_session_id=kwargs["host_session_id"],
            workspace=str(kwargs["runtime_paths"].runtime_workspace_root),
        )
        session = _FakeSession(bound.binding)
        calls.append((kwargs, session))
        return session

    monkeypatch.setattr(module, "prepare_execution_session", build)
    return calls


@pytest.mark.asyncio
async def test_child_inherits_exact_parent_provider_paths_and_gets_new_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    route = _route(tmp_path)
    calls = _install_session_builder(monkeypatch)
    factory = CodexSubagentExecutionFactory(route)

    execution = await factory.create(_request(), _context())

    assert len(calls) == 1
    kwargs, session = calls[0]
    child = execution.binding
    parent = route.bound.binding
    assert child is not parent
    assert child.subject_id == "subagent:parent-session_sub_explore_deadbeef"
    assert child.host_session_id == "parent-session_sub_explore_deadbeef"
    assert child.workspace == parent.workspace
    assert (child.provider_id, child.config_revision, child.fingerprint) == (
        parent.provider_id,
        parent.config_revision,
        parent.fingerprint,
    )
    assert kwargs["runtime_paths"] is route.runtime_paths
    assert session.started_context.cwd == str(route.runtime_paths.cwd)
    assert session.started_context.agent_id == child.subject_id
    assert session.started_context.host_session_id == child.host_session_id
    assert session.started_context.metadata["parent_subject_id"] == "alice"
    assert "Role: Inspect only the delegated scope." in session.started_context.system_prompt

    await execution.close("test")
    assert session.stopped is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("context", "message"),
    [
        (
            ParentExecutionContext("other-session", "alice"),
            "parent Session",
        ),
        (
            ParentExecutionContext("parent-session", "mallory"),
            "parent subject",
        ),
    ],
)
async def test_parent_scope_mismatch_is_rejected_before_child_construction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    context: ParentExecutionContext,
    message: str,
) -> None:
    calls = _install_session_builder(monkeypatch)
    factory = CodexSubagentExecutionFactory(_route(tmp_path))

    with pytest.raises(ValueError, match=message):
        await factory.create(_request(), context)

    assert calls == []


@pytest.mark.asyncio
async def test_existing_cross_provider_child_binding_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    route = _route(tmp_path)
    request = _request()
    route.bindings.bind(
        ExecutionConfigSource(
            explicit=AgentExecutionSpec("native", "malicious-child")
        ),
        subject_id=f"subagent:{request.subagent_id}",
        host_session_id=request.subagent_id,
        workspace=route.bound.binding.workspace,
    )
    calls = _install_session_builder(monkeypatch)
    factory = CodexSubagentExecutionFactory(route)

    with pytest.raises(ValueError, match="does not match its binding"):
        await factory.create(request, _context())

    assert calls == []


@pytest.mark.asyncio
async def test_child_identity_from_another_parent_is_rejected_before_construction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install_session_builder(monkeypatch)
    factory = CodexSubagentExecutionFactory(_route(tmp_path))

    with pytest.raises(ValueError, match="does not belong"):
        await factory.create(
            _request("other-parent_sub_explore_deadbeef"),
            _context(),
        )

    assert calls == []


@pytest.mark.asyncio
async def test_turn_projects_chunks_and_settles_from_provider_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install_session_builder(monkeypatch)
    execution = await CodexSubagentExecutionFactory(_route(tmp_path)).create(
        _request(),
        _context(),
    )
    session = calls[0][1]
    chunks = [
        OutputSchema(
            type="llm_reasoning",
            index=0,
            payload={"content": "checking"},
        ),
        OutputSchema(
            type="llm_output",
            index=1,
            payload={"content": "child result"},
        ),
    ]
    session.outputs_by_turn["turn-1"] = [
        *(ProjectedOutput("turn-1", chunk=item) for item in chunks),
        ProjectedOutput("turn-1", terminal=TurnEventKind.FINISHED),
    ]
    observed: list[Any] = []
    results: list[Any] = []

    async def on_chunk(chunk: Any) -> None:
        observed.append(chunk)

    async def on_result(result: Any) -> None:
        results.append(result)

    await execution.run_turn(
        SubagentTurnRequest(task_id="task-1", query="inspect"),
        on_chunk=on_chunk,
        on_result=on_result,
    )

    assert observed == chunks
    assert session.sent.content == "inspect"
    assert session.sent.metadata == {
        "task_id": "task-1",
        "parent_session_id": "parent-session",
        "subagent_id": "parent-session_sub_explore_deadbeef",
    }
    assert len(results) == 1
    assert results[0].output == "child result"
    assert results[0].reasoning == "checking"
    assert results[0].is_error is False


@pytest.mark.asyncio
async def test_failed_terminal_is_a_structured_subagent_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install_session_builder(monkeypatch)
    execution = await CodexSubagentExecutionFactory(_route(tmp_path)).create(
        _request(),
        _context(),
    )
    calls[0][1].outputs_by_turn["turn-1"] = [
        ProjectedOutput("turn-1", terminal=TurnEventKind.FAILED)
    ]
    results: list[Any] = []

    async def on_result(result: Any) -> None:
        results.append(result)

    await execution.run_turn(
        SubagentTurnRequest(task_id="task-1", query="inspect"),
        on_result=on_result,
    )

    assert len(results) == 1
    assert results[0].is_error is True
    assert results[0].error_code == "PROVIDER_TURN_FAILED"
    assert results[0].output == "Codex subagent turn failed"


@pytest.mark.asyncio
async def test_close_releases_only_exact_child_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    route = _route(tmp_path)
    _install_session_builder(monkeypatch)
    factory = CodexSubagentExecutionFactory(route)
    request = _request()
    execution = await factory.create(request, _context())
    child_binding = execution.binding

    await execution.close("done")
    await execution.close("again")
    replacement = route.bindings.bind(
        ExecutionConfigSource(
            explicit=AgentExecutionSpec("native", "replacement")
        ),
        subject_id=child_binding.subject_id,
        host_session_id=child_binding.host_session_id,
        workspace=child_binding.workspace,
    )

    assert replacement.binding.provider_id == "native"
    assert route.bound.binding.provider_id == "codex"


@pytest.mark.asyncio
async def test_close_retains_child_binding_until_provider_exit_is_confirmed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    route = _route(tmp_path)
    calls = _install_session_builder(monkeypatch)
    factory = CodexSubagentExecutionFactory(route)
    request = _request()
    execution = await factory.create(request, _context())
    session = calls[0][1]
    session.stop_failures = 1

    with pytest.raises(RuntimeError, match="child exit unconfirmed"):
        await execution.close("parent_ended")

    assert execution.closed is False
    assert factory._live[request.subagent_id] is execution
    with pytest.raises(ValueError, match="does not match its binding"):
        route.bindings.bind(
            ExecutionConfigSource(
                explicit=AgentExecutionSpec("native", "replacement")
            ),
            subject_id=execution.binding.subject_id,
            host_session_id=execution.binding.host_session_id,
            workspace=execution.binding.workspace,
        )

    await execution.close("retry")
    assert execution.closed is True
    assert request.subagent_id not in factory._live


@pytest.mark.asyncio
async def test_core_runtime_preserves_parent_and_child_execution_identities(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenswarm.runtime.harness import codex_subagent as module

    route = _route(tmp_path)
    sessions: list[_FakeSession] = []

    def build(source: Any, **kwargs: Any) -> _FakeSession:
        bound = kwargs["bindings"].bind(
            source,
            subject_id=kwargs["subject_id"],
            host_session_id=kwargs["host_session_id"],
            workspace=str(kwargs["runtime_paths"].runtime_workspace_root),
        )
        session = _FakeSession(bound.binding)
        session.outputs_by_turn["turn-1"] = [
            ProjectedOutput(
                "turn-1",
                chunk=OutputSchema(
                    type="llm_output",
                    index=0,
                    payload={"content": "runtime child result"},
                ),
            ),
            ProjectedOutput("turn-1", terminal=TurnEventKind.FINISHED),
        ]
        sessions.append(session)
        return session

    monkeypatch.setattr(module, "prepare_execution_session", build)
    factory = CodexSubagentExecutionFactory(route)
    parent_agent = SimpleNamespace(
        deep_config=SimpleNamespace(workspace=str(route.runtime_paths.cwd))
    )
    control = SubagentControl(
        parent_agent,
        "parent-session",
        execution_factory=factory,
    )
    parent_subject = ExecutionSubject(
        subject_id="alice",
        display_name="Alice parent",
        kind="agent",
        session_id="parent-session",
    )

    with execution_subject_scope(parent_subject):
        spawned = await control.spawn("explore_agent", "inspect")
    waited = await control.wait([spawned.subagent_id], timeout_ms=2_000)
    instance = control._manager.get(spawned.subagent_id)

    assert waited.results == {spawned.subagent_id: "runtime child result"}
    assert instance.execution_subject.subject_id == f"subagent:{spawned.subagent_id}"
    assert instance.execution_subject.parent_subject_id == "alice"
    assert sessions[0].binding.subject_id == instance.execution_subject.subject_id
    assert sessions[0].binding.host_session_id == instance.execution_subject.session_id

    await control.cancel_all("test_complete")
    assert sessions[0].stopped is True


@pytest.mark.asyncio
async def test_cancelled_turn_aborts_only_the_child_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install_session_builder(monkeypatch)
    execution = await CodexSubagentExecutionFactory(_route(tmp_path)).create(
        _request(),
        _context(),
    )
    session = calls[0][1]

    async def blocked_outputs(_turn_id: str):
        await asyncio.Event().wait()
        yield  # pragma: no cover

    session.outputs = blocked_outputs
    task = asyncio.create_task(
        execution.run_turn(
            SubagentTurnRequest(task_id="task-1", query="inspect"),
            on_result=lambda _result: asyncio.sleep(0),
        )
    )
    await asyncio.sleep(0)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert session.aborted is True


def test_non_codex_parent_is_rejected(tmp_path: Path) -> None:
    route = _route(tmp_path)
    native = AgentExecutionSpec("native", "r1")
    source = ExecutionConfigSource(explicit=native)
    bindings = ExecutionBindingStore()
    bound = bindings.bind(
        source,
        subject_id="alice",
        host_session_id="native-parent",
        workspace=route.bound.binding.workspace,
    )
    native_route = dataclasses.replace(
        route,
        source=source,
        bindings=bindings,
        bound=bound,
    )

    with pytest.raises(ValueError, match="Codex parent"):
        CodexSubagentExecutionFactory(native_route)
