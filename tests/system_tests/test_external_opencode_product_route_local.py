# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Real OpenCode CLI through the JiuwenSwarm External Single product adapter.

RUN_OPENCODE_OC3=1 pytest tests/system_tests/test_external_opencode_product_route_local.py
requires non-root Linux, cgroup v2, and an active systemd user manager.  The
model endpoint is a task-owned loopback fixture; no remote model is contacted.
"""

from __future__ import annotations

import dataclasses
import json
import os
import socket
from pathlib import Path

import pytest
from aiohttp import web

from openjiuwen.harness_protocol import AgentExecutionSpec, ExecutionAuthorization

from jiuwenswarm.common.auth import session_store
from jiuwenswarm.common.runtime_workspace import RuntimeWorkspacePaths
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.runtime.harness.binding_store import ExecutionBindingStore
from jiuwenswarm.runtime.harness.config_source import ExecutionConfigSource
from jiuwenswarm.runtime.harness.recovery_store import SessionExecutionRecovery
from jiuwenswarm.runtime.harness.request_binding import AdmittedExecutionRoute
from jiuwenswarm.server.runtime.agent_adapter.engine_adapter import EngineAgentAdapter

pytestmark = [
    pytest.mark.integration,
    pytest.mark.system,
    pytest.mark.skipif(
        os.environ.get("RUN_OPENCODE_OC3") != "1",
        reason="real managed OpenCode product route is opt-in",
    ),
]


class _ModelFixture:
    def __init__(self) -> None:
        self.actions: list[dict] = []
        self.requests: list[dict] = []

    async def respond(self, request: web.Request) -> web.Response:
        body = await request.json()
        self.requests.append(body)
        action = (
            self.actions.pop(0)
            if body.get("tools") and self.actions
            else {"text": "OC3-PRODUCT-ROUTE-OK"}
        )
        if "tool" in action:
            delta = {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": f"call_{len(self.requests)}",
                        "type": "function",
                        "function": {
                            "name": action["tool"],
                            "arguments": json.dumps(action["args"]),
                        },
                    }
                ]
            }
            finish = "tool_calls"
        else:
            delta = {"content": action.get("text", "OC3-PRODUCT-ROUTE-OK")}
            finish = "stop"
        base = {
            "id": f"chatcmpl-oc3-{len(self.requests)}",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "fixture",
        }
        chunks = [
            {
                **base,
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant", **delta},
                        "finish_reason": None,
                    }
                ],
            },
            {
                **base,
                "choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
                "usage": {
                    "prompt_tokens": 17,
                    "completion_tokens": 5,
                    "total_tokens": 22,
                },
            },
        ]
        data = "".join(
            "data: " + json.dumps(chunk) + "\n\n" for chunk in chunks
        ) + "data: [DONE]\n\n"
        return web.Response(text=data, content_type="text/event-stream")


def _route(root: Path, spec: AgentExecutionSpec) -> AdmittedExecutionRoute:
    source = ExecutionConfigSource(explicit=spec)
    bindings = ExecutionBindingStore()
    bound = bindings.bind(
        source,
        subject_id="local-test",
        host_session_id="oc3-session",
        workspace=str(root),
    )
    paths = RuntimeWorkspacePaths(
        internal_workspace_dir=root,
        runtime_workspace_root=root,
        cwd=root,
        project_root=root,
    )
    return AdmittedExecutionRoute("web", source, bindings, bound, paths)


@pytest.fixture
def product_storage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from jiuwenswarm.runtime.harness import context_bridge, recovery_store

    sessions = tmp_path / "sessions"
    auth = tmp_path / "auth"

    def resolve_session(session_id: str, create: bool = False):
        path = sessions / session_id
        if create:
            path.mkdir(parents=True, exist_ok=True)
        return path, None

    def auth_dir() -> Path:
        auth.mkdir(parents=True, exist_ok=True)
        return auth

    monkeypatch.setattr(recovery_store, "resolve_session_dir", resolve_session)
    monkeypatch.setattr(
        recovery_store,
        "get_read_history_path",
        lambda session_id: sessions / session_id / "history.jsonl",
    )
    monkeypatch.setattr(context_bridge, "get_agent_sessions_dir", lambda: sessions)
    monkeypatch.setattr(session_store, "auth_dir", auth_dir)
    return sessions


@pytest.mark.asyncio
async def test_real_opencode_cli_runs_product_context_interaction_and_cold_resume(
    tmp_path: Path,
    product_storage: Path,
) -> None:
    model = _ModelFixture()
    app = web.Application()
    app.router.add_post("/v1/chat/completions", model.respond)
    runner = web.AppRunner(app)
    await runner.setup()
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    await web.SockSite(runner, sock).start()

    root = (tmp_path / "project").resolve()
    runtime_root = (tmp_path / "runtime").resolve()
    root.mkdir()
    runtime_root.mkdir(mode=0o700)
    (root / "JIUWENSWARM.md").write_text("OC3-PROJECT-CONTEXT", encoding="utf-8")
    upload = product_storage / "oc3-session" / "uploads" / "notes.txt"
    upload.parent.mkdir(parents=True)
    upload.write_text("OC3-ATTACHMENT-CONTENT", encoding="utf-8")
    model.actions = [
        {
            "tool": "bash",
            "args": {
                "command": "printf OC3-APPROVED-TOOL > oc3-artifact.txt",
                "description": "OC3 product approval fixture",
            },
        },
        {"text": "OC3-PRODUCT-ROUTE-OK"},
    ]
    spec = AgentExecutionSpec(
        "opencode",
        "oc3-local-r1",
        authorization=ExecutionAuthorization(False),
        provider_config={
            "cli_path": os.environ.get(
                "OPENCODE_OC1_CLI", os.path.expanduser("~/.opencode/bin/opencode")
            ),
            "runtime_root": str(runtime_root),
            "model": {
                "model": "fixture",
                "api_base": f"http://127.0.0.1:{port}/v1",
                "api_key": "fixture-only",
            },
            "turn_timeout_s": 25,
        },
    )

    async def run_once(request_id: str, query: str, *, attachment: bool):
        route = _route(root, spec)
        recovery = SessionExecutionRecovery(
            session_id="oc3-session",
            execution_profile_id="opencode-local",
            binding=route.bound.binding,
            runtime_paths=route.runtime_paths,
        )
        route = dataclasses.replace(route, recovery=recovery)
        adapter = EngineAgentAdapter(route)
        params = {"mode": "code", "query": query}
        if attachment:
            params["files"] = {
                "uploaded_documents": [
                    {"filename": "notes.txt", "path": str(upload)}
                ]
            }
        request = AgentRequest(
            request_id=request_id,
            channel_id="web",
            session_id="oc3-session",
            params=params,
            is_stream=True,
        )
        request._execution_route = route
        chunks = []
        approvals = 0
        provider_session_id = None
        try:
            await adapter.create_instance(mode="code")
            adapter.select_execution_for_request(request)
            stream = adapter.process_message_stream_impl(request, {"query": query})
            async for chunk in stream:
                chunks.append(chunk)
                payload = chunk.payload or {}
                if payload.get("event_type") != "chat.ask_user_question":
                    continue
                approvals += 1
                answer = AgentRequest(
                    request_id=f"{request_id}-answer",
                    channel_id="web",
                    session_id="oc3-session",
                    params={
                        "request_id": payload["request_id"],
                        "source": payload["source"],
                        "answers": [{"selected_options": ["allow_once"]}],
                    },
                )
                accepted = await adapter.handle_user_answer(answer)
                assert accepted.payload == {"accepted": True, "resolved": True}
            session = adapter.execution_session
            assert session is not None
            provider_session_id = session.engine.harness.provider_session_id
        finally:
            await adapter.cleanup()
        return route, chunks, approvals, provider_session_id

    try:
        first_route, chunks, approvals, first_provider_session = await run_once(
            "oc3-request", "OC3-USER-QUERY", attachment=True
        )
        _, cold_chunks, cold_approvals, cold_provider_session = await run_once(
            "oc3-cold-request", "OC3-COLD-RESUME", attachment=False
        )
    finally:
        await runner.cleanup()
        for directory in runtime_root.rglob("*"):
            if directory.is_dir() and not directory.is_symlink():
                directory.chmod(0o700)

    payloads = [chunk.payload for chunk in chunks if chunk.payload]
    assert approvals == 1
    assert any(payload.get("event_type") == "chat.tool_result" for payload in payloads)
    assert payloads[-1]["event_type"] == "chat.final"
    assert cold_approvals == 0
    assert cold_chunks[-1].payload["event_type"] == "chat.final"
    assert first_provider_session == cold_provider_session
    rendered = json.dumps(model.requests[0], ensure_ascii=False)
    assert "OC3-USER-QUERY" in rendered
    assert "OC3-PROJECT-CONTEXT" in rendered
    assert "notes.txt" in rendered
    assert "jiuwenswarm-authorized-attachments" in rendered
    assert "OC3-COLD-RESUME" in json.dumps(model.requests[-1], ensure_ascii=False)
    assert (root / "oc3-artifact.txt").read_text() == "OC3-APPROVED-TOOL"
    assert not (
        first_route.runtime_paths.runtime_workspace_root
        / ".jiuwenswarm"
        / "session-inputs"
        / "oc3-session"
    ).exists()
