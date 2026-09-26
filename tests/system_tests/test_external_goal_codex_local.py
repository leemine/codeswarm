# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Real Codex CLI/MCP Goal completion with loopback model fixtures only.

This verifies the product execution seam, not remote LLM quality or browser UI.
The real Runtime coordinator, Engine adapter, GoalManager, tools, assessor Model
and durable history writer are used. No production execution loop is replaced.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from openjiuwen.harness.goal import GoalStatus
from openjiuwen.harness_protocol import AgentExecutionSpec, ExecutionAuthorization

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.runtime import AgentRuntime
from jiuwenswarm.runtime.context import reset_runtime_context, set_runtime_context
from jiuwenswarm.runtime.harness.goal_assessment import catalog_model_factory
from jiuwenswarm.runtime.session.model import SessionWorkKind
from jiuwenswarm.server.runtime.agent_adapter.engine_adapter import EngineAgentAdapter
from jiuwenswarm.server.runtime.session.session_history import load_history_records

from .test_external_codex_product_route_local import _ResponsesFixture, _route

pytestmark = [pytest.mark.integration, pytest.mark.system]


class _AssessmentEndpoint:
    """OpenAI-compatible loopback endpoint used by the actual core Model.invoke."""

    def __init__(self):
        self.requests = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, _format, *_args):
                return None

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                owner.requests.append(body)
                payload = json.dumps(
                    {
                        "id": "assessment-local",
                        "object": "chat.completion",
                        "created": 1,
                        "model": "local-assessor",
                        "choices": [
                            {
                                "index": 0,
                                "finish_reason": "stop",
                                "message": {
                                    "role": "assistant",
                                    "content": json.dumps(
                                        {
                                            "status": "complete",
                                            "evidence": "R1-09B-INDEPENDENT-ASSESSOR-VERIFIED",
                                        }
                                    ),
                                },
                            }
                        ],
                        "usage": {
                            "prompt_tokens": 5,
                            "completion_tokens": 3,
                            "total_tokens": 8,
                        },
                    }
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self.server.server_port}/v1"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


@pytest.mark.asyncio
async def test_real_codex_goal_tools_assessment_usage_and_history(
    tmp_path, monkeypatch
):
    pytest.importorskip(
        "openai_codex", reason="optional real bundled Codex CLI required"
    )
    root, home, codex_home = (
        tmp_path / name for name in ("project", "home", "codex-home")
    )
    for path in (root, home, codex_home, codex_home / "skills"):
        path.mkdir()
    monkeypatch.setenv("JIUWENSWARM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("JIUWENSWARM_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.chdir(tmp_path)
    runtime = AgentRuntime()
    sid = "r1-a2-session"  # Existing local product route fixture's bound session.
    await runtime._session_coordinator.register_session(sid, "web")
    adapter = None
    context = None
    transport = None
    identity = None
    with _ResponsesFixture() as responses, _AssessmentEndpoint() as assessment:

        def respond(body, index):
            nonlocal identity
            if identity is None:
                marker = (
                    "submit_goal_report must include this exact attempt identity:\n"
                )
                for text in _strings(body):
                    if marker in text:
                        identity, _ = json.JSONDecoder().raw_decode(
                            text.split(marker, 1)[1].lstrip()
                        )
                        break
            if index <= 2:
                args = (
                    {}
                    if index == 1
                    else {
                        **identity,
                        "status": "complete",
                        "evidence": "R1-09B-REAL-MCP-REPORT",
                    }
                )
                return {
                    "type": "function_call",
                    "namespace": "mcp__jiuwenswarm_product_tools",
                    "name": "get_current_goal" if index == 1 else "submit_goal_report",
                    "id": f"fc_goal_{index}",
                    "call_id": f"call_goal_{index}",
                    "arguments": json.dumps(args),
                }
            return {
                "type": "message",
                "role": "assistant",
                "id": f"msg_goal_{index}",
                "status": "completed",
                "content": [
                    {
                        "type": "output_text",
                        "text": "R1-09B-LOCAL-GOAL-COMPLETE",
                        "annotations": [],
                    }
                ],
            }

        responses.responder = respond
        spec = AgentExecutionSpec(
            "codex",
            "r1-09b-loopback",
            authorization=ExecutionAuthorization(full_access=True),
            provider_config={
                "inherit_process_env": False,
                "env": {
                    "HOME": str(home),
                    "CODEX_HOME": str(codex_home),
                    "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                },
                "mcp_required": True,
                "model": {
                    "model": "gpt-5.6-sol",
                    "provider": "r1_09b_loopback",
                    "api_base": responses.base_url,
                    "api_key": "local-only-not-a-secret",
                },
            },
        )
        adapter = EngineAgentAdapter(_route(root, spec))
        adapter.set_goal_assessor_factory(
            catalog_model_factory(
                [
                    {
                        "model_client_config": {
                            "client_provider": "OpenAI",
                            "model_name": "local-assessor",
                            "api_base": assessment.base_url,
                            "api_key": "local-only-not-a-secret",
                            "endpoint_profile": "openai_compatible",
                            "timeout": 15,
                        },
                        "model_config_obj": {},
                    }
                ],
                "local-assessor#0",
            )
        )
        request = AgentRequest(
            request_id="r1-09b-local-goal",
            session_id=sid,
            channel_id="web",
            user_id="local-test",
            req_method=ReqMethod.COMMAND_GOAL,
            is_stream=True,
            params={
                "mode": "agent.code",
                "action": "set",
                "objective": "Report R1-09B local MCP success",
                "max_attempts": 2,
            },
        )
        request._execution_route = adapter.route
        try:
            await adapter.create_instance()
            adapter.select_execution_for_request(request)
            context = set_runtime_context(runtime, None)
            async with asyncio.timeout(90):
                chunks = [
                    chunk
                    async for chunk in runtime._session_coordinator.run_stream(
                        sid,
                        request.request_id,
                        SessionWorkKind.GOAL_STREAM,
                        lambda: adapter.process_message_stream_impl(
                            request, {"query": request.params["objective"]}
                        ),
                    )
                ]
            transport = adapter.execution_session._tool_transport
            goal = adapter._goal_runtime.manager.peek()
            assert goal.status is GoalStatus.COMPLETED, goal.to_dict()
            assert goal.attempt_count == goal.last_assessed_attempt == 1
            assert (
                goal.last_assessment.evidence == "R1-09B-INDEPENDENT-ASSESSOR-VERIFIED"
            )
            assert len(assessment.requests) == 1
            assert not assessment.requests[0].get("tools")
            assert assessment.requests[0]["temperature"] == 0.0
            assert "R1-09B-LOCAL-GOAL-COMPLETE" in json.dumps(
                assessment.requests[0]["messages"]
            )
            assert identity is not None and identity["goal_id"] == goal.goal_id
            assert (
                identity["attempt_index"] == 1 and identity["revision"] == goal.revision
            )
            assert len(responses.requests) == 3
            tool_outputs = list(_strings(responses.requests[2]))
            assert any("R1-09B-REAL-MCP-REPORT" in value for value in tool_outputs)
            assert any(
                "Goal report accepted (status: complete)." in value
                for value in tool_outputs
            )
            assert goal.token_usage.total_tokens == 2 * len(responses.requests) + 8
            assert goal.token_usage.input_tokens == len(responses.requests) + 5
            assert goal.token_usage.output_tokens == len(responses.requests) + 3
            assert len([chunk for chunk in chunks if chunk.is_complete]) == 1
            assert (
                len(
                    [
                        chunk
                        for chunk in chunks
                        if (chunk.payload or {}).get("event_type") == "chat.final"
                    ]
                )
                == 1
            )
            rows = load_history_records(sid)
            objectives = [
                row
                for row in rows
                if row.get("is_goal_objective_message")
                and row.get("goal_id") == goal.goal_id
            ]
            cards = [
                row
                for row in rows
                if row.get("is_goal_completed_message")
                and row.get("goal_id") == goal.goal_id
            ]
            assert len(objectives) == len(cards) == 1
            assert objectives[0]["content"] == request.params["objective"]
            assert cards[0]["id"] == f"goal-completed-{goal.goal_id}"
            assert adapter._goal_runtime.owner is None
            snapshot = runtime._session_coordinator.snapshot_session(sid)
            assert (
                sum(
                    item.work_kind is SessionWorkKind.GOAL_STREAM
                    and item.state.value == "succeeded"
                    for item in snapshot.executions
                )
                == 1
            )
            (tmp_path / "result.json").write_text(
                json.dumps(
                    {
                        "local_only": True,
                        "real_codex_cli_mcp": True,
                        "remote_model": False,
                        "provider_requests": len(responses.requests),
                        "assessor_requests": len(assessment.requests),
                        "goal": goal.to_dict(),
                        "objective_rows": len(objectives),
                        "completion_cards": len(cards),
                    },
                    indent=2,
                )
            )
        finally:
            if context is not None:
                reset_runtime_context(context)
            await runtime.close()
            if adapter is not None:
                await adapter.cleanup()
    assert transport is not None and not transport.started
    assert not adapter.has_session_runtime()
