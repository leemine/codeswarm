# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Real Codex CLI through the JiuwenSwarm External Single product adapter."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import shlex
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from openjiuwen.harness_protocol import AgentExecutionSpec, ExecutionAuthorization
from openjiuwen.core.foundation.tool.schema import ToolOutput
from openjiuwen.harness.subagent_runtime import (
    ParentExecutionContext,
    SubagentBuildRequest,
    SubagentTurnRequest,
)
from openjiuwen.harness_providers.codex import native_plugin_content_digest

from jiuwenswarm.common.runtime_workspace import RuntimeWorkspacePaths
from jiuwenswarm.common.auth import session_store
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.runtime.harness.binding_store import ExecutionBindingStore
from jiuwenswarm.runtime.harness.external_subagent import (
    ExternalSubagentExecutionFactory,
)
from jiuwenswarm.runtime.harness.config_source import ExecutionConfigSource
from jiuwenswarm.runtime.harness.request_binding import AdmittedExecutionRoute
from jiuwenswarm.runtime.harness.recovery_store import (
    ExecutionRecoveryUnavailableError,
    SessionExecutionRecovery,
)
from jiuwenswarm.runtime.harness.tool_gateway import (
    ProductToolGateway,
    ProductToolScope,
)
from jiuwenswarm.server.runtime.agent_adapter.engine_adapter import EngineAgentAdapter

pytestmark = [pytest.mark.integration, pytest.mark.system]


class _ResponsesFixture:
    """Minimal loopback Responses endpoint for the bundled real Codex CLI."""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.items: list[dict] = []
        self.responder = None
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, _format, *_args):
                return None

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                owner.requests.append(body)
                index = len(owner.requests)
                item = (
                    owner.responder(body, index)
                    if owner.responder is not None
                    else owner.items.pop(0)
                    if owner.items
                    else {
                        "type": "message",
                        "role": "assistant",
                        "id": f"msg_{index}",
                        "status": "completed",
                        "content": [
                            {
                                "type": "output_text",
                                "text": "R1-A2-PRODUCT-ROUTE-OK",
                                "annotations": [],
                            }
                        ],
                    }
                )
                response = {
                    "id": f"resp_{index}",
                    "object": "response",
                    "status": "in_progress",
                    "output": [],
                }
                events = [
                    ("response.created", {"response": response}),
                    (
                        "response.output_item.added",
                        {"output_index": 0, "item": item},
                    ),
                    (
                        "response.output_item.done",
                        {"output_index": 0, "item": item},
                    ),
                    (
                        "response.completed",
                        {
                            "response": {
                                **response,
                                "status": "completed",
                                "output": [item],
                                "usage": {
                                    "input_tokens": 1,
                                    "output_tokens": 1,
                                    "total_tokens": 2,
                                },
                            }
                        },
                    ),
                ]
                data = "".join(
                    "event: "
                    + name
                    + "\ndata: "
                    + json.dumps({"type": name, **payload})
                    + "\n\n"
                    for name, payload in events
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}/v1"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def _route(root, spec: AgentExecutionSpec) -> AdmittedExecutionRoute:
    source = ExecutionConfigSource(explicit=spec)
    bindings = ExecutionBindingStore()
    bound = bindings.bind(
        source,
        subject_id="local-test",
        host_session_id="r1-a2-session",
        workspace=str(root),
    )
    paths = RuntimeWorkspacePaths(
        internal_workspace_dir=root,
        runtime_workspace_root=root,
        cwd=root,
        project_root=root,
    )
    return AdmittedExecutionRoute("web", source, bindings, bound, paths)


@pytest.mark.asyncio
async def test_real_codex_cli_runs_through_external_product_adapter(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
):
    from jiuwenswarm.runtime.harness import recovery_store

    sdk = pytest.importorskip(
        "openai_codex", reason="optional Codex SDK and bundled CLI required"
    )
    root = (tmp_path / "project").resolve()
    home = (tmp_path / "home").resolve()
    codex_home = (tmp_path / "codex-home").resolve()
    for path in (root, home, codex_home):
        path.mkdir()
    (codex_home / "skills").mkdir()
    binary = sdk.client._resolve_codex_bin(sdk.CodexConfig())
    readable = {
        ":minimal": "read",
        str(root): "read",
        str(codex_home / "tmp"): "read",
        str(os.path.dirname(binary)): "read",
    }
    permission_config = (
        'default_permissions = "r1-a2-read"\n'
        "[permissions.r1-a2-read.filesystem]\n"
        + "\n".join(
            f"{json.dumps(path)} = {json.dumps(access)}"
            for path, access in readable.items()
        )
        + "\n[permissions.r1-a2-read.network]\nenabled=false\n"
    )
    (codex_home / "config.toml").write_text(permission_config, encoding="utf-8")
    (root / "JIUWENSWARM.md").write_text("R1-A2-PROJECT-CONTEXT", encoding="utf-8")
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
    monkeypatch.setattr(session_store, "auth_dir", auth_dir)

    with _ResponsesFixture() as responses:
        spec = AgentExecutionSpec(
            "codex",
            "r1-a2-local",
            provider_config={
                "inherit_process_env": False,
                "env": {
                    "HOME": str(home),
                    "CODEX_HOME": str(codex_home),
                    "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                },
                "startup_source_roots": [str(root), str(codex_home / "skills")],
                "mcp_required": True,
                "mcp_default_tools_approval_mode": "prompt",
                "model": {
                    "model": "gpt-5.6-sol",
                    "provider": "r1_a2_fixture",
                    "api_base": responses.base_url,
                    "api_key": "local-only",
                },
            },
        )
        route = _route(root, spec)
        recovery = SessionExecutionRecovery(
            session_id="r1-a2-session",
            execution_profile_id="codex-local",
            binding=route.bound.binding,
            runtime_paths=route.runtime_paths,
        )
        route = dataclasses.replace(route, recovery=recovery)
        adapter = EngineAgentAdapter(route)
        request = AgentRequest(
            request_id="r1-a2-request",
            channel_id="web",
            session_id="r1-a2-session",
            params={"mode": "code", "query": "R1-A2-USER-QUERY"},
            is_stream=True,
        )
        request._execution_route = route

        try:
            await adapter.create_instance(mode="code")
            adapter.select_execution_for_request(request)
            chunks = [
                chunk
                async for chunk in adapter.process_message_stream_impl(
                    request, {"query": "R1-A2-USER-QUERY"}
                )
            ]
        finally:
            await adapter.cleanup()

        cold_route = _route(root, spec)
        cold_recovery = SessionExecutionRecovery(
            session_id="r1-a2-session",
            execution_profile_id="codex-local",
            binding=cold_route.bound.binding,
            runtime_paths=cold_route.runtime_paths,
        )
        cold_route = dataclasses.replace(cold_route, recovery=cold_recovery)
        cold_adapter = EngineAgentAdapter(cold_route)
        cold_request = AgentRequest(
            request_id="r1-a2-cold-request",
            channel_id="web",
            session_id="r1-a2-session",
            params={"mode": "code", "query": "R1-A2-COLD-RESUME"},
            is_stream=True,
        )
        cold_request._execution_route = cold_route
        try:
            await cold_adapter.create_instance(mode="code")
            cold_adapter.select_execution_for_request(cold_request)
            cold_chunks = [
                chunk
                async for chunk in cold_adapter.process_message_stream_impl(
                    cold_request,
                    {"query": "R1-A2-COLD-RESUME"},
                )
            ]
            cold_session = cold_adapter.execution_session
            assert cold_session is not None
            cold_card = cold_session.engine.harness.card
        finally:
            await cold_adapter.cleanup()

        final_recovery = SessionExecutionRecovery(
            session_id="r1-a2-session",
            execution_profile_id="codex-local",
            binding=cold_route.bound.binding,
            runtime_paths=cold_route.runtime_paths,
        )
        final_plan = final_recovery.prepare(
            cold_card,
            agent_id="external:codex:r1-a2-session",
        )

    payloads = [chunk.payload for chunk in chunks if chunk.payload]
    assert any(
        payload.get("event_type") == "chat.delta"
        and "R1-A2-PRODUCT-ROUTE-OK" in str(payload.get("content"))
        for payload in payloads
    ), payloads
    assert payloads[-1]["event_type"] == "chat.final"
    assert responses.requests
    sent = json.dumps(responses.requests[0], ensure_ascii=False)
    assert "R1-A2-USER-QUERY" in sent
    assert "R1-A2-PROJECT-CONTEXT" in sent
    cold_payloads = [chunk.payload for chunk in cold_chunks if chunk.payload]
    assert cold_payloads[-1]["event_type"] == "chat.final"
    assert len(responses.requests) == 2
    assert "R1-A2-COLD-RESUME" in json.dumps(responses.requests[1], ensure_ascii=False)
    assert final_plan.checkpoint is not None
    assert final_plan.checkpoint.data["resumed"] is True


@pytest.mark.asyncio
async def test_real_codex_cli_runs_independently_bound_child(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
):
    from jiuwenswarm.runtime.harness import recovery_store

    sdk = pytest.importorskip(
        "openai_codex", reason="optional Codex SDK and bundled CLI required"
    )
    root = (tmp_path / "project").resolve()
    task_cwd = root / "task"
    home = (tmp_path / "home").resolve()
    codex_home = (tmp_path / "codex-home").resolve()
    for path in (root, task_cwd, home, codex_home, codex_home / "skills"):
        path.mkdir(exist_ok=True)
    binary = sdk.client._resolve_codex_bin(sdk.CodexConfig())
    readable = {
        ":minimal": "read",
        str(root): "read",
        str(codex_home / "tmp"): "read",
        str(os.path.dirname(binary)): "read",
    }
    permission_config = (
        'default_permissions = "r1-b3-read"\n'
        "[permissions.r1-b3-read.filesystem]\n"
        + "\n".join(
            f"{json.dumps(path)} = {json.dumps(access)}"
            for path, access in readable.items()
        )
        + "\n[permissions.r1-b3-read.network]\nenabled=false\n"
    )
    (codex_home / "config.toml").write_text(permission_config, encoding="utf-8")
    sessions = tmp_path / "sessions"
    auth = tmp_path / "auth"

    def resolve_session(session_id: str, create: bool = False):
        path = sessions / session_id
        if create:
            path.mkdir(parents=True, exist_ok=True)
        return path, None

    def resolve_subagent(parent_id: str, subagent_id: str, create: bool = False):
        path = sessions / parent_id / "subagents" / subagent_id / "history.jsonl"
        if create:
            path.parent.mkdir(parents=True, exist_ok=True)
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
    monkeypatch.setattr(
        recovery_store,
        "resolve_subagent_history_path",
        resolve_subagent,
    )
    monkeypatch.setattr(session_store, "auth_dir", auth_dir)

    with _ResponsesFixture() as responses:
        responses.items.append(
            {
                "type": "message",
                "role": "assistant",
                "id": "msg_r1_b3_child",
                "status": "completed",
                "content": [
                    {
                        "type": "output_text",
                        "text": "R1-B3-CODEX-CHILD-OK",
                        "annotations": [],
                    }
                ],
            }
        )
        responses.items.append(
            {
                "type": "message",
                "role": "assistant",
                "id": "msg_r1_b3_child_resumed",
                "status": "completed",
                "content": [
                    {
                        "type": "output_text",
                        "text": "R1-B3-CODEX-CHILD-RESUMED",
                        "annotations": [],
                    }
                ],
            }
        )
        spec = AgentExecutionSpec(
            "codex",
            "r1-b3-child-local",
            provider_config={
                "inherit_process_env": False,
                "env": {
                    "HOME": str(home),
                    "CODEX_HOME": str(codex_home),
                    "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                },
                "cwd": str(task_cwd),
                "startup_source_roots": [
                    str(root),
                    str(codex_home / "skills"),
                ],
                "mcp_required": False,
                "model": {
                    "model": "gpt-5.6-sol",
                    "provider": "r1_b3_fixture",
                    "api_base": responses.base_url,
                    "api_key": "local-only",
                },
            },
        )
        route = _route(root, spec)
        route = AdmittedExecutionRoute(
            route.channel_id,
            route.source,
            route.bindings,
            route.bound,
            RuntimeWorkspacePaths(
                internal_workspace_dir=root,
                runtime_workspace_root=root,
                cwd=task_cwd,
                project_root=root,
            ),
        )
        route = dataclasses.replace(
            route,
            recovery=SessionExecutionRecovery(
                session_id="r1-a2-session",
                execution_profile_id="codex-local",
                binding=route.bound.binding,
                runtime_paths=route.runtime_paths,
            ),
        )
        factory = ExternalSubagentExecutionFactory(route)
        build_request = SubagentBuildRequest(
            subagent_id="r1-a2-session_sub_explore_b3",
            subagent_type="explore_agent",
            display_name="B3 Explorer",
            role="Return the delegated result",
        )
        parent_context = ParentExecutionContext(
            parent_session_id="r1-a2-session",
            parent_subject_id="local-test",
        )
        execution = await factory.create(build_request, parent_context)
        results = []

        async def settle(result):
            results.append(result)

        try:
            await execution.run_turn(
                SubagentTurnRequest(
                    task_id="r1-b3-task",
                    query="R1-B3-CHILD-QUERY",
                ),
                on_result=settle,
            )
        finally:
            await execution.close("test_complete")

        cold_route = _route(root, spec)
        cold_route = dataclasses.replace(
            cold_route,
            runtime_paths=route.runtime_paths,
            recovery=SessionExecutionRecovery(
                session_id="r1-a2-session",
                execution_profile_id="codex-local",
                binding=cold_route.bound.binding,
                runtime_paths=route.runtime_paths,
            ),
        )
        cold_factory = ExternalSubagentExecutionFactory(cold_route)
        assert await cold_factory.can_restore(build_request, parent_context) is True
        cold_execution = await cold_factory.create(build_request, parent_context)
        cold_results = []

        async def settle_cold(result):
            cold_results.append(result)

        try:
            await cold_execution.run_turn(
                SubagentTurnRequest(
                    task_id="r1-b3-task-resumed",
                    query="R1-B3-CHILD-COLD-RESUME",
                ),
                on_result=settle_cold,
            )
        finally:
            await cold_execution.close("test_complete")

    assert execution.binding is not route.bound.binding
    assert execution.binding.subject_id == "subagent:r1-a2-session_sub_explore_b3"
    assert execution.binding.workspace == route.bound.binding.workspace
    assert execution.binding.fingerprint == route.bound.binding.fingerprint
    assert len(results) == 1
    assert results[0].output == "R1-B3-CODEX-CHILD-OK"
    assert results[0].is_error is False
    assert len(cold_results) == 1
    assert cold_results[0].output == "R1-B3-CODEX-CHILD-RESUMED"
    assert cold_results[0].is_error is False
    assert len(responses.requests) == 2
    rendered = json.dumps(responses.requests[0], ensure_ascii=False)
    assert "R1-B3-CHILD-QUERY" in rendered
    assert str(task_cwd) in rendered
    assert "R1-B3-CHILD-COLD-RESUME" in json.dumps(
        responses.requests[1], ensure_ascii=False
    )
    provider_sessions = [
        str((request.get("client_metadata") or {}).get("session_id") or "")
        for request in responses.requests
    ]
    assert provider_sessions[0]
    assert provider_sessions[1] == provider_sessions[0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "authorization", [None, ExecutionAuthorization(False), ExecutionAuthorization(True)]
)
async def test_real_codex_cli_calls_session_owned_product_gateway(
    tmp_path,
    authorization,
    monkeypatch: pytest.MonkeyPatch,
):
    from jiuwenswarm.runtime.harness import recovery_store

    sdk = pytest.importorskip(
        "openai_codex", reason="optional Codex SDK and bundled CLI required"
    )
    root = (tmp_path / "project").resolve()
    home = (tmp_path / "home").resolve()
    codex_home = (tmp_path / "codex-home").resolve()
    for path in (root, home, codex_home, codex_home / "skills"):
        path.mkdir()
    binary = sdk.client._resolve_codex_bin(sdk.CodexConfig())
    readable = {
        ":minimal": "read",
        str(root): "read",
        str(codex_home / "tmp"): "read",
        str(os.path.dirname(binary)): "read",
    }
    permission_config = (
        'default_permissions = "r1-b1-read"\n'
        "[permissions.r1-b1-read.filesystem]\n"
        + "\n".join(
            f"{json.dumps(path)} = {json.dumps(access)}"
            for path, access in readable.items()
        )
        + "\n[permissions.r1-b1-read.network]\nenabled=false\n"
    )
    (codex_home / "config.toml").write_text(permission_config, encoding="utf-8")
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
    monkeypatch.setattr(session_store, "auth_dir", auth_dir)

    class EchoTool:
        card = SimpleNamespace(
            name="echo",
            description="Return the fixed B1 product marker.",
            input_params={
                "type": "object",
                "properties": {"value": {"type": "string"}},
                "required": ["value"],
                "additionalProperties": False,
            },
            parallel_safe=True,
        )

        def __init__(self) -> None:
            self.calls = []

        async def invoke(self, inputs, **_kwargs):
            self.calls.append(dict(inputs))
            return ToolOutput(success=True, data={"content": "R1-B1-PRODUCT-MCP-OK"})

        def render_for_llm(self, output) -> str:
            return str(output.data["content"])

    tool = EchoTool()
    gateway = ProductToolGateway(
        [tool],
        scope=ProductToolScope("local-test", "r1-a2-session", str(root)),
    )

    with _ResponsesFixture() as responses:
        responses.items.append(
            {
                "type": "function_call",
                "namespace": "mcp__jiuwenswarm_product_tools",
                "name": "echo",
                "id": "fc_r1_b1",
                "call_id": "call_r1_b1",
                "arguments": json.dumps({"value": "from-codex"}),
            }
        )
        spec = AgentExecutionSpec(
            "codex",
            "r1-b1-product-tools-local",
            authorization=authorization,
            provider_config={
                "inherit_process_env": False,
                "env": {
                    "HOME": str(home),
                    "CODEX_HOME": str(codex_home),
                    "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                },
                "startup_source_roots": (
                    None
                    if authorization and authorization.full_access
                    else [str(root), str(codex_home / "skills")]
                ),
                "mcp_required": True,
                "mcp_default_tools_approval_mode": "prompt",
                "model": {
                    "model": "gpt-5.6-sol",
                    "provider": "r1_b1_fixture",
                    "api_base": responses.base_url,
                    "api_key": "local-only",
                },
            },
        )
        route = _route(root, spec)
        route = dataclasses.replace(
            route,
            recovery=SessionExecutionRecovery(
                session_id="r1-a2-session",
                execution_profile_id="codex-local",
                binding=route.bound.binding,
                runtime_paths=route.runtime_paths,
            ),
        )
        adapter = EngineAgentAdapter(route, tool_gateway=gateway)
        request = AgentRequest(
            request_id="r1-b1-product-tool-request",
            channel_id="web",
            session_id="r1-a2-session",
            params={"mode": "code", "query": "Call the prescribed product tool once."},
            is_stream=True,
        )
        request._execution_route = route
        chunks = []
        approval_count = 0
        cold_blocked = False
        transport = None
        try:
            await adapter.create_instance(mode="code")
            adapter.select_execution_for_request(request)
            stream = adapter.process_message_stream_impl(
                request, {"query": "Call the prescribed product tool once."}
            )
            while True:
                try:
                    chunk = await anext(stream)
                except StopAsyncIteration:
                    break
                chunks.append(chunk)
                payload = chunk.payload or {}
                if payload.get("event_type") != "chat.ask_user_question":
                    continue
                approval_count += 1
                live_session = adapter.execution_session
                assert live_session is not None
                cold = SessionExecutionRecovery(
                    session_id="r1-a2-session",
                    execution_profile_id="codex-local",
                    binding=route.bound.binding,
                    runtime_paths=route.runtime_paths,
                )
                with pytest.raises(
                    ExecutionRecoveryUnavailableError,
                    match="interaction Turn did not reach",
                ):
                    cold.prepare(
                        live_session.engine.harness.card,
                        agent_id="external:codex:r1-a2-session",
                    )
                cold_blocked = True
                answer = AgentRequest(
                    request_id=f"r1-b1-answer-{approval_count}",
                    channel_id="web",
                    session_id="r1-a2-session",
                    params={
                        "request_id": payload["request_id"],
                        "source": payload["source"],
                        "answers": [{"selected_options": ["allow_once"]}],
                    },
                )
                accepted = await adapter.handle_user_answer(answer)
                assert accepted.payload == {"accepted": True, "resolved": True}
            live_session = adapter.execution_session
            assert live_session is not None
            resumed = SessionExecutionRecovery(
                session_id="r1-a2-session",
                execution_profile_id="codex-local",
                binding=route.bound.binding,
                runtime_paths=route.runtime_paths,
            ).prepare(
                live_session.engine.harness.card,
                agent_id="external:codex:r1-a2-session",
            )
            assert resumed.resume_policy.value == "require_resume"
            transport = adapter.execution_session._tool_transport
            assert transport is not None and transport.started
        finally:
            await adapter.cleanup()

    needs_approval = authorization is None or not authorization.full_access
    assert approval_count == int(needs_approval)
    assert cold_blocked is needs_approval
    assert tool.calls == [{"value": "from-codex"}]
    assert "R1-B1-PRODUCT-MCP-OK" in json.dumps(responses.requests)
    assert any(
        (chunk.payload or {}).get("event_type") == "chat.final" for chunk in chunks
    )
    assert transport is not None and not transport.started


@pytest.mark.asyncio
async def test_real_codex_cli_runs_six_tool_same_engine_child_chain(tmp_path):
    sdk = pytest.importorskip(
        "openai_codex", reason="optional Codex SDK and bundled CLI required"
    )
    root = (tmp_path / "project").resolve()
    home = (tmp_path / "home").resolve()
    codex_home = (tmp_path / "codex-home").resolve()
    for path in (root, home, codex_home, codex_home / "skills"):
        path.mkdir()
    binary = sdk.client._resolve_codex_bin(sdk.CodexConfig())
    readable = {
        ":minimal": "read",
        str(root): "read",
        str(codex_home / "tmp"): "read",
        str(os.path.dirname(binary)): "read",
    }
    permission_config = (
        'default_permissions = "r1-b4-read"\n'
        "[permissions.r1-b4-read.filesystem]\n"
        + "\n".join(
            f"{json.dumps(path)} = {json.dumps(access)}"
            for path, access in readable.items()
        )
        + "\n[permissions.r1-b4-read.network]\nenabled=false\n"
    )
    (codex_home / "config.toml").write_text(permission_config, encoding="utf-8")

    with _ResponsesFixture() as responses:
        parent_calls = 0
        parent_provider_session = None

        def respond(body, index):
            nonlocal parent_calls, parent_provider_session
            provider_session = str(
                (body.get("client_metadata") or {}).get("session_id") or ""
            )
            if parent_provider_session is None:
                parent_provider_session = provider_session
            if provider_session != parent_provider_session:
                return {
                    "type": "message",
                    "role": "assistant",
                    "id": f"msg_child_{index}",
                    "status": "completed",
                    "content": [
                        {
                            "type": "output_text",
                            "text": "R1-B4-CHILD-OK",
                            "annotations": [],
                        }
                    ],
                }
            parent_calls += 1
            if parent_calls == 1:
                return {
                    "type": "function_call",
                    "namespace": "mcp__jiuwenswarm_product_tools",
                    "name": "subagent_spawn",
                    "id": "fc_r1_b4_spawn",
                    "call_id": "call_r1_b4_spawn",
                    "arguments": json.dumps(
                        {
                            "subagent_type": "verification_agent",
                            "task_description": "R1-B4-CHILD-QUERY",
                            "display_name": "B4 verifier",
                            "role": "Return the fixed child marker",
                        }
                    ),
                }
            if parent_calls == 2:
                return {
                    "type": "function_call",
                    "namespace": "mcp__jiuwenswarm_product_tools",
                    "name": "subagent_wait",
                    "id": "fc_r1_b4_wait",
                    "call_id": "call_r1_b4_wait",
                    "arguments": json.dumps(
                        {
                            "subagent_ids": ["r1-a2-session_sub_verification_agent"],
                            "timeout_ms": 120_000,
                        }
                    ),
                }
            return {
                "type": "message",
                "role": "assistant",
                "id": f"msg_parent_{index}",
                "status": "completed",
                "content": [
                    {
                        "type": "output_text",
                        "text": "R1-B4-PARENT-OK",
                        "annotations": [],
                    }
                ],
            }

        responses.responder = respond
        spec = AgentExecutionSpec(
            "codex",
            "r1-b4-product-subagents-local",
            provider_config={
                "inherit_process_env": False,
                "env": {
                    "HOME": str(home),
                    "CODEX_HOME": str(codex_home),
                    "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                },
                "startup_source_roots": [str(root), str(codex_home / "skills")],
                "mcp_required": True,
                "mcp_default_tools_approval_mode": "prompt",
                "model": {
                    "model": "gpt-5.6-sol",
                    "provider": "r1_b4_fixture",
                    "api_base": responses.base_url,
                    "api_key": "local-only",
                },
            },
        )
        route = _route(root, spec)
        adapter = EngineAgentAdapter(route)
        request = AgentRequest(
            request_id="r1-b4-product-subagent-request",
            channel_id="web",
            session_id="r1-a2-session",
            params={"mode": "code", "query": "R1-B4-PARENT-QUERY"},
            is_stream=True,
        )
        request._execution_route = route
        chunks = []
        approval_count = 0
        runtime = None
        try:
            await adapter.create_instance(mode="code")
            runtime = adapter._subagent_runtime
            adapter.select_execution_for_request(request)
            stream = adapter.process_message_stream_impl(
                request,
                {"query": "R1-B4-PARENT-QUERY"},
            )
            async for chunk in stream:
                chunks.append(chunk)
                payload = chunk.payload or {}
                if payload.get("event_type") != "chat.ask_user_question":
                    continue
                approval_count += 1
                answer = AgentRequest(
                    request_id=f"r1-b4-answer-{approval_count}",
                    channel_id="web",
                    session_id="r1-a2-session",
                    params={
                        "request_id": payload["request_id"],
                        "source": payload["source"],
                        "answers": [{"selected_options": ["allow_once"]}],
                    },
                )
                accepted = await adapter.handle_user_answer(answer)
                assert accepted.payload == {"accepted": True, "resolved": True}
        finally:
            await adapter.cleanup()

    rendered_requests = json.dumps(responses.requests, ensure_ascii=False)
    assert approval_count == 2, (
        responses.requests,
        [chunk.payload for chunk in chunks],
    )
    assert "R1-B4-CHILD-OK" in rendered_requests
    assert "R1-B4-PARENT-OK" in json.dumps(
        [chunk.payload for chunk in chunks],
        ensure_ascii=False,
    )
    assert runtime is not None and runtime.has_control() is False
    assert not any(
        bound.binding.host_session_id.startswith("r1-a2-session_sub_")
        for bound in route.bindings._bindings.values()
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "allow,authorization",
    [
        (False, None),
        (True, None),
        (False, ExecutionAuthorization(False)),
        (True, ExecutionAuthorization(False)),
        (True, ExecutionAuthorization(True)),
    ],
)
async def test_real_codex_tool_approval_roundtrips_through_product_adapter(
    tmp_path, allow, authorization, monkeypatch
):
    pytest.importorskip(
        "openai_codex", reason="optional Codex SDK and bundled CLI required"
    )
    root = (tmp_path / "project").resolve()
    home = (tmp_path / "home").resolve()
    codex_home = (tmp_path / "codex-home").resolve()
    for path in (root, home, codex_home):
        path.mkdir()
    marker = root / "approval-marker.txt"
    sessions = tmp_path / "sessions"
    upload = sessions / "r1-a2-session" / "uploads" / "attachment.txt"
    upload.parent.mkdir(parents=True)
    upload.write_text("R1-A2-ATTACHMENT", encoding="utf-8")
    from jiuwenswarm.runtime.harness import context_bridge

    monkeypatch.setattr(context_bridge, "get_agent_sessions_dir", lambda: sessions)
    staged = (
        root
        / ".jiuwenswarm"
        / "session-inputs"
        / "r1-a2-session"
        / "r1-a2-approval-request"
        / "attachment.txt"
    )

    with _ResponsesFixture() as responses:
        responses.items.append(
            {
                "type": "function_call",
                "name": "exec_command",
                "id": "fc_r1_a2",
                "call_id": "call_r1_a2",
                "arguments": json.dumps(
                    {
                        "cmd": (
                            f"cat {shlex.quote(str(staged))} > approval-marker.txt"
                        ),
                        "workdir": str(root),
                    }
                ),
            }
        )
        spec = AgentExecutionSpec(
            "codex",
            "r1-a2-approval-local",
            authorization=authorization,
            provider_config={
                "inherit_process_env": False,
                "env": {
                    "HOME": str(home),
                    "CODEX_HOME": str(codex_home),
                    "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                },
                "mcp_required": False,
                "model": {
                    "model": "gpt-5.6-sol",
                    "provider": "r1_a2_fixture",
                    "api_base": responses.base_url,
                    "api_key": "local-only",
                },
            },
        )
        route = _route(root, spec)
        adapter = EngineAgentAdapter(route)
        request = AgentRequest(
            request_id="r1-a2-approval-request",
            channel_id="web",
            session_id="r1-a2-session",
            params={
                "mode": "code",
                "query": "Run the prescribed tool once.",
                "files": {
                    "uploaded_documents": [
                        {"filename": "attachment.txt", "path": str(upload)}
                    ]
                },
            },
            is_stream=True,
        )
        request._execution_route = route
        chunks = []

        try:
            await adapter.create_instance(mode="code")
            adapter.select_execution_for_request(request)
            stream = adapter.process_message_stream_impl(
                request, {"query": "Run the prescribed tool once."}
            )
            if authorization is not None and authorization.full_access:
                chunks.extend([chunk async for chunk in stream])
                assert not any(
                    (chunk.payload or {}).get("event_type") == "chat.ask_user_question"
                    for chunk in chunks
                )
            else:
                while True:
                    chunk = await anext(stream)
                    chunks.append(chunk)
                    payload = chunk.payload or {}
                    if payload.get("event_type") == "chat.ask_user_question":
                        break
                answer = AgentRequest(
                    request_id="r1-a2-answer",
                    channel_id="web",
                    session_id="r1-a2-session",
                    params={
                        "request_id": payload["request_id"],
                        "source": payload["source"],
                        "answers": [
                            {"selected_options": ["allow_once" if allow else "deny"]}
                        ],
                    },
                )
                answer_response = await adapter.handle_user_answer(answer)
                assert answer_response.payload == {"accepted": True, "resolved": True}
                chunks.extend([chunk async for chunk in stream])
        finally:
            await adapter.cleanup()

    assert marker.exists() is allow
    if allow:
        assert marker.read_text(encoding="utf-8") == "R1-A2-ATTACHMENT"
    assert not staged.exists()
    assert any(
        (chunk.payload or {}).get("event_type") == "chat.final" for chunk in chunks
    )


@pytest.mark.asyncio
async def test_real_codex_native_plugin_runs_through_product_adapter(tmp_path):
    sdk = pytest.importorskip(
        "openai_codex", reason="optional Codex SDK and bundled CLI required"
    )
    root = (tmp_path / "project").resolve()
    home = (tmp_path / "home").resolve()
    codex_home = (tmp_path / "codex-home").resolve()
    market = (tmp_path / "market").resolve()
    plugin = market / "plugins" / "c1-probe"
    for path in (
        root,
        home,
        codex_home,
        codex_home / "skills",
        market / ".agents/plugins",
        plugin / ".codex-plugin",
        plugin / "skills/marker",
    ):
        path.mkdir(parents=True)
    (market / ".agents/plugins/marketplace.json").write_text(
        json.dumps(
            {
                "name": "c1-local",
                "plugins": [
                    {
                        "name": "c1-probe",
                        "source": {"source": "local", "path": "./plugins/c1-probe"},
                        "policy": {
                            "installation": "AVAILABLE",
                            "authentication": "ON_INSTALL",
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    (plugin / ".codex-plugin/plugin.json").write_text(
        json.dumps(
            {
                "name": "c1-probe",
                "version": "1.0.0",
                "skills": "./skills",
                "mcpServers": "./.mcp.json",
            }
        ),
        encoding="utf-8",
    )
    (plugin / "skills/marker/SKILL.md").write_text(
        "---\nname: marker\ndescription: Read the C1 product marker and call its MCP tool.\n---\n"
        "C1-PRODUCT-SKILL\n",
        encoding="utf-8",
    )
    marker = root / "c1-mcp-called.txt"
    pidfile = root / "c1-mcp.pid"
    server_code = (
        "import os\nfrom pathlib import Path\nfrom mcp.server.fastmcp import FastMCP\n"
        f"Path({str(pidfile)!r}).write_text(str(os.getpid()))\n"
        "m=FastMCP('c1-product')\n@m.tool()\ndef marker() -> str:\n"
        '    """Return the C1 product marker."""\n'
        f"    Path({str(marker)!r}).write_text('C1-PRODUCT-MCP-CALLED')\n"
        "    return 'C1-PRODUCT-MCP-OK'\nm.run(transport='stdio')\n"
    )
    (plugin / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "c1_probe": {"command": sys.executable, "args": ["-c", server_code]}
                }
            }
        ),
        encoding="utf-8",
    )
    env = {
        "HOME": str(home),
        "CODEX_HOME": str(codex_home),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
    }
    binary = str(sdk.client._resolve_codex_bin(sdk.CodexConfig()))
    for args in (
        ("plugin", "marketplace", "add", str(market)),
        ("plugin", "add", "c1-probe@c1-local"),
    ):
        process = await asyncio.create_subprocess_exec(
            binary,
            *args,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), 20)
        assert process.returncode == 0, (stdout, stderr)
    config_path = codex_home / "config.toml"
    installed_config = config_path.read_text(encoding="utf-8")
    readable = {
        ":minimal": "read",
        str(root): "read",
        str(codex_home): "read",
        str(Path(binary).parent): "read",
        str(Path(sys.executable).parent): "read",
    }
    permission_config = (
        'default_permissions = "c1-product"\n[permissions.c1-product.filesystem]\n'
    )
    permission_config += "\n".join(
        f"{json.dumps(path)} = {json.dumps(access)}"
        for path, access in readable.items()
    )
    permission_config += "\n[permissions.c1-product.network]\nenabled=false\n"
    config_path.write_text(permission_config + installed_config, encoding="utf-8")
    installed_plugin = codex_home / "plugins/cache/c1-local/c1-probe/1.0.0"
    skill = installed_plugin / "skills/marker/SKILL.md"

    with _ResponsesFixture() as responses:
        responses.items.extend(
            [
                {
                    "type": "function_call",
                    "name": "exec_command",
                    "id": "fc_c1_skill",
                    "call_id": "call_c1_skill",
                    "arguments": json.dumps(
                        {"cmd": f"cat {shlex.quote(str(skill))}", "login": False}
                    ),
                },
                {
                    "type": "function_call",
                    "namespace": "mcp__c1_probe",
                    "name": "marker",
                    "id": "fc_c1_mcp",
                    "call_id": "call_c1_mcp",
                    "arguments": "{}",
                },
            ]
        )
        spec = AgentExecutionSpec(
            "codex",
            "r1-c1-plugin-local",
            provider_config={
                "inherit_process_env": False,
                "env": env,
                "startup_source_roots": [
                    str(root),
                    str(codex_home / "plugins"),
                    str(codex_home / "skills"),
                    str(market),
                ],
                "native_plugins": [
                    {
                        "plugin_id": "c1-probe@c1-local",
                        "source_type": "local",
                        "source_locator": str(plugin),
                        "version": "1.0.0",
                        "content_sha256": native_plugin_content_digest(
                            installed_plugin
                        ),
                        "enabled": True,
                        "required_components": ["skills", "mcp"],
                        "mcp_server_names": ["c1_probe"],
                    }
                ],
                "model": {
                    "model": "gpt-5.6-sol",
                    "provider": "r1_c1_fixture",
                    "api_base": responses.base_url,
                    "api_key": "local-only",
                },
            },
        )
        route = _route(root, spec)
        adapter = EngineAgentAdapter(route)
        request = AgentRequest(
            request_id="r1-c1-plugin-request",
            channel_id="web",
            session_id="r1-a2-session",
            params={"mode": "code", "query": "Use the C1 native plugin once."},
            is_stream=True,
        )
        request._execution_route = route
        chunks = []
        approval_count = 0
        try:
            await adapter.create_instance(mode="code")
            adapter.select_execution_for_request(request)
            stream = adapter.process_message_stream_impl(
                request, {"query": "Use the C1 native plugin once."}
            )
            while True:
                try:
                    chunk = await anext(stream)
                except StopAsyncIteration:
                    break
                chunks.append(chunk)
                payload = chunk.payload or {}
                if payload.get("event_type") != "chat.ask_user_question":
                    continue
                approval_count += 1
                answer = AgentRequest(
                    request_id=f"r1-c1-answer-{approval_count}",
                    channel_id="web",
                    session_id="r1-a2-session",
                    params={
                        "request_id": payload["request_id"],
                        "source": payload["source"],
                        "answers": [{"selected_options": ["allow_once"]}],
                    },
                )
                accepted = await adapter.handle_user_answer(answer)
                assert accepted.payload == {"accepted": True, "resolved": True}
        finally:
            await adapter.cleanup()

    assert approval_count == 2
    assert marker.read_text(encoding="utf-8") == "C1-PRODUCT-MCP-CALLED"
    rendered_requests = json.dumps(responses.requests)
    assert "C1-PRODUCT-SKILL" in rendered_requests
    assert "C1-PRODUCT-MCP-OK" in rendered_requests
    assert any(
        (chunk.payload or {}).get("event_type") == "chat.final" for chunk in chunks
    )
    pid = int(pidfile.read_text(encoding="utf-8"))
    deadline = time.monotonic() + 5
    while Path(f"/proc/{pid}").exists() and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
    assert not Path(f"/proc/{pid}").exists()


@pytest.mark.asyncio
async def test_real_codex_heartbeat_tool_creates_job_and_scheduler_reuses_session(tmp_path, monkeypatch):
    """Real CLI/MCP plus original Scheduler/Runtime; model responses stay local."""
    from unittest.mock import AsyncMock, Mock

    from jiuwenswarm.agents.harness.code.rails.heartbeat import runtime as heartbeat_module
    from jiuwenswarm.agents.harness.code.rails.heartbeat.runtime import HeartbeatRailRuntime
    from jiuwenswarm.agents.harness.code.rails.heartbeat.session_resolver import SessionSummary
    from jiuwenswarm.common.schema.message import ReqMethod
    from jiuwenswarm.runtime import AgentRuntime
    from jiuwenswarm.runtime.plan import PlanStateResult
    from jiuwenswarm.runtime.session import SessionWorkKind
    from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer
    from jiuwenswarm.server.runtime.session import session_metadata

    pytest.importorskip("openai_codex", reason="optional Codex SDK and bundled CLI required")
    root, home, codex_home = (tmp_path / name for name in ("project", "home", "codex-home"))
    for path in (root, home, codex_home, codex_home / "skills"):
        path.mkdir()
    monkeypatch.setattr(heartbeat_module, "get_heartbeat_jobs_path", lambda: tmp_path / "heartbeat.json")
    monkeypatch.setattr(session_metadata, "get_session_metadata", lambda *args, **kwargs: {"user_id": "local-test"})
    finished = asyncio.Event()
    calls = []
    adapter = None

    class Facade:
        async def process_message_stream(self, request):
            calls.append(request)
            request._execution_route = route
            adapter.select_execution_for_request(request)
            async for chunk in adapter.process_message_stream_impl(request, {"query": (request.params or {}).get("content", "continue")}):
                yield chunk

    facade = Facade()
    manager = SimpleNamespace(
        begin_foreground_chat=AsyncMock(), end_foreground_chat=AsyncMock(),
        cleanup=AsyncMock(), cancel_all_inflight_work=AsyncMock(),
        cleanup_session_runtime=AsyncMock(return_value=True),
        pin_agent=Mock(), unpin_agent=Mock(),
        get_agent_for_session_nowait=Mock(return_value=facade),
    )
    runtime = AgentRuntime(
        agent_manager=manager, initializer=AsyncMock(),
        plan_controller=SimpleNamespace(
            ensure_state=AsyncMock(return_value=PlanStateResult()),
            check_post_process_exit=AsyncMock(return_value=[]), reset_session=Mock(),
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
        resolve=lambda channel_id, session_id: SessionSummary(session_id=session_id, channel_id=channel_id)
    )
    # Drive the clock explicitly; no scheduler sleep or remote model is involved.
    heartbeat._available = True

    async def completed(session_id):
        await heartbeat._release_if_no_active_jobs(session_id)
        finished.set()

    heartbeat.execution.set_completion_hook(completed)
    with _ResponsesFixture() as responses:
        responses.items = [
            {"type": "function_call", "namespace": "mcp__jiuwenswarm_product_tools", "name": "heartbeat_create_job", "id": "fc_hb", "call_id": "call_hb", "arguments": json.dumps({"name": "followup", "prompt": "Return R1-08-AUTO-OK", "schedule": {"type": "interval", "interval_seconds": 60}, "max_runs": 1})},
        ]
        spec = AgentExecutionSpec(
            "codex", "r1-08-local", authorization=ExecutionAuthorization(full_access=True),
            provider_config={
                "inherit_process_env": False,
                "env": {"HOME": str(home), "CODEX_HOME": str(codex_home), "PATH": os.environ.get("PATH", "/usr/bin:/bin")},
                "mcp_required": True,
                "model": {"model": "gpt-5.6-sol", "provider": "r1_08_fixture", "api_base": responses.base_url, "api_key": "local-only"},
            },
        )
        route = _route(root, spec)
        adapter = EngineAgentAdapter(route)
        adapter.set_heartbeat_service(heartbeat)
        transport = None
        try:
            await adapter.create_instance()
            request = AgentRequest(request_id="r1-08-create", channel_id="web", session_id="r1-a2-session", req_method=ReqMethod.CHAT_SEND, params={"mode": "agent.code.normal", "content": "Create the prescribed follow-up."}, is_stream=True, user_id="local-test")
            async with asyncio.timeout(45):
                initial = [event async for event in runtime.stream(request, trigger_hook=False)]
                assert any(event.event_type == "chat.final" for event in initial)
                jobs = await heartbeat.store.list_jobs()
                assert len(jobs) == 1
                job = jobs[0]
                assert job.session_id == request.session_id
                assert job.metadata["user_id"] == "local-test"
                original_session = adapter.execution_session
                transport = original_session._tool_transport
                heartbeat.scheduler._now_fn = lambda: job.next_run_at
                await heartbeat.scheduler._tick_once()
                await finished.wait()
            saved = await heartbeat.store.get_job(job.id)
            assert saved.run_count == 1
            assert saved.run_state.last_run_status == "succeeded"
            assert adapter.execution_session is original_session
            assert len(calls) == 2
            assert calls[1].metadata["automation"]["kind"] == "heartbeat"
            assert calls[1].session_id == request.session_id
            snapshot = runtime._session_coordinator.snapshot_session(request.session_id)
            assert sum(item.work_kind is SessionWorkKind.HEARTBEAT and item.state.value == "succeeded" for item in snapshot.executions) == 1
            pushes = [call.args[0] for call in server.send_push.await_args_list]
            assert any(item["payload"].get("event_type") == "chat.final" for item in pushes)
            assert pushes[-1]["payload"]["is_processing"] is False
            assert heartbeat._pinned_agents == {}
            assert not heartbeat.execution.active_session_ids()
            assert len(responses.requests) >= 3
        finally:
            await heartbeat.stop()
            await runtime.close()
            await adapter.cleanup()
    assert transport is not None and not transport.started
    assert not adapter.has_session_runtime()
