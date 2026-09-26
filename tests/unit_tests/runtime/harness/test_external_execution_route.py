# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""R1-03A1 construction routing, cache isolation and shared adapter tests."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from openjiuwen.harness.engine import HarnessEngine
from openjiuwen.harness.engine.config import config_fingerprint
from openjiuwen.harness_protocol import (
    AgentExecutionSpec,
    DeliveryMode,
    HarnessEvent,
    HarnessContext,
    HarnessState,
    SendReceipt,
    TurnError,
    TurnEventKind,
    TurnLifecycleEvent,
    TurnResult,
    TurnStatus,
    TurnTermination,
    TurnTerminationKind,
)
from openjiuwen.core.session.stream.base import OutputSchema
from openjiuwen.harness_providers.io_adapter import ProjectedOutput

from jiuwenswarm.common.runtime_workspace import RuntimeWorkspacePaths
from jiuwenswarm.runtime.harness.binding_store import ExecutionBindingStore
from jiuwenswarm.runtime.harness.config_source import ExecutionConfigSource
from jiuwenswarm.runtime.harness.execution_session import (
    ExecutionExitState,
    ExecutionExitUnconfirmedError,
    ExecutionSession,
)
from jiuwenswarm.runtime.harness.event_projection import ExternalEventProjection
from jiuwenswarm.runtime.harness.context_bridge import (
    build_external_input,
    cleanup_staged_inputs,
)
from jiuwenswarm.runtime.harness.request_binding import (
    AdmittedExecutionRoute,
    bind_admitted_request_execution,
)
from jiuwenswarm.runtime.harness.recovery_store import (
    ExecutionRecoveryUnavailableError,
)
from jiuwenswarm.runtime.plan import PlanModeController, PlanStateResult


def _route(
    tmp_path: Path,
    *,
    session_id: str = "session-1",
    subject_id: str = "alice",
    provider_id: str = "codex",
    revision: str = "r1",
) -> AdmittedExecutionRoute:
    root = (tmp_path / session_id).resolve()
    root.mkdir(parents=True, exist_ok=True)
    source = ExecutionConfigSource(explicit=AgentExecutionSpec(provider_id, revision))
    bindings = ExecutionBindingStore()
    bound = bindings.bind(
        source,
        subject_id=subject_id,
        host_session_id=session_id,
        workspace=str(root),
    )
    paths = RuntimeWorkspacePaths(
        internal_workspace_dir=root,
        runtime_workspace_root=root,
        cwd=root,
        project_root=root,
    )
    return AdmittedExecutionRoute("web", source, bindings, bound, paths)


def test_admission_reuses_runtime_workspace_and_freezes_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jiuwenswarm.runtime.harness import recovery_store

    spec = AgentExecutionSpec("codex", "r1", provider_config={"model": "test"})
    server_config = {
        "execution": {
            "default_profile_id": "native",
            "profiles": {
                "native": {"provider_id": "native", "config_revision": "r1"},
                "codex": {
                    "provider_id": "codex",
                    "config_revision": "r1",
                    "provider_config": {"model": "test"},
                },
            },
        }
    }
    monkeypatch.setattr("jiuwenswarm.common.config.get_config", lambda: server_config)
    monkeypatch.setattr(
        "jiuwenswarm.common.utils.get_agent_workspace_dir",
        lambda: tmp_path / "internal",
    )
    sessions = tmp_path / "sessions"
    monkeypatch.setattr(
        recovery_store,
        "resolve_session_dir",
        lambda session_id, create=False: (
            sessions / session_id,
            None,
        ),
    )
    monkeypatch.setattr(
        recovery_store,
        "get_read_history_path",
        lambda session_id: sessions / session_id / "history.jsonl",
    )
    remembered: list[Any] = []
    manager = SimpleNamespace(
        execution_bindings=ExecutionBindingStore(),
        remember_execution_binding=lambda channel, session, binding: remembered.append(
            (channel, session, binding)
        ),
    )
    project = (tmp_path / "project").resolve()
    project.mkdir()
    request = SimpleNamespace(
        session_id="session-1",
        channel_id="web",
        user_id="alice",
        params={},
    )
    metadata = {
        "execution_profile_id": "codex",
        "execution_config_revision": "r1",
        "execution_config_fingerprint": config_fingerprint(spec),
    }

    route = bind_admitted_request_execution(
        manager,
        request,
        str(project),
        session_metadata=metadata,
    )

    assert route is request._execution_route
    assert route.runtime_paths.runtime_workspace_root == project
    assert route.runtime_paths.cwd == project
    assert route.bound.binding.workspace == str(project)
    assert route.recovery is not None
    assert route.recovery.path == sessions / "session-1" / "execution-recovery.json"
    assert route.cache_identity == ("web", *route.bound.binding.cache_key)
    assert remembered == [("web", "session-1", route.bound.binding)]

    server_config["execution"]["profiles"]["codex"]["provider_config"]["model"] = (
        "changed"
    )
    with pytest.raises(
        ExecutionRecoveryUnavailableError,
        match="configuration fingerprint changed",
    ):
        bind_admitted_request_execution(
            manager,
            request,
            str(project),
            session_metadata=metadata,
        )
    server_config["execution"]["profiles"]["codex"]["provider_config"]["model"] = "test"
    server_config["execution"]["profiles"]["codex"]["authorization"] = {
        "full_access": False
    }
    with pytest.raises(
        ExecutionRecoveryUnavailableError, match="configuration fingerprint changed"
    ):
        bind_admitted_request_execution(
            manager, request, str(project), session_metadata=metadata
        )
    del server_config["execution"]["profiles"]["codex"]["authorization"]
    with pytest.raises(ExecutionRecoveryUnavailableError, match="Binding changed"):
        bind_admitted_request_execution(
            manager,
            SimpleNamespace(
                session_id="session-1",
                channel_id="web",
                user_id="mallory",
                params={},
            ),
            str(project),
            session_metadata=metadata,
        )


@pytest.mark.asyncio
async def test_manager_cache_isolates_external_binding_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jiuwenswarm.server.runtime.agent_adapter import interface
    from jiuwenswarm.server.runtime.agent_manager import AgentManager

    class Facade:
        instances: list["Facade"] = []

        def __init__(self) -> None:
            self.create_kwargs: dict[str, Any] = {}
            self.cleaned = False
            type(self).instances.append(self)

        def set_heartbeat_service(self, _service: object) -> None:
            return None

        def set_personal_context_runtime_enabled(self, _enabled: bool) -> None:
            return None

        def set_permissions_changed_notifier(self, _notifier: object) -> None:
            return None

        def set_permissions_external_input_context_builder(
            self, _builder: object
        ) -> None:
            return None

        async def create_instance(self, _config: object, **kwargs: Any) -> None:
            self.create_kwargs = kwargs

        async def cleanup_session_runtime(self, session_id: str) -> bool:
            route = self.create_kwargs.get("execution_route")
            return bool(
                route is not None and route.bound.binding.host_session_id == session_id
            )

        def has_session_runtime(self, _session_id: str | None = None) -> bool:
            return False

        def owns_external_execution(self, session_id: str | None = None) -> bool:
            route = self.create_kwargs.get("execution_route")
            return bool(
                route is not None
                and (
                    session_id is None
                    or route.bound.binding.host_session_id == session_id
                )
            )

        async def cleanup(self) -> None:
            self.cleaned = True

    monkeypatch.setattr(interface, "JiuWenSwarm", Facade)
    manager = AgentManager()
    first_route = _route(tmp_path, session_id="one")
    second_route = _route(tmp_path, session_id="two")
    try:
        first = await manager.get_agent(
            channel_id="web", mode="code", execution_route=first_route
        )
        first_again = await manager.get_agent(
            channel_id="web", mode="code", execution_route=first_route
        )
        second = await manager.get_agent(
            channel_id="web", mode="code", execution_route=second_route
        )

        assert first is first_again
        assert second is not first
        assert len(Facade.instances) == 2
        assert Facade.instances[0].create_kwargs["execution_route"] is first_route
        assert Facade.instances[1].create_kwargs["execution_route"] is second_route
        keys = tuple(manager.agents["web"])
        assert all(":execution:codex:r1:" in key for key in keys)

        await manager.recreate_agent("web")
        assert manager.agents["web"][keys[0]] is first
        assert manager.agents["web"][keys[1]] is second

        assert (
            await manager.cleanup_session_runtime(channel_id="web", session_id="one")
            is True
        )
        assert first.cleaned is True
        assert tuple(manager.agents["web"].values()) == (second,)
    finally:
        await manager.cleanup()


@pytest.mark.asyncio
async def test_admission_callback_routes_before_facade_construction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jiuwenswarm.server.runtime.agent_adapter import interface
    from jiuwenswarm.server.runtime.agent_manager import AgentManager

    observed: list[AdmittedExecutionRoute | None] = []

    class Facade:
        def set_heartbeat_service(self, _service: object) -> None:
            return None

        def set_personal_context_runtime_enabled(self, _enabled: bool) -> None:
            return None

        def set_permissions_changed_notifier(self, _notifier: object) -> None:
            return None

        def set_permissions_external_input_context_builder(
            self, _builder: object
        ) -> None:
            return None

        async def create_instance(self, _config: object, **kwargs: Any) -> None:
            observed.append(kwargs.get("execution_route"))

        async def cleanup(self) -> None:
            return None

    monkeypatch.setattr(interface, "JiuWenSwarm", Facade)
    manager = AgentManager()
    request = SimpleNamespace(
        channel_id="web",
        session_id="session-1",
        params={"mode": "code"},
    )
    route = _route(tmp_path)

    def on_admitted(_project_dir: str | None) -> None:
        assert observed == []
        request._execution_route = route

    try:
        facade = await manager.get_agent_for_request(
            request,
            on_admitted=on_admitted,
        )
        assert facade is not None
        assert observed == [route]
    finally:
        await manager.cleanup()


@pytest.mark.asyncio
async def test_native_route_keeps_existing_cache_and_external_definition_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jiuwenswarm.server.runtime.agent_adapter import interface
    from jiuwenswarm.server.runtime.agent_manager import AgentManager

    create_calls: list[dict[str, Any]] = []

    class Facade:
        def set_heartbeat_service(self, _service: object) -> None:
            return None

        def set_personal_context_runtime_enabled(self, _enabled: bool) -> None:
            return None

        def set_permissions_changed_notifier(self, _notifier: object) -> None:
            return None

        def set_permissions_external_input_context_builder(
            self, _builder: object
        ) -> None:
            return None

        async def create_instance(self, _config: object, **kwargs: Any) -> None:
            create_calls.append(kwargs)

        async def cleanup(self) -> None:
            return None

    monkeypatch.setattr(interface, "JiuWenSwarm", Facade)
    manager = AgentManager()
    native_route = _route(tmp_path, provider_id="native")
    external_route = _route(tmp_path, session_id="external")
    try:
        native = await manager.get_agent(
            channel_id="web", mode="agent", execution_route=native_route
        )
        existing = await manager.get_agent(channel_id="web", mode="agent")
        assert native is existing
        assert create_calls == [{"mode": "agent", "sub_mode": None}]

        with pytest.raises(ValueError, match="Native Agent definition"):
            await manager.get_agent(
                channel_id="web",
                mode="code",
                execution_route=external_route,
                agent_definition={"id": "native-only"},
                agent_definition_fingerprint="fingerprint",
            )
    finally:
        await manager.cleanup()


@pytest.mark.asyncio
async def test_shared_engine_adapter_constructs_without_native_deep_adapter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jiuwenswarm.server.runtime.agent_adapter import engine_adapter as module
    from jiuwenswarm.server.runtime.agent_adapter.agent_adapters import create_adapter
    from jiuwenswarm.server.runtime.agent_adapter.engine_adapter import (
        EngineAgentAdapter,
    )

    route = _route(tmp_path)
    stopped: list[bool] = []

    class Session:
        binding = route.bound.binding
        closed = False

        async def stop(self) -> None:
            self.closed = True
            stopped.append(True)

        async def abort(self, *, immediate: bool = False) -> None:
            del immediate

    session = Session()
    monkeypatch.setattr(
        module, "prepare_execution_session", lambda *args, **kwargs: session
    )

    adapter = create_adapter("harness", mode="code", execution_route=route)
    assert isinstance(adapter, EngineAgentAdapter)
    await adapter.create_instance(mode="code")
    assert adapter.execution_session is session

    request = SimpleNamespace(_execution_route=route)
    adapter.select_execution_for_request(request)
    with pytest.raises(RuntimeError, match="route changed"):
        adapter.select_execution_for_request(
            SimpleNamespace(_execution_route=_route(tmp_path))
        )
    with pytest.raises(RuntimeError, match="route changed"):
        adapter.select_execution_for_request(
            SimpleNamespace(_execution_route=_route(tmp_path, session_id="other"))
        )

    assert await adapter.cleanup_session_adapter("session-1") is True
    assert stopped == [True]
    assert adapter.has_session_runtime() is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider_id", "harness_type"),
    [
        pytest.param("codex", "CodexHarness", id="codex"),
        pytest.param("opencode", "OpenCodeHarness", id="opencode"),
    ],
)
async def test_actual_facade_constructs_external_provider_through_shared_engine_adapter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider_id: str,
    harness_type: str,
) -> None:
    from jiuwenswarm.server.runtime.agent_adapter.engine_adapter import (
        EngineAgentAdapter,
    )
    from jiuwenswarm.server.runtime.agent_adapter.interface import JiuWenSwarm

    monkeypatch.setattr(
        JiuWenSwarm, "_prepare_skill_library", staticmethod(lambda: None)
    )
    facade = JiuWenSwarm()
    heartbeat_service = AsyncMock()
    facade.set_heartbeat_service(heartbeat_service)
    route = _route(tmp_path, provider_id=provider_id)
    try:
        await facade.create_instance(
            {"channel_id": "web"},
            mode="code",
            execution_route=route,
        )
        assert isinstance(facade._adapter, EngineAgentAdapter)
        session = facade._adapter.execution_session
        assert session is not None
        assert type(session.engine.harness).__name__ == harness_type
        assert session.binding is route.bound.binding
        assert session._tool_gateway is not None
        tool_names = {tool.name for tool in await session._tool_gateway.definitions()}
        assert "heartbeat_create_job" in tool_names
        assert "subagent_spawn" in tool_names
        assert len(tool_names) == 15
        assert facade.owns_external_execution("session-1") is True
    finally:
        await facade.cleanup()


@pytest.mark.asyncio
async def test_execution_session_validates_paths_and_owns_one_event_reader(
    tmp_path: Path,
) -> None:
    route = _route(tmp_path)

    class Cursor:
        def __init__(self) -> None:
            self.closed = asyncio.Event()

        def __aiter__(self):
            return self

        async def __anext__(self):
            await self.closed.wait()
            raise StopAsyncIteration

        async def aclose(self) -> None:
            self.closed.set()

    class Harness:
        def __init__(self) -> None:
            self.card = SimpleNamespace(name="fake")
            self.state = HarnessState.TERMINATED
            self.provider_session_id = None
            self.cursor = Cursor()
            self.start_calls = 0
            self.stop_calls = 0
            self.stop_failures = 0

        async def start(self, _context: HarnessContext) -> None:
            self.start_calls += 1
            self.state = HarnessState.IDLE

        async def stop(self) -> None:
            self.stop_calls += 1
            if self.stop_failures:
                self.stop_failures -= 1
                raise RuntimeError("provider exit unconfirmed")
            self.state = HarnessState.TERMINATED
            self.cursor.closed.set()

        def events(self) -> Cursor:
            return self.cursor

    harness = Harness()
    observed: list[HarnessEvent] = []

    async def observe(event: HarnessEvent) -> None:
        observed.append(event)

    session = ExecutionSession(
        HarnessEngine(route.bound.binding, harness),
        route.runtime_paths,
        event_observer=observe,
    )
    context = HarnessContext(
        agent_name="external",
        agent_id="external-1",
        host_session_id="session-1",
        system_prompt="",
        cwd=str(route.runtime_paths.cwd),
    )

    started = HarnessEvent(
        sequence=1,
        timestamp=1.0,
        event=TurnLifecycleEvent(TurnEventKind.STARTED),
        host_session_id="session-1",
        agent_id="external-1",
        turn_id="turn-1",
    )
    assert session.provider_started("turn-1") is False
    await session._observe_event(started)
    assert session.provider_started("turn-1") is True
    assert observed == [started]
    session.forget_submission("turn-1")
    assert session.provider_started("turn-1") is False

    await session.start(context)
    assert session.started is True
    assert harness.start_calls == 1
    with pytest.raises(RuntimeError, match="already started"):
        await session.start(context)
    await session.stop()
    await session.stop()
    assert session.closed is True
    assert session.exit_state is ExecutionExitState.EXIT_CONFIRMED
    assert harness.stop_calls == 1

    wrong_root = (tmp_path / "outside").resolve()
    wrong_root.mkdir()
    wrong_context = HarnessContext(
        agent_name="external",
        agent_id="external-1",
        host_session_id="session-1",
        system_prompt="",
        cwd=str(wrong_root),
    )
    other_harness = Harness()
    other = ExecutionSession(
        HarnessEngine(route.bound.binding, other_harness),
        route.runtime_paths,
    )
    with pytest.raises(ValueError, match="runtime paths"):
        await other.start(wrong_context)
    await other.stop()


@pytest.mark.asyncio
async def test_execution_session_retries_unconfirmed_provider_exit(
    tmp_path: Path,
) -> None:
    route = _route(tmp_path)

    class Cursor:
        def __init__(self) -> None:
            self.closed = asyncio.Event()

        def __aiter__(self):
            return self

        async def __anext__(self):
            await self.closed.wait()
            raise StopAsyncIteration

    class Harness:
        card = SimpleNamespace(name="fake")
        state = HarnessState.TERMINATED
        provider_session_id = None

        def __init__(self, *, fail_first: bool = True) -> None:
            self.cursor = Cursor()
            self.stop_calls = 0
            self.fail_first = fail_first

        async def start(self, _context) -> None:
            self.state = HarnessState.IDLE

        async def stop(self) -> None:
            self.stop_calls += 1
            if self.fail_first and self.stop_calls == 1:
                raise RuntimeError("still alive")
            self.state = HarnessState.TERMINATED
            self.cursor.closed.set()

        def events(self):
            return self.cursor

    harness = Harness()
    session = ExecutionSession(
        HarnessEngine(route.bound.binding, harness), route.runtime_paths
    )
    await session.start(
        HarnessContext(
            agent_name="external",
            agent_id="external-1",
            host_session_id="session-1",
            system_prompt="",
            cwd=str(route.runtime_paths.cwd),
        )
    )
    other_route = _route(tmp_path, session_id="session-2")
    other_harness = Harness(fail_first=False)
    other = ExecutionSession(
        HarnessEngine(other_route.bound.binding, other_harness),
        other_route.runtime_paths,
    )
    await other.start(
        HarnessContext(
            agent_name="external",
            agent_id="external-2",
            host_session_id="session-2",
            system_prompt="",
            cwd=str(other_route.runtime_paths.cwd),
        )
    )

    with pytest.raises(ExecutionExitUnconfirmedError) as caught:
        await session.stop()
    assert caught.value.code == "EXECUTION_EXIT_UNCONFIRMED"
    assert session.closed is False
    assert session.exit_state is ExecutionExitState.EXIT_UNCONFIRMED
    assert other.exit_state is ExecutionExitState.RUNNING
    assert other.closed is False
    with pytest.raises(RuntimeError, match="not running"):
        session.outputs("turn-1")

    await session.stop()
    assert session.closed is True
    assert session.exit_state is ExecutionExitState.EXIT_CONFIRMED
    assert harness.stop_calls == 2
    await other.stop()
    assert other_harness.stop_calls == 1


@pytest.mark.asyncio
async def test_execution_session_stop_timeout_is_unconfirmed_and_retryable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenswarm.runtime.harness import execution_session as module

    route = _route(tmp_path)
    release = asyncio.Event()
    entered = asyncio.Event()

    class Cursor:
        def __init__(self) -> None:
            self.closed = asyncio.Event()

        def __aiter__(self):
            return self

        async def __anext__(self):
            await self.closed.wait()
            raise StopAsyncIteration

    class Harness:
        card = SimpleNamespace(name="fake")
        state = HarnessState.TERMINATED
        provider_session_id = None

        def __init__(self) -> None:
            self.cursor = Cursor()
            self.stop_calls = 0

        async def start(self, _context) -> None:
            self.state = HarnessState.IDLE

        async def stop(self) -> None:
            self.stop_calls += 1
            if self.stop_calls == 1:
                entered.set()
                await release.wait()
            self.state = HarnessState.TERMINATED
            self.cursor.closed.set()

        def events(self):
            return self.cursor

    monkeypatch.setattr(module, "RESOURCE_STOP_TIMEOUT_S", 0.01)
    harness = Harness()
    session = ExecutionSession(
        HarnessEngine(route.bound.binding, harness), route.runtime_paths
    )
    await session.start(
        HarnessContext(
            agent_name="external",
            agent_id="external-1",
            host_session_id="session-1",
            system_prompt="",
            cwd=str(route.runtime_paths.cwd),
        )
    )

    stopping = asyncio.create_task(session.stop())
    await entered.wait()
    assert session.exit_state is ExecutionExitState.STOP_REQUESTED
    with pytest.raises(ExecutionExitUnconfirmedError) as caught:
        await stopping
    assert isinstance(caught.value.failures[0][1], TimeoutError)
    assert session.exit_state is ExecutionExitState.EXIT_UNCONFIRMED

    release.set()
    await session.stop()
    assert session.exit_state is ExecutionExitState.EXIT_CONFIRMED
    assert harness.stop_calls == 2


@pytest.mark.asyncio
async def test_half_started_session_is_retained_until_cleanup_retry_succeeds(
    tmp_path: Path,
) -> None:
    route = _route(tmp_path)

    class Harness:
        card = SimpleNamespace(name="fake")
        state = HarnessState.TERMINATED
        provider_session_id = None

        def __init__(self) -> None:
            self.stop_calls = 0

        async def start(self, _context) -> None:
            raise RuntimeError("startup failed after allocation")

        async def stop(self) -> None:
            self.stop_calls += 1
            if self.stop_calls < 3:
                raise RuntimeError("half-started process still alive")

    harness = Harness()
    session = ExecutionSession(
        HarnessEngine(route.bound.binding, harness), route.runtime_paths
    )
    context = HarnessContext(
        agent_name="external",
        agent_id="external-1",
        host_session_id="session-1",
        system_prompt="",
        cwd=str(route.runtime_paths.cwd),
    )

    with pytest.raises(ExecutionExitUnconfirmedError):
        await session.start(context)
    assert session.exit_state is ExecutionExitState.EXIT_UNCONFIRMED
    with pytest.raises(RuntimeError, match="cleanup is pending"):
        await session.start(context)

    await session.stop()
    assert session.closed is True
    assert session.exit_state is ExecutionExitState.EXIT_CONFIRMED
    assert harness.stop_calls == 3


@pytest.mark.asyncio
async def test_external_context_stages_only_session_authorized_attachments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jiuwenswarm.runtime.harness import context_bridge

    route = _route(tmp_path)
    (route.runtime_paths.runtime_workspace_root / "JIUWENSWARM.md").write_text(
        "project rule", encoding="utf-8"
    )
    sessions = tmp_path / "sessions"
    upload = sessions / "session-1" / "uploads" / "notes.txt"
    upload.parent.mkdir(parents=True)
    upload.write_text("attachment body", encoding="utf-8")
    monkeypatch.setattr(context_bridge, "get_agent_sessions_dir", lambda: sessions)

    external_input = await build_external_input(
        query="read the attachment",
        request_id="request-1",
        session_id="session-1",
        params={
            "files": {
                "uploaded_documents": [{"filename": "notes.txt", "path": str(upload)}]
            }
        },
        paths=route.runtime_paths,
        include_personal_context=False,
    )

    content = str(external_input.content)
    assert "project rule" in content
    assert "jiuwenswarm-authorized-attachments" in content
    staged = (
        route.runtime_paths.runtime_workspace_root
        / ".jiuwenswarm"
        / "session-inputs"
        / "session-1"
        / "request-1"
        / "notes.txt"
    )
    assert staged.read_text(encoding="utf-8") == "attachment body"

    outside = tmp_path / "other-user.txt"
    outside.write_text("secret", encoding="utf-8")
    with pytest.raises(ValueError, match="authorized roots"):
        await build_external_input(
            query="read",
            request_id="request-2",
            session_id="session-1",
            params={
                "files": {
                    "uploaded_documents": [
                        {"filename": "other-user.txt", "path": str(outside)}
                    ]
                }
            },
            paths=route.runtime_paths,
            include_personal_context=False,
        )

    cleanup_staged_inputs(route.runtime_paths, session_id="session-1")
    assert not staged.exists()


@pytest.mark.asyncio
async def test_engine_adapter_streams_projected_output_and_terminal_final(
    tmp_path: Path,
) -> None:
    from jiuwenswarm.server.runtime.agent_adapter.engine_adapter import (
        EngineAgentAdapter,
    )

    route = _route(tmp_path)

    class Session:
        binding = route.bound.binding
        started = False
        closed = False

        def __init__(self) -> None:
            self.started_context: HarnessContext | None = None
            self.sent = None

        async def start(self, context: HarnessContext) -> None:
            self.started_context = context
            self.started = True

        async def send(self, content: Any, *, immediate: bool = False) -> SendReceipt:
            self.sent = (content, immediate)
            return SendReceipt("message-1", "turn-1", DeliveryMode.AUTO)

        async def outputs(self, turn_id: str):
            assert turn_id == "turn-1"
            yield ProjectedOutput(
                turn_id=turn_id,
                chunk=OutputSchema(
                    type="llm_output",
                    index=0,
                    payload={"content": "hello"},
                ),
            )
            yield ProjectedOutput(turn_id=turn_id, terminal=TurnEventKind.FINISHED)

        def abandon_output(self, _turn_id: str) -> None:
            raise AssertionError("completed output must not be abandoned")

    adapter = EngineAgentAdapter(route)
    session = Session()
    adapter._session = session  # product seam under test; construction is covered above
    request = SimpleNamespace(
        request_id="request-1",
        channel_id="web",
        metadata={},
        params={"mode": "code"},
    )

    chunks = [
        chunk
        async for chunk in adapter.process_message_stream_impl(
            request,
            {"query": "hi"},
        )
    ]

    assert session.started_context is not None
    assert session.started_context.cwd == str(route.runtime_paths.cwd)
    assert session.sent is not None
    assert [chunk.payload["event_type"] for chunk in chunks] == [
        "runtime.accepted",
        "runtime.accepted",
        "chat.delta",
        "chat.final",
    ]
    assert chunks[0].payload["submission_status"] == "harness_accepted"
    assert chunks[1].payload["submission_status"] == "provider_accepted"
    assert chunks[0].payload["provider_message_id"] == "message-1"
    assert chunks[0].payload["provider_turn_id"] == "turn-1"
    assert chunks[2].payload["content"] == "hello"
    assert chunks[-1].payload["terminal_status"] == "completed"
    assert chunks[-1].is_complete is True
    assert chunks[-1].runtime_completion == "completed"


@pytest.mark.asyncio
async def test_external_projection_preserves_normalized_terminal_error() -> None:
    projection = ExternalEventProjection("session-1")
    projection.register_turn(
        "turn-1",
        request_id="request-1",
        channel_id="web",
        mode="code",
    )
    await projection.observe(
        SimpleNamespace(
            turn_id="turn-1",
            event=TurnLifecycleEvent(
                TurnEventKind.FAILED,
                TurnResult(
                    status=TurnStatus.FAILED,
                    error=TurnError("provider rejected the configured source"),
                ),
            ),
        )
    )

    payload = projection.owned_payload(
        ProjectedOutput(turn_id="turn-1", terminal=TurnEventKind.FAILED)
    )

    assert payload == {
        "event_type": "chat.error",
        "error": "provider rejected the configured source",
        "code": "EXECUTION_FAILED",
        "terminal_status": "failed",
    }


@pytest.mark.asyncio
async def test_external_projection_distinguishes_cancelled_terminal() -> None:
    projection = ExternalEventProjection("session-1")
    projection.register_turn(
        "turn-1",
        request_id="request-1",
        channel_id="web",
        mode="code",
    )
    await projection.observe(
        SimpleNamespace(
            turn_id="turn-1",
            event=TurnLifecycleEvent(
                TurnEventKind.ABORTED,
                TurnResult(
                    status=TurnStatus.INTERRUPTED,
                    termination=TurnTermination(
                        TurnTerminationKind.USER_ABORT,
                        "cancelled by the user",
                    ),
                ),
            ),
        )
    )

    payload = projection.owned_payload(
        ProjectedOutput(turn_id="turn-1", terminal=TurnEventKind.ABORTED)
    )

    assert payload == {
        "event_type": "chat.error",
        "error": "cancelled by the user",
        "code": "EXECUTION_CANCELLED",
        "terminal_status": "cancelled",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_started", [False, True])
async def test_engine_adapter_reports_eof_without_terminal_as_unknown(
    tmp_path: Path,
    provider_started: bool,
) -> None:
    from jiuwenswarm.server.runtime.agent_adapter.engine_adapter import (
        EngineAgentAdapter,
    )

    route = _route(tmp_path)

    class Session:
        binding = route.bound.binding
        started = True
        closed = False

        def __init__(self) -> None:
            self.abandoned: list[str] = []

        async def send(self, _content: Any, *, immediate: bool = False) -> SendReceipt:
            assert immediate is False
            return SendReceipt("message-1", "turn-1", DeliveryMode.AUTO)

        async def outputs(self, _turn_id: str):
            if False:
                yield None

        def provider_started(self, _turn_id: str) -> bool:
            return provider_started

        def abandon_output(self, turn_id: str) -> None:
            self.abandoned.append(turn_id)

    adapter = EngineAgentAdapter(route)
    session = Session()
    adapter._session = session
    request = SimpleNamespace(
        request_id="request-1",
        channel_id="web",
        metadata={},
        params={"mode": "code"},
    )

    chunks = [
        chunk
        async for chunk in adapter.process_message_stream_impl(
            request,
            {"query": "hi"},
        )
    ]

    expected = [
        {
            "event_type": "runtime.accepted",
            "request_id": "request-1",
            "submission_status": "harness_accepted",
            "provider_message_id": "message-1",
            "provider_turn_id": "turn-1",
        },
    ]
    if provider_started:
        expected.append(
            {
                "event_type": "runtime.accepted",
                "request_id": "request-1",
                "submission_status": "provider_accepted",
                "provider_message_id": "message-1",
                "provider_turn_id": "turn-1",
            }
        )
    expected.append(
        {
            "event_type": "chat.error",
            "error": "External execution stream ended without a terminal result",
            "code": "EXECUTION_TERMINAL_UNKNOWN",
            "terminal_status": "unknown",
            "submission_status": (
                "provider_accepted" if provider_started else "unknown"
            ),
            "provider_message_id": "message-1",
            "provider_turn_id": "turn-1",
        }
    )
    assert [chunk.payload for chunk in chunks] == expected
    assert chunks[-1].is_complete is True
    assert chunks[-1].runtime_completion == "unknown"
    assert session.abandoned == ["turn-1"]


@pytest.mark.asyncio
async def test_engine_adapter_nonstream_cancel_is_not_ok(tmp_path: Path) -> None:
    from jiuwenswarm.server.runtime.agent_adapter.engine_adapter import (
        EngineAgentAdapter,
    )

    route = _route(tmp_path)

    class Session:
        binding = route.bound.binding
        started = True
        closed = False

        async def send(self, _content: Any, *, immediate: bool = False) -> SendReceipt:
            assert immediate is False
            return SendReceipt("message-1", "turn-1", DeliveryMode.AUTO)

        async def outputs(self, turn_id: str):
            yield ProjectedOutput(turn_id=turn_id, terminal=TurnEventKind.ABORTED)

        def abandon_output(self, _turn_id: str) -> None:
            raise AssertionError("cancelled output observed its terminal")

    adapter = EngineAgentAdapter(route)
    adapter._session = Session()
    request = SimpleNamespace(
        request_id="request-1",
        channel_id="web",
        metadata={},
        params={"mode": "code"},
    )

    response = await adapter.process_message_impl(request, {"query": "hi"})

    assert response.ok is False
    assert response.payload == {
        "content": "",
        "error": "External execution Turn was cancelled",
        "code": "EXECUTION_CANCELLED",
        "terminal_status": "cancelled",
    }


@pytest.mark.asyncio
async def test_external_interaction_answers_and_controls_reuse_live_session(
    tmp_path: Path,
) -> None:
    from jiuwenswarm.server.runtime.agent_adapter.engine_adapter import (
        EngineAgentAdapter,
    )

    route = _route(tmp_path)

    class IO:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def pause(self) -> None:
            self.calls.append("pause")

        async def resume(self) -> None:
            self.calls.append("resume")

    class Session:
        binding = route.bound.binding
        started = True
        closed = False

        def __init__(self) -> None:
            self.io = IO()
            self.answers = []
            self.aborts: list[bool] = []

        async def answer(self, value) -> bool:
            self.answers.append(value)
            return True

        async def abort(self, *, immediate: bool = False) -> None:
            self.aborts.append(immediate)

    adapter = EngineAgentAdapter(route)
    session = Session()
    adapter._session = session
    approval = SimpleNamespace(
        request_id="answer-1",
        channel_id="web",
        metadata={},
        params={
            "request_id": "approval-1",
            "source": "tool_approval",
            "answers": [{"selected_options": ["allow_once"]}],
        },
    )

    response = await adapter.handle_user_answer(approval)
    assert response.payload == {"accepted": True, "resolved": True}
    assert session.answers[0].user_inputs["approval-1"]["approved"] is True

    ask_user = EngineAgentAdapter._interaction_answer(
        {
            "request_id": "question-1",
            "source": "ask_user_interrupt",
            "answers": [
                {
                    "question": "Choose one",
                    "selected_options": ["alpha"],
                    "custom_input": "",
                }
            ],
        }
    )
    assert ask_user is not None
    assert ask_user.user_inputs["question-1"] == {"answers": {"Choose one": "alpha"}}

    for intent in ("pause", "resume", "cancel"):
        result = await adapter.process_interrupt(
            SimpleNamespace(
                request_id=f"interrupt-{intent}",
                channel_id="web",
                metadata={},
                params={"intent": intent},
            )
        )
        assert result.ok is True
    assert session.io.calls == ["pause", "resume"]
    assert session.aborts == [True]


@pytest.mark.asyncio
async def test_detached_external_output_reuses_history_and_push_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenswarm.runtime.harness import event_projection as module

    history: list[dict[str, Any]] = []
    pushes: list[dict[str, Any]] = []

    async def record_history(_call, **kwargs) -> None:
        history.append(kwargs)

    async def record_push(message) -> None:
        pushes.append(message)

    monkeypatch.setattr(module, "run_history_io", record_history)
    monkeypatch.setattr(module, "send_runtime_push", record_push)
    monkeypatch.setattr(
        module,
        "build_server_push_message",
        lambda **kwargs: kwargs,
    )
    projection = ExternalEventProjection("session-1")
    projection.register_turn(
        "turn-1",
        request_id="request-1",
        channel_id="web",
        mode="code",
    )

    await projection(
        ProjectedOutput(
            turn_id="turn-1",
            chunk=OutputSchema(
                type="llm_output",
                index=0,
                payload={"content": "hello"},
            ),
        )
    )
    await projection(ProjectedOutput(turn_id="turn-1", terminal=TurnEventKind.FINISHED))

    assert [item["event_type"] for item in history] == [
        "chat.delta",
        "chat.final",
    ]
    assert history[-1]["content"] == "hello"
    assert [item["payload"]["event_type"] for item in pushes] == [
        "chat.delta",
        "chat.final",
    ]


@pytest.mark.asyncio
async def test_detached_external_push_waits_for_durable_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from concurrent.futures import Future

    from jiuwenswarm.runtime.harness import event_projection as module

    receipt: Future[None] = Future()
    history: list[dict[str, Any]] = []
    pushes: list[dict[str, Any]] = []

    async def record_history(_call, **kwargs):
        history.append(kwargs)
        return receipt

    async def record_push(message) -> None:
        pushes.append(message)

    monkeypatch.setattr(module, "run_history_io", record_history)
    monkeypatch.setattr(module, "send_runtime_push", record_push)
    monkeypatch.setattr(module, "build_server_push_message", lambda **kwargs: kwargs)
    projection = ExternalEventProjection("session-1")
    projection.register_turn(
        "turn-1",
        request_id="request-1",
        channel_id="web",
        mode="code",
    )
    item = ProjectedOutput(
        turn_id="turn-1",
        terminal=TurnEventKind.FINISHED,
    )

    delivery = asyncio.create_task(projection(item))
    await asyncio.sleep(0)
    assert len(history) == 1
    assert history[0]["delivery_id"] == ("harness:turn-1:terminal:finished:chat.final")
    assert pushes == []

    receipt.set_result(None)
    await delivery
    assert len(pushes) == 1


@pytest.mark.asyncio
async def test_external_projection_ignores_output_after_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenswarm.runtime.harness import event_projection as module

    push = AsyncMock()
    monkeypatch.setattr(module, "send_runtime_push", push)
    projection = ExternalEventProjection("session-1")
    projection.register_turn(
        "turn-1",
        request_id="request-1",
        channel_id="web",
        mode="code",
    )

    terminal = projection.owned_payload(
        ProjectedOutput("turn-1", terminal=TurnEventKind.FINISHED)
    )
    await projection(
        ProjectedOutput(
            "turn-1",
            chunk=OutputSchema(
                type="llm_output",
                index=99,
                payload={"content": "late output"},
            ),
        )
    )

    assert terminal is not None
    assert terminal["terminal_status"] == "completed"
    push.assert_not_awaited()


@pytest.mark.asyncio
async def test_detached_external_replay_after_push_failure_does_not_duplicate_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from concurrent.futures import Future

    from jiuwenswarm.runtime.harness import event_projection as module

    monkeypatch.setattr(module, "build_server_push_message", lambda **kwargs: kwargs)
    pushes: list[dict[str, Any]] = []
    persisted: dict[str, dict[str, Any]] = {}
    failed_once = False

    def append_durable(**kwargs):
        persisted.setdefault(kwargs["delivery_id"], kwargs)
        receipt: Future[None] = Future()
        receipt.set_result(None)
        return receipt

    async def direct_history(fn, **kwargs):
        return fn(**kwargs)

    async def push(message: dict[str, Any]) -> None:
        nonlocal failed_once
        pushes.append(message)
        if message["payload"].get("event_type") == "chat.final" and not failed_once:
            failed_once = True
            raise ConnectionError("injected push failure")

    monkeypatch.setattr(module, "send_runtime_push", push)
    monkeypatch.setattr(module, "append_history_record_durable", append_durable)
    monkeypatch.setattr(module, "run_history_io", direct_history)
    projection = ExternalEventProjection("session-replay")
    projection.register_turn(
        "turn-replay",
        request_id="request-replay",
        channel_id="web",
        mode="code",
    )
    monkeypatch.setattr(
        projection,
        "payload",
        lambda _item, *, state=None: {
            "event_type": "chat.final",
            "content": "durable result",
        },
    )
    item = ProjectedOutput(
        turn_id="turn-replay",
        terminal=TurnEventKind.FINISHED,
    )

    await projection(item)
    assert "turn-replay" in projection._turns
    assert pushes[-1]["payload"]["code"] == "DETACHED_DELIVERY_UNCONFIRMED"

    await projection(item)
    assert projection._turns == {}
    assert len(persisted) == 1
    assert next(iter(persisted.values()))["content"] == "durable result"


@pytest.mark.asyncio
async def test_detached_external_persistence_failure_is_product_visible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from concurrent.futures import Future

    from jiuwenswarm.runtime.harness import event_projection as module

    receipt: Future[None] = Future()
    receipt.set_exception(OSError("injected durable write failure"))
    pushes: list[dict[str, Any]] = []

    async def record_history(_call, **_kwargs):
        return receipt

    async def record_push(message: dict[str, Any]) -> None:
        pushes.append(message)

    monkeypatch.setattr(module, "run_history_io", record_history)
    monkeypatch.setattr(module, "send_runtime_push", record_push)
    monkeypatch.setattr(module, "build_server_push_message", lambda **kwargs: kwargs)
    projection = ExternalEventProjection("session-failure")
    projection.register_turn(
        "turn-failure",
        request_id="request-failure",
        channel_id="web",
        mode="code",
    )
    monkeypatch.setattr(
        projection,
        "payload",
        lambda _item, *, state=None: {
            "event_type": "chat.final",
            "content": "result",
        },
    )

    await projection(
        ProjectedOutput(
            turn_id="turn-failure",
            terminal=TurnEventKind.FINISHED,
        )
    )

    assert "turn-failure" in projection._turns
    assert len(pushes) == 1
    assert pushes[0]["payload"]["code"] == "HISTORY_PERSISTENCE_UNCONFIRMED"
    assert pushes[0]["payload"]["event_type"] == "chat.error"


@pytest.mark.asyncio
async def test_request_owned_critical_history_waits_for_durable_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from concurrent.futures import Future

    from jiuwenswarm.server.runtime.agent_adapter import interface as module

    receipt: Future[None] = Future()
    calls: list[dict[str, Any]] = []

    async def direct_history(fn, **kwargs):
        calls.append(kwargs)
        return receipt

    monkeypatch.setattr(module, "_run_history_io", direct_history)
    persistence = asyncio.create_task(
        module._append_request_assistant_history(
            session_id="session-1",
            request_id="request-1",
            channel_id="web",
            event_type="chat.final",
            content="done",
            timestamp=1.0,
            extra={"completed_at": 2.0},
            mode="code",
        )
    )
    await asyncio.sleep(0)

    assert persistence.done() is False
    assert calls[0]["delivery_id"].startswith("request:request-1:chat.final:")
    receipt.set_result(None)
    await persistence


@pytest.mark.asyncio
async def test_external_route_skips_native_deep_agent_plan_state() -> None:
    class ExternalAgent:
        _runtime_execution_route = SimpleNamespace(provider_id="codex")

        async def ensure_instance(self):
            raise AssertionError("External execution must not build a DeepAgent")

    request = SimpleNamespace(
        session_id="session-1",
        req_method=None,
        params={"mode": "agent.code.normal"},
    )
    controller = PlanModeController()

    result = await controller.ensure_state(
        request,
        "code",
        "normal",
        ExternalAgent(),
    )
    exited = await controller.check_post_process_exit(request, ExternalAgent())

    assert result == PlanStateResult()
    assert exited == []
