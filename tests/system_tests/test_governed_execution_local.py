# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Real Codex CLI + local Responses fixture at the governed Runtime boundary.

The Runtime, disk ACL, submission guard, External product adapter and Codex CLI
are real. A small manager/plan facade supplies an already constructed adapter;
this is not the full AgentManager construction route, Web E2E or remote model.
"""
from __future__ import annotations

import asyncio
import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from openjiuwen.harness_protocol import AgentExecutionSpec, ExecutionAuthorization
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.governance.contracts import TrustedIdentity
from jiuwenswarm.governance.preparation import AlreadySubmitted
from jiuwenswarm.runtime import AgentRuntime
from jiuwenswarm.runtime.plan import PlanStateResult
from jiuwenswarm.runtime.session.model import SessionRequestDuplicateError
from jiuwenswarm.server.runtime.agent_adapter.engine_adapter import EngineAgentAdapter
from jiuwenswarm.server.runtime.session import project_store
from jiuwenswarm.server.runtime.session.project_access import ProjectAccessStore
from tests.system_tests.test_external_codex_product_route_local import _ResponsesFixture, _route

pytestmark = [pytest.mark.integration, pytest.mark.system]


@pytest.mark.asyncio
@pytest.mark.timeout(90)
async def test_governed_runtime_real_codex_revoke_accept_and_unknown(tmp_path, monkeypatch):
    pytest.importorskip("openai_codex", reason="Codex SDK and bundled real CLI required")
    root, home, codex_home = (tmp_path / name for name in ("project", "home", "codex-home"))
    for directory in (root, home, codex_home, codex_home / "skills"):
        directory.mkdir()
    monkeypatch.setenv("JIUWENSWARM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(project_store, "_projects_file", lambda: tmp_path / "projects.json")
    project = project_store.create_project("Governed CLI", str(root), "code")
    store = ProjectAccessStore()
    store.initialize(project.project_id, "owner")
    revision = store.replace_acl(project.project_id, "owner", acl={"member": ["execute"]}, expected_revision=1)
    # Reloading a separate instance must see the persisted grant.
    assert ProjectAccessStore().authorize(project.project_id, "member", "execute").allowed
    identity = TrustedIdentity("member", "local-test", "local-canary-host")
    request_count_at_drop = 0
    disposition = "revoke"
    adapter = None

    class Facade:
        async def process_message_stream(self, request):
            nonlocal request_count_at_drop
            request._execution_route = route
            adapter.select_execution_for_request(request)
            async for chunk in adapter.process_message_stream_impl(request, {"query": request.params["query"]}):
                if disposition != "drop_ack":
                    yield chunk
            if disposition == "drop_ack":
                request_count_at_drop = len(responses.requests)
                raise ConnectionError("canary dropped acknowledgement after real Provider execution")

    facade = Facade()
    manager = SimpleNamespace(
        begin_foreground_chat=AsyncMock(), end_foreground_chat=AsyncMock(),
        cleanup=AsyncMock(), cancel_all_inflight_work=AsyncMock(),
        cleanup_session_runtime=AsyncMock(return_value=True),
        get_agent_for_session_nowait=Mock(return_value=facade),
        pin_agent=Mock(), unpin_agent=Mock(),
    )
    runtime = AgentRuntime(
        agent_manager=manager, initializer=AsyncMock(),
        trusted_identity_resolver=lambda _: identity,
        # Deliberately use Runtime's default disk-backed authority, not a fake.
        plan_controller=SimpleNamespace(
            ensure_state=AsyncMock(return_value=PlanStateResult()),
            check_post_process_exit=AsyncMock(return_value=[]), reset_session=Mock(),
        ),
    )

    async def prepare(request, channel_id, **kwargs):
        nonlocal revision
        if disposition == "revoke":
            revision = store.replace_acl(project.project_id, "owner", acl={}, expected_revision=revision)
        return "code", "normal", facade

    runtime._prepare_chat_turn = prepare

    def request(request_id):
        return AgentRequest(
            request_id=request_id, channel_id="web", session_id="r1-a2-session",
            req_method=ReqMethod.CHAT_SEND, is_stream=True,
            params={"mode": "agent.code.normal", "work_mode": "code", "project_id": project.project_id,
                    "project_dir": str(root), "query": "Return R1-A2-PRODUCT-ROUTE-OK"},
            # Deliberately different from authenticated actor/subject.
            user_id="untrusted-wire-owner",
        )

    with _ResponsesFixture() as responses:
        spec = AgentExecutionSpec(
            "codex", "r1-13a-local", authorization=ExecutionAuthorization(full_access=True),
            provider_config={
                "inherit_process_env": False,
                "env": {"HOME": str(home), "CODEX_HOME": str(codex_home), "PATH": os.environ.get("PATH", "/usr/bin:/bin")},
                "model": {"model": "gpt-5.6-sol", "provider": "r1_13a_fixture", "api_base": responses.base_url, "api_key": "local-only"},
            },
        )
        route = _route(root, spec)
        adapter = EngineAgentAdapter(route)
        try:
            await adapter.create_instance(mode="code")
            async with asyncio.timeout(60):
                denied = [event async for event in runtime.stream(request("revoked"), trigger_hook=False)]
                assert any(not event.ok for event in denied)
                assert responses.requests == [], "revocation must stop submission before the real CLI sends"
                revision = store.replace_acl(project.project_id, "owner", acl={"member": ["execute"]}, expected_revision=revision)
                disposition = "allow"
                accepted = [event async for event in runtime.stream(request("accepted"), trigger_hook=False)]
                assert any(event.event_type == "chat.final" for event in accepted), accepted
                assert "R1-A2-PRODUCT-ROUTE-OK" in json.dumps([event.payload for event in accepted])
                assert len(responses.requests) == 1
                with pytest.raises(AlreadySubmitted, match="accepted"):
                    runtime._prepare_governed_request(request("accepted"))
                with pytest.raises(SessionRequestDuplicateError):
                    _ = [event async for event in runtime.stream(request("accepted"), trigger_hook=False)]
                assert len(responses.requests) == 1
                disposition = "drop_ack"
                unknown = [event async for event in runtime.stream(request("unknown"), trigger_hook=False)]
                assert any(not event.ok for event in unknown)
                assert request_count_at_drop == 2
                with pytest.raises(AlreadySubmitted, match="unknown"):
                    runtime._prepare_governed_request(request("unknown"))
                with pytest.raises(SessionRequestDuplicateError):
                    _ = [event async for event in runtime.stream(request("unknown"), trigger_hook=False)]
                assert len(responses.requests) == 2
        finally:
            await runtime.close()
            await adapter.cleanup()
