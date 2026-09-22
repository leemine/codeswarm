# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""R1-03A1 construction routing, cache isolation and shared adapter tests."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from openjiuwen.harness.engine import HarnessEngine
from openjiuwen.harness.engine.config import config_fingerprint
from openjiuwen.harness_protocol import (
    AgentExecutionSpec,
    DeliveryMode,
    HarnessContext,
    HarnessState,
    SendReceipt,
    TurnError,
    TurnEventKind,
    TurnLifecycleEvent,
    TurnResult,
    TurnStatus,
)
from openjiuwen.core.session.stream.base import OutputSchema
from openjiuwen.harness_providers.io_adapter import ProjectedOutput

from jiuwenswarm.common.runtime_workspace import RuntimeWorkspacePaths
from jiuwenswarm.runtime.harness.binding_store import ExecutionBindingStore
from jiuwenswarm.runtime.harness.config_source import ExecutionConfigSource
from jiuwenswarm.runtime.harness.execution_session import ExecutionSession
from jiuwenswarm.runtime.harness.event_projection import ExternalEventProjection
from jiuwenswarm.runtime.harness.context_bridge import (
    build_external_input,
    cleanup_staged_inputs,
)
from jiuwenswarm.runtime.harness.request_binding import (
    AdmittedExecutionRoute,
    bind_admitted_request_execution,
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
    source = ExecutionConfigSource(
        explicit=AgentExecutionSpec(provider_id, revision)
    )
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
    assert route.cache_identity == ("web", *route.bound.binding.cache_key)
    assert remembered == [("web", "session-1", route.bound.binding)]


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

        def set_permissions_external_input_context_builder(self, _builder: object) -> None:
            return None

        async def create_instance(self, _config: object, **kwargs: Any) -> None:
            self.create_kwargs = kwargs

        async def cleanup_session_runtime(self, session_id: str) -> bool:
            route = self.create_kwargs.get("execution_route")
            return bool(
                route is not None
                and route.bound.binding.host_session_id == session_id
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

        assert await manager.cleanup_session_runtime(
            channel_id="web", session_id="one"
        ) is True
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

        def set_permissions_external_input_context_builder(self, _builder: object) -> None:
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

        def set_permissions_external_input_context_builder(self, _builder: object) -> None:
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
    from jiuwenswarm.server.runtime.agent_adapter.engine_adapter import EngineAgentAdapter

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
    monkeypatch.setattr(module, "prepare_execution_session", lambda *args, **kwargs: session)

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
async def test_actual_facade_constructs_codex_through_shared_engine_adapter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from openjiuwen.harness_providers.codex import CodexHarness

    from jiuwenswarm.server.runtime.agent_adapter.engine_adapter import EngineAgentAdapter
    from jiuwenswarm.server.runtime.agent_adapter.interface import JiuWenSwarm

    monkeypatch.setattr(JiuWenSwarm, "_prepare_skill_library", staticmethod(lambda: None))
    facade = JiuWenSwarm()
    route = _route(tmp_path)
    try:
        await facade.create_instance(
            {"channel_id": "web"},
            mode="code",
            execution_route=route,
        )
        assert isinstance(facade._adapter, EngineAgentAdapter)
        session = facade._adapter.execution_session
        assert session is not None
        assert isinstance(session.engine.harness, CodexHarness)
        assert session.binding is route.bound.binding
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

        async def start(self, _context: HarnessContext) -> None:
            self.start_calls += 1
            self.state = HarnessState.IDLE

        async def stop(self) -> None:
            self.stop_calls += 1
            self.state = HarnessState.TERMINATED
            self.cursor.closed.set()

        def events(self) -> Cursor:
            return self.cursor

    harness = Harness()
    session = ExecutionSession(
        HarnessEngine(route.bound.binding, harness),
        route.runtime_paths,
    )
    context = HarnessContext(
        agent_name="external",
        agent_id="external-1",
        host_session_id="session-1",
        system_prompt="",
        cwd=str(route.runtime_paths.cwd),
    )

    await session.start(context)
    assert session.started is True
    assert harness.start_calls == 1
    with pytest.raises(RuntimeError, match="already started"):
        await session.start(context)
    await session.stop()
    await session.stop()
    assert session.closed is True
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
                "uploaded_documents": [
                    {"filename": "notes.txt", "path": str(upload)}
                ]
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
        "chat.delta",
        "chat.final",
    ]
    assert chunks[0].payload["content"] == "hello"


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
    await projection(
        ProjectedOutput(turn_id="turn-1", terminal=TurnEventKind.FINISHED)
    )

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
