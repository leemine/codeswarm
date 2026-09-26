# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Real local Codex tools exit before Runtime admits a preempting user turn."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import shlex
import signal
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from openjiuwen.harness_protocol import AgentExecutionSpec

from jiuwenswarm.agents.harness.code.rails.heartbeat import runtime as heartbeat_module
from jiuwenswarm.agents.harness.code.rails.heartbeat.models import HeartbeatSchedule
from jiuwenswarm.agents.harness.code.rails.heartbeat.runtime import HeartbeatRailRuntime
from jiuwenswarm.agents.harness.code.rails.heartbeat.session_resolver import (
    SessionSummary,
)
from jiuwenswarm.common.auth import session_store
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.runtime import AgentRuntime
from jiuwenswarm.runtime.harness import recovery_store
from jiuwenswarm.runtime.harness.execution_session import ExecutionExitState
from jiuwenswarm.runtime.harness.recovery_store import SessionExecutionRecovery
from jiuwenswarm.runtime.plan import PlanStateResult
from jiuwenswarm.runtime.session import SessionWorkKind
from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer
from jiuwenswarm.server.runtime.agent_adapter.engine_adapter import EngineAgentAdapter
from jiuwenswarm.server.runtime.session import session_metadata
from tests.system_tests.test_external_codex_product_route_local import (
    _ResponsesFixture,
    _route,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.system,
    pytest.mark.skipif(sys.platform != "linux", reason="owned /proc process evidence"),
]
SESSION = "r1-a2-session"
USER_QUERY = "R1-08-USER-AFTER-PREEMPTION"
USER_REPLY = "R1-08-RESUMED-USER-OK"


def _process(pid: int) -> tuple[str, str, int] | None:
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().split(") ", 1)[1].split()
        return fields[19], fields[0], int(fields[1])
    except (FileNotFoundError, ProcessLookupError):
        return None


def _owned_tools(scripts: set[bytes]) -> dict[int, str]:
    found = {}
    for path in Path("/proc").iterdir():
        if not path.name.isdigit():
            continue
        try:
            if not scripts.intersection((path / "cmdline").read_bytes().split(b"\0")):
                continue
            state = _process(int(path.name))
            if state is not None:
                found[int(path.name)] = state[0]
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            continue
    return found


def _remaining(owned: dict[int, str]) -> list[int]:
    return [
        pid
        for pid, start in owned.items()
        if (state := _process(pid)) is not None and state[0] == start
    ]


@pytest.mark.asyncio
async def test_real_codex_heartbeat_preemption_waits_for_tools_and_resumes_binding(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Keep the original Scheduler/Runtime/admission and the real External adapter."""
    sdk = pytest.importorskip("openai_codex", reason="locked Codex SDK/CLI required")
    root, home, codex_home = (tmp_path / n for n in ("project", "home", "codex-home"))
    for path in (root, home, codex_home, codex_home / "skills"):
        path.mkdir()
    parent, child = root / "owned_parent.py", root / "owned_child.py"
    child.write_text("import time\ntime.sleep(120)\n")
    parent.write_text(
        "import subprocess,time\n"
        f"subprocess.Popen(['/usr/bin/python3', {str(child)!r}])\n"
        "time.sleep(120)\n"
    )
    scripts = {str(parent).encode(), str(child).encode()}
    binary = str(sdk.client._resolve_codex_bin(sdk.CodexConfig()))
    readable = {
        ":minimal": "read",
        str(root): "write",
        str(codex_home / "tmp"): "read",
        str(Path(binary).parent): "read",
    }
    (codex_home / "config.toml").write_text(
        'default_permissions = "heartbeat-probe"\n[permissions.heartbeat-probe.filesystem]\n'
        + "\n".join(f"{json.dumps(p)} = {json.dumps(a)}" for p, a in readable.items())
        + "\n[permissions.heartbeat-probe.network]\nenabled=false\n"
    )
    sessions, auth = tmp_path / "sessions", tmp_path / "auth"
    auth.mkdir()

    def resolve_session(session_id, create=False):
        path = sessions / session_id
        if create:
            path.mkdir(parents=True, exist_ok=True)
        return path, None

    monkeypatch.setattr(recovery_store, "resolve_session_dir", resolve_session)
    monkeypatch.setattr(
        recovery_store,
        "get_read_history_path",
        lambda sid: sessions / sid / "history.jsonl",
    )
    monkeypatch.setattr(session_store, "auth_dir", lambda: auth)
    monkeypatch.setattr(
        heartbeat_module, "get_heartbeat_jobs_path", lambda: tmp_path / "heartbeat.json"
    )
    monkeypatch.setattr(
        session_metadata,
        "get_session_metadata",
        lambda *a, **k: {"user_id": "local-test"},
    )
    monkeypatch.setattr(
        heartbeat_module,
        "_load_limits",
        lambda: {
            "execution_timeout_seconds": 60,
            "user_preemption_timeout_seconds": 15,
        },
    )
    requests, user_entry_evidence, owned = [], [], {}
    adapter = None

    class Facade:
        async def process_message_stream(self, request):
            # This bridge only supplies the existing product adapter; Runtime
            # owns cancellation, context, generation, admission and scheduling.
            requests.append(request)
            request._execution_route = route
            adapter.select_execution_for_request(request)
            async for chunk in adapter.process_message_stream_impl(
                request,
                {"query": (request.params or {}).get("content", "continue")},
            ):
                yield chunk
                payload = chunk.payload or {}
                if payload.get("event_type") == "chat.ask_user_question":
                    answer = AgentRequest(
                        request_id="local-tool-approval",
                        channel_id="web",
                        session_id=SESSION,
                        params={
                            "request_id": payload["request_id"],
                            "source": payload["source"],
                            "answers": [{"selected_options": ["allow_once"]}],
                        },
                    )
                    accepted = await adapter.handle_user_answer(answer)
                    assert accepted.payload == {"accepted": True, "resolved": True}

    facade = Facade()
    manager = SimpleNamespace(
        begin_foreground_chat=AsyncMock(),
        end_foreground_chat=AsyncMock(),
        cleanup=AsyncMock(),
        cancel_all_inflight_work=AsyncMock(),
        cleanup_session_runtime=AsyncMock(return_value=True),
        pin_agent=Mock(),
        unpin_agent=Mock(),
        get_agent_for_session_nowait=Mock(return_value=facade),
    )
    runtime = AgentRuntime(
        agent_manager=manager,
        initializer=AsyncMock(),
        plan_controller=SimpleNamespace(
            ensure_state=AsyncMock(return_value=PlanStateResult()),
            check_post_process_exit=AsyncMock(return_value=[]),
            reset_session=Mock(),
        ),
    )

    async def prepare(request, channel_id, **kwargs):
        request.params["mode"] = "agent.code.normal"
        return "code", "normal", facade

    runtime._prepare_chat_turn = prepare
    server = AgentWebSocketServer.__new__(AgentWebSocketServer)
    server._runtime, server._agent_manager = runtime, manager
    server.send_push = AsyncMock(return_value=True)
    heartbeat = HeartbeatRailRuntime(server)
    server._heartbeat_runtime = heartbeat
    runtime.set_admission_controller(heartbeat.admission)
    runtime.set_session_delete_lifecycle(heartbeat)
    heartbeat.scheduler._session_resolver = SimpleNamespace(
        resolve=lambda channel_id, session_id: SessionSummary(
            session_id=session_id, channel_id=channel_id
        )
    )
    heartbeat._available = True

    with _ResponsesFixture() as responses:

        def respond(body, index):
            if index == 1:
                return {
                    "type": "function_call",
                    "name": "exec_command",
                    "id": "fc_sleep",
                    "call_id": "call_sleep",
                    "arguments": json.dumps(
                        {
                            "cmd": "/usr/bin/python3 " + shlex.quote(str(parent)),
                            "workdir": str(root),
                            "login": False,
                            "yield_time_ms": 10000,
                        }
                    ),
                }
            if USER_QUERY in json.dumps(body):
                user_entry_evidence.append(_remaining(owned))
            return {
                "type": "message",
                "role": "assistant",
                "id": f"msg_{index}",
                "status": "completed",
                "content": [
                    {"type": "output_text", "text": USER_REPLY, "annotations": []}
                ],
            }

        responses.responder = respond
        spec = AgentExecutionSpec(
            "codex",
            "r1-08-preemption-local",
            provider_config={
                "inherit_process_env": False,
                "env": {
                    "HOME": str(home),
                    "CODEX_HOME": str(codex_home),
                    "PATH": "/usr/bin:/bin",
                    "HTTP_PROXY": "http://127.0.0.1:1",
                    "HTTPS_PROXY": "http://127.0.0.1:1",
                    "NO_PROXY": "127.0.0.1,localhost",
                },
                "startup_source_roots": [str(root), str(codex_home / "skills")],
                "mcp_required": True,
                "mcp_default_tools_approval_mode": "prompt",
                "model": {
                    "model": "gpt-5.6-sol",
                    "provider": "r1_08_fixture",
                    "api_base": responses.base_url,
                    "api_key": "local-only",
                },
            },
        )
        route = _route(root, spec)
        recovery = SessionExecutionRecovery(
            session_id=SESSION,
            execution_profile_id="codex-local",
            binding=route.bound.binding,
            runtime_paths=route.runtime_paths,
        )
        route = dataclasses.replace(route, recovery=recovery)
        adapter = EngineAgentAdapter(route)
        adapter.set_heartbeat_service(heartbeat)
        try:
            async with asyncio.timeout(70):
                await adapter.create_instance()
                job = await heartbeat.store.create_job(
                    name="slow native tool",
                    channel_id="web",
                    session_id=SESSION,
                    prompt="Run the prescribed sleeping tool.",
                    schedule=HeartbeatSchedule.from_dict(
                        {"type": "interval", "interval_seconds": 60}
                    ),
                    source="web_rpc",
                    next_run_at=1000.0,
                    now=999.0,
                    metadata={"user_id": "local-test"},
                )
                heartbeat.scheduler._now_fn = lambda: 1000.0
                await heartbeat.scheduler._tick_once()
                async with asyncio.timeout(30):
                    while len(found := _owned_tools(scripts)) < 2:
                        await asyncio.sleep(0.05)
                owned.update(found)
                original = adapter.execution_session
                assert original is not None and original.started
                provider_session = original.io.session_id
                # Discover only the app-server ancestor of this test's unique
                # tool argv. No SDK internals serve as product control hooks.
                parent_pid = next(pid for pid in owned if _process(pid)[2] not in owned)
                app_pid = _process(parent_pid)[2]
                assert b"app-server" in Path(f"/proc/{app_pid}/cmdline").read_bytes()
                owned[app_pid] = _process(app_pid)[0]
                snapshot = runtime._session_coordinator.snapshot_session(SESSION)
                assert any(
                    x.work_kind is SessionWorkKind.HEARTBEAT
                    and x.state.value == "running"
                    for x in snapshot.executions
                )
                request = AgentRequest(
                    request_id="r1-08-user-preempt",
                    channel_id="web",
                    session_id=SESSION,
                    req_method=ReqMethod.CHAT_SEND,
                    params={"mode": "agent.code.normal", "content": USER_QUERY},
                    is_stream=True,
                    user_id="local-test",
                )
                events = [e async for e in runtime.stream(request, trigger_hook=False)]
                assert user_entry_evidence and all(
                    not pids for pids in user_entry_evidence
                )
                assert _remaining(owned) == []
                assert (
                    original.closed
                    and original.exit_state is ExecutionExitState.EXIT_CONFIRMED
                )
                replacement = adapter.execution_session
                assert (
                    replacement is not original
                    and replacement.binding == original.binding
                )
                assert replacement.io.session_id == provider_session
                assert any(
                    e.event_type == "chat.delta" and USER_REPLY in str(e.payload)
                    for e in events
                )
                assert events[-1].event_type == "chat.final"
                saved = await heartbeat.store.get_job(job.id)
                assert saved.run_state.last_run_status == "cancelled"
                assert (
                    saved.run_state.last_error == "heartbeat preempted by user request"
                )
                snapshot = runtime._session_coordinator.snapshot_session(SESSION)
                assert {x.work_kind: x.state.value for x in snapshot.executions} == {
                    SessionWorkKind.HEARTBEAT: "cancelled",
                    SessionWorkKind.CHAT_STREAM: "succeeded",
                }
                assert requests[0].metadata["automation"]["kind"] == "heartbeat"
                assert requests[0].session_id == requests[1].session_id == SESSION
                assert not heartbeat.execution.active_session_ids()
                assert not heartbeat.admission.is_user_active(SESSION)
                print(
                    json.dumps(
                        {
                            "owned_pids": list(owned),
                            "remaining_before_user_model": user_entry_evidence,
                            "same_provider_session": True,
                            "heartbeat": "cancelled",
                            "user": "succeeded",
                        }
                    )
                )
        finally:
            # Existing lifecycle owns teardown; fallback may signal only exact
            # task PIDs with the same start time, never user processes by name.
            try:
                await asyncio.wait_for(heartbeat.stop(), 20)
                await asyncio.wait_for(runtime.close(), 20)
                await asyncio.wait_for(adapter.cleanup(), 20)
            finally:
                owned.update(_owned_tools(scripts))
                for pid in _remaining(owned):
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
    assert not adapter.has_session_runtime()
