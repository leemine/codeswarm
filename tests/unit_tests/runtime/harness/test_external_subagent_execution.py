# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Same-engine External child execution tests."""

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
from openjiuwen.harness_protocol import (
    AgentExecutionSpec,
    ExecutionAuthorization,
    ProviderCapability,
    ProviderCapabilityInventory,
    ProviderCapabilityKind,
    ToolInvocation,
    TurnEventKind,
)
from openjiuwen.harness_providers.io_adapter import ProjectedOutput

from jiuwenswarm.common.runtime_workspace import RuntimeWorkspacePaths
from jiuwenswarm.runtime.harness.binding_store import ExecutionBindingStore
from jiuwenswarm.runtime.harness.capability_catalog import compile_capability_catalog
from jiuwenswarm.runtime.harness.external_subagent import (
    ExternalSubagentExecutionFactory,
)
from jiuwenswarm.runtime.harness.config_source import ExecutionConfigSource
from jiuwenswarm.runtime.harness.request_binding import AdmittedExecutionRoute
from jiuwenswarm.runtime.harness.surface import (
    EffectiveSurfaceSnapshot,
    build_surface_identity,
    compile_surface_policy,
)


def _route(
    tmp_path: Path,
    *,
    provider_id: str = "codex",
) -> AdmittedExecutionRoute:
    root = (tmp_path / "project").resolve()
    cwd = root / "task"
    cwd.mkdir(parents=True)
    spec = AgentExecutionSpec(
        provider_id,
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


def _request(
    subagent_id: str = "parent-session_sub_explore_deadbeef",
) -> SubagentBuildRequest:
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


def _surface_route(
    tmp_path: Path, *, work_mode: str = "code"
) -> AdmittedExecutionRoute:
    route = _route(tmp_path)
    metadata = {
        "session_id": "parent-session",
        "channel_id": "web",
        "user_id": "alice",
        "mode": f"agent.{work_mode}.normal",
        "work_mode": work_mode,
        "project_dir": str(route.runtime_paths.project_root),
        "execution_profile_id": "profile",
    }
    identity = build_surface_identity(
        metadata=metadata,
        binding=route.bound.binding,
        paths=route.runtime_paths,
        channel_id="web",
    )
    surface = compile_surface_policy(
        EffectiveSurfaceSnapshot(identity, metadata["mode"]),
        authorization=ExecutionAuthorization(),
        include_personal_context=False,
    )
    catalog = compile_capability_catalog(
        surface,
        provider_inventory=ProviderCapabilityInventory(
            "codex",
            (
                ProviderCapability(
                    "filesystem", ProviderCapabilityKind.CATEGORY
                ),
            ),
        ),
        product_tool_names=("subagent_spawn",),
        product_subagent_types=(
            "research_agent" if work_mode == "work" else "explore_agent",
        ),
        authorization=ExecutionAuthorization(),
    )
    return dataclasses.replace(
        route,
        surface=dataclasses.replace(surface, capability_catalog=catalog),
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
    factory = ExternalSubagentExecutionFactory(route)

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
    from jiuwenswarm.runtime.harness import external_subagent as module

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
@pytest.mark.parametrize("provider_id", ["codex", "opencode"])
async def test_child_inherits_exact_parent_provider_paths_and_gets_new_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider_id: str,
) -> None:
    route = _route(tmp_path, provider_id=provider_id)
    calls = _install_session_builder(monkeypatch)
    factory = ExternalSubagentExecutionFactory(route)

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
    # Parent Heartbeat tools never propagate to a delegated execution.
    assert kwargs.get("tool_gateway") is None
    assert session.started_context.cwd == str(route.runtime_paths.cwd)
    assert session.started_context.agent_id == child.subject_id
    assert session.started_context.host_session_id == child.host_session_id
    assert session.started_context.metadata["parent_subject_id"] == "alice"
    assert (
        "Role: Inspect only the delegated scope."
        in session.started_context.system_prompt
    )

    await execution.close("test")
    assert session.stopped is True


@pytest.mark.asyncio
async def test_child_inherits_parent_surface_capability_catalog(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    route = _surface_route(tmp_path)
    calls = _install_session_builder(monkeypatch)
    factory = ExternalSubagentExecutionFactory(
        route,
        allowed_subagent_types=("explore_agent",),
    )

    execution = await factory.create(_request(), _context())

    context = calls[0][1].started_context
    assert (
        context.metadata["capability_catalog_fingerprint"]
        == route.surface.capability_catalog.fingerprint
    )
    assert context.metadata["surface"]["work_mode"] == "code"
    await execution.close("test")


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
    factory = ExternalSubagentExecutionFactory(_route(tmp_path))

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
        ExecutionConfigSource(explicit=AgentExecutionSpec("native", "malicious-child")),
        subject_id=f"subagent:{request.subagent_id}",
        host_session_id=request.subagent_id,
        workspace=route.bound.binding.workspace,
    )
    calls = _install_session_builder(monkeypatch)
    factory = ExternalSubagentExecutionFactory(route)

    with pytest.raises(ValueError, match="does not match its binding"):
        await factory.create(request, _context())

    assert calls == []


@pytest.mark.asyncio
async def test_child_identity_from_another_parent_is_rejected_before_construction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install_session_builder(monkeypatch)
    factory = ExternalSubagentExecutionFactory(_route(tmp_path))

    with pytest.raises(ValueError, match="does not belong"):
        await factory.create(
            _request("other-parent_sub_explore_deadbeef"),
            _context(),
        )

    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("subagent_type", ["browser_agent", "unknown-agent"])
async def test_unavailable_profile_is_rejected_before_child_construction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    subagent_type: str,
) -> None:
    calls = _install_session_builder(monkeypatch)
    route = _route(tmp_path)
    factory = ExternalSubagentExecutionFactory(route)
    request = dataclasses.replace(_request(), subagent_type=subagent_type)
    binding_count = len(route.bindings._bindings)

    with pytest.raises(ValueError):
        await factory.create(request, _context())

    assert calls == []
    assert len(route.bindings._bindings) == binding_count


@pytest.mark.asyncio
async def test_browser_profile_uses_same_provider_session_and_browser_gateway(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenswarm.runtime.harness import external_subagent as module

    class FakeBrowserGateway:
        execution_identity = SimpleNamespace(
            task=SimpleNamespace(task_id="browser-task-test")
        )

        async def close(self) -> None:
            return None

    gateway = FakeBrowserGateway()
    build_calls: list[dict[str, Any]] = []

    def build_browser(**kwargs: Any):
        build_calls.append(kwargs)
        return SimpleNamespace(
            gateway=gateway,
            system_prompt="Dedicated Browser child prompt.",
        )

    monkeypatch.setattr(module, "_build_external_browser_resources", build_browser)
    monkeypatch.setattr(
        module, "_build_external_browser_identity", lambda **_kwargs: gateway.execution_identity,
    )
    calls = _install_session_builder(monkeypatch)

    admitted: list[tuple[Any, Any]] = []

    def admission(identity: Any, invocation: Any) -> bool:
        admitted.append((identity, invocation))
        return True

    factory = ExternalSubagentExecutionFactory(
        _route(tmp_path),
        browser_admit=admission,
    )
    request = dataclasses.replace(
        _request(),
        subagent_type="browser_agent",
        browser_capabilities=("vision",),
    )

    execution = await factory.create(request, _context())

    assert len(build_calls) == 1
    assert build_calls[0]["request"] is request
    assert build_calls[0]["admit"] is admission
    assert build_calls[0]["artifact_sink"] is None
    assert build_calls[0]["decision_id_for"] is None
    assert admitted[0][0] is gateway.execution_identity
    assert admitted[0][1].call_id == "browser-task-test:profile-use"
    assert admitted[0][1].name == "browser_profile_use"
    assert admitted[0][1].arguments == {}
    assert calls[0][0]["tool_gateway"] is gateway
    assert (
        "Dedicated Browser child prompt." in calls[0][1].started_context.system_prompt
    )
    assert execution.binding.provider_id == "codex"
    await execution.close("test")


@pytest.mark.asyncio
async def test_browser_profile_denial_precedes_resource_and_child_construction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenswarm.runtime.harness import external_subagent as module

    class FakeBrowserGateway:
        execution_identity = SimpleNamespace(
            task=SimpleNamespace(task_id="browser-task-denied")
        )

        def __init__(self) -> None:
            self.closed = False

        async def close(self) -> None:
            self.closed = True

    gateway = FakeBrowserGateway()
    monkeypatch.setattr(
        module, "_build_external_browser_identity", lambda **_kwargs: gateway.execution_identity,
    )

    def unexpected_materialization(**_kwargs):
        raise AssertionError("Profile denial must precede Browser materialization")

    monkeypatch.setattr(
        module,
        "_build_external_browser_resources",
        unexpected_materialization,
    )
    calls = _install_session_builder(monkeypatch)
    admitted: list[Any] = []

    async def deny_profile(_identity: Any, invocation: Any) -> bool:
        admitted.append(invocation)
        return False

    factory = ExternalSubagentExecutionFactory(
        _route(tmp_path),
        browser_admit=deny_profile,
    )
    request = dataclasses.replace(
        _request(),
        subagent_type="browser_agent",
        browser_capabilities=("vision",),
    )

    with pytest.raises(PermissionError, match="profile use is not allowed"):
        await factory.create(request, _context())

    assert calls == []
    assert len(admitted) == 1
    assert admitted[0].call_id == "browser-task-denied:profile-use"
    assert admitted[0].name == "browser_profile_use"
    assert admitted[0].arguments == {}
    assert gateway.closed is False


@pytest.mark.asyncio
async def test_generic_profile_rejects_browser_capabilities_before_construction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install_session_builder(monkeypatch)
    factory = ExternalSubagentExecutionFactory(_route(tmp_path))
    request = dataclasses.replace(_request(), browser_capabilities=("core",))

    with pytest.raises(ValueError, match="only valid"):
        await factory.create(request, _context())

    assert calls == []


def test_non_callable_browser_admission_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="must be callable"):
        ExternalSubagentExecutionFactory(
            _route(tmp_path),
            browser_admit=object(),  # type: ignore[arg-type]
        )


def test_browser_artifact_callbacks_fail_closed_before_construction(
    tmp_path: Path,
) -> None:
    async def sink(_artifact: Any, _path: Path) -> None:
        return None

    route = _route(tmp_path)
    with pytest.raises(ValueError, match="configured together"):
        ExternalSubagentExecutionFactory(
            route,
            browser_admit=lambda _identity, _invocation: True,
            browser_artifact_sink=sink,
        )
    with pytest.raises(ValueError, match="sink must be callable"):
        ExternalSubagentExecutionFactory(
            route,
            browser_admit=lambda _identity, _invocation: True,
            browser_artifact_sink=object(),  # type: ignore[arg-type]
            browser_decision_id_for=lambda _identity, _invocation: "decision",
        )
    with pytest.raises(ValueError, match="requires Browser admission"):
        ExternalSubagentExecutionFactory(
            route,
            browser_artifact_sink=sink,
            browser_decision_id_for=lambda _identity, _invocation: "decision",
        )


@pytest.mark.asyncio
async def test_turn_projects_chunks_and_settles_from_provider_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install_session_builder(monkeypatch)
    execution = await ExternalSubagentExecutionFactory(_route(tmp_path)).create(
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
    execution = await ExternalSubagentExecutionFactory(_route(tmp_path)).create(
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
    assert results[0].output == "External subagent turn failed"


@pytest.mark.asyncio
async def test_close_releases_only_exact_child_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    route = _route(tmp_path)
    _install_session_builder(monkeypatch)
    factory = ExternalSubagentExecutionFactory(route)
    request = _request()
    execution = await factory.create(request, _context())
    child_binding = execution.binding

    await execution.close("done")
    await execution.close("again")
    replacement = route.bindings.bind(
        ExecutionConfigSource(explicit=AgentExecutionSpec("native", "replacement")),
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
    factory = ExternalSubagentExecutionFactory(route)
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
            ExecutionConfigSource(explicit=AgentExecutionSpec("native", "replacement")),
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
    from jiuwenswarm.runtime.harness import external_subagent as module

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
    factory = ExternalSubagentExecutionFactory(route)
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
    execution = await ExternalSubagentExecutionFactory(_route(tmp_path)).create(
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


def test_unsupported_parent_is_rejected(tmp_path: Path) -> None:
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

    with pytest.raises(ValueError, match="supported, consistent parent"):
        ExternalSubagentExecutionFactory(native_route)


@pytest.mark.asyncio
async def test_browser_start_failure_retains_cleanup_before_releasing_recovery(tmp_path, monkeypatch):
    from jiuwenswarm.runtime.harness import external_subagent as module

    route = _route(tmp_path)
    identity = SimpleNamespace(task=SimpleNamespace(task_id="browser-task-cleanup"))
    configuration = route.runtime_paths.internal_workspace_dir / "browser-config" / identity.task.task_id
    events = []

    class Admission:
        def __call__(self, _identity, _invocation):
            return True

        async def release_task(self, _identity):
            assert not configuration.exists()
            events.append("release-recovery")

    class Gateway:
        failed = True

        async def close(self):
            events.append("close")
            if self.failed:
                raise RuntimeError("exit unconfirmed")

    gateway = Gateway()

    def materialize(**_kwargs):
        configuration.mkdir(parents=True)
        (configuration / "managed-download-init.cjs").write_text("test config")
        return SimpleNamespace(gateway=gateway)

    def fail_session(*_args, **_kwargs):
        raise ValueError("startup failed")

    monkeypatch.setattr(module, "_build_external_browser_identity", lambda **_kwargs: identity)
    monkeypatch.setattr(module, "_build_external_browser_resources", materialize)
    monkeypatch.setattr(module, "prepare_execution_session", fail_session)
    factory = ExternalSubagentExecutionFactory(route, browser_admit=Admission())
    request = dataclasses.replace(_request(), subagent_type="browser_agent", browser_capabilities=("vision",))
    with pytest.raises(BaseExceptionGroup):
        await factory.create(request, _context())
    assert configuration.exists()
    assert events == ["close"]
    gateway.failed = False
    await factory.close_pending()
    await factory.close_pending()
    assert events == ["close", "close", "release-recovery"]


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_id", ["codex", "opencode"])
@pytest.mark.parametrize("work_research_enabled", [True, False])
async def test_research_policy_is_frozen_to_work_parent_and_keeps_binding(
    tmp_path, monkeypatch, provider_id, work_research_enabled,
):
    from jiuwenswarm.agents.harness.work.research import work_research_instructions

    route = _route(tmp_path, provider_id=provider_id)
    calls = _install_session_builder(monkeypatch)
    factory = ExternalSubagentExecutionFactory(
        route, work_research_enabled=work_research_enabled,
    )
    request = dataclasses.replace(_request(), subagent_type="research_agent")
    execution = await factory.create(request, _context())
    prompt = calls[0][1].started_context.system_prompt
    assert (work_research_instructions() in prompt) is work_research_enabled
    assert execution.binding.provider_id == route.provider_id
    assert execution.binding.config_revision == route.bound.binding.config_revision
    assert execution.binding.workspace == route.bound.binding.workspace
    assert execution.binding.subject_id != route.bound.binding.subject_id
    gateway = calls[0][0]["tool_gateway"]
    if work_research_enabled:
        assert gateway.tool_names == ("review_research_report",)
        assert gateway.scope.subject_id == execution.binding.subject_id
        assert gateway.scope.host_session_id == request.subagent_id
        assert gateway.scope.workspace == execution.binding.workspace
        result = await gateway.invoke(ToolInvocation(
            call_id="review-invalid-draft",
            name="review_research_report",
            arguments={
                "sources": [{"id": "source", "text": "Observed fact", "start_line": 1, "complete": True}],
                "claims": [{
                    "id": "claim", "section": "Findings", "kind": "fact", "text": "Draft fact",
                    "refs": [{"source_id": "source", "start_line": 1, "end_line": 1, "quote": "Invented quote"}],
                }],
            },
        ))
        assert "structural_valid" in result.content
        assert "false" in result.content.lower()
        unknown = await gateway.invoke(ToolInvocation(
            call_id="not-a-file-tool", name="read_file", arguments={},
        ))
        assert unknown.is_error
    else:
        assert gateway is None
    await execution.close("research_finished")
    assert calls[0][1].stopped


@pytest.mark.asyncio
async def test_work_research_does_not_inject_policy_into_general_child(tmp_path, monkeypatch):
    calls = _install_session_builder(monkeypatch)
    factory = ExternalSubagentExecutionFactory(_route(tmp_path), work_research_enabled=True)
    execution = await factory.create(_request(), _context())
    assert "# Evidence research" not in calls[0][1].started_context.system_prompt
    assert calls[0][0]["tool_gateway"] is None
    await execution.close("finished")


@pytest.mark.asyncio
@pytest.mark.parametrize("work_mode", ["work", "code"])
async def test_frozen_surface_owns_research_policy_over_legacy_flag(
    tmp_path, monkeypatch, work_mode,
):
    from jiuwenswarm.agents.harness.work.research import work_research_instructions
    from jiuwenswarm.runtime.harness.external_subagent_profiles import (
        ExternalSubagentProfileUnavailableError,
    )

    route = _surface_route(tmp_path, work_mode=work_mode)
    calls = _install_session_builder(monkeypatch)
    factory = ExternalSubagentExecutionFactory(
        route, work_research_enabled=work_mode != "work",
    )
    request = dataclasses.replace(_request(), subagent_type="research_agent")
    if work_mode == "code":
        with pytest.raises(ExternalSubagentProfileUnavailableError, match="not mounted"):
            await factory.create(request, _context())
        assert calls == []
        return
    execution = await factory.create(request, _context())
    assert work_research_instructions() in calls[0][1].started_context.system_prompt
    assert calls[0][1].started_context.metadata["surface"]["work_mode"] == "work"
    assert calls[0][0]["tool_gateway"].tool_names == ("review_research_report",)
    await execution.close("research_finished")
    assert calls[0][1].stopped
