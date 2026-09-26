# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""R1-08: original Heartbeat tools share the admitted External parent gateway."""
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from openjiuwen.harness_protocol import AgentExecutionSpec, ToolInvocation

from jiuwenswarm.agents.harness.code.rails.heartbeat.tools import HEARTBEAT_TOOL_NAMES
from jiuwenswarm.common.runtime_workspace import RuntimeWorkspacePaths
from jiuwenswarm.runtime.harness.binding_store import ExecutionBindingStore
from jiuwenswarm.runtime.harness.config_source import ExecutionConfigSource
from jiuwenswarm.runtime.harness.request_binding import AdmittedExecutionRoute
from jiuwenswarm.server.runtime.agent_adapter.engine_adapter import EngineAgentAdapter


def _route(root: Path, provider: str, subject: str = "alice"):
    source = ExecutionConfigSource(explicit=AgentExecutionSpec(provider, "r1-08"))
    bindings = ExecutionBindingStore()
    bound = bindings.bind(source, subject_id=subject, host_session_id="parent", workspace=str(root))
    return AdmittedExecutionRoute(
        "web", source, bindings, bound, RuntimeWorkspacePaths(root, root, root, root)
    )


@pytest.mark.parametrize("provider", ["codex", "opencode"])
async def test_original_heartbeat_tools_share_parent_gateway_and_fixed_identity(tmp_path, provider):
    adapter = EngineAgentAdapter(_route(tmp_path, provider))
    service = AsyncMock()
    service.handle_operation.return_value = {"ok": True}
    adapter.set_heartbeat_service(service)
    try:
        await adapter.create_instance()
        gateway = adapter.execution_session._tool_gateway
        assert gateway is adapter._subagent_runtime.gateway
        names = {tool.name for tool in await gateway.definitions()}
        assert names == HEARTBEAT_TOOL_NAMES | {
            "subagent_spawn", "subagent_wait", "subagent_list", "subagent_send_input",
            "subagent_close", "subagent_resume",
            "get_current_goal", "submit_goal_report",
        }
        cases = [
            ("list_jobs", "list", {}),
            ("get_job", "get", {"job_id": "job"}),
            ("create_job", "create", {"name": "followup", "prompt": "check", "schedule": {"type": "interval", "interval_seconds": 60}}),
            ("update_job", "update", {"job_id": "job", "patch": {"enabled": False}}),
            ("delete_job", "delete", {"job_id": "job"}),
            ("toggle_job", "toggle", {"job_id": "job", "enabled": False}),
            ("preview_job", "preview", {"job_id": "job"}),
            ("run_now", "run_now", {"job_id": "job"}),
            ("cancel_run", "cancel", {"job_id": "job"}),
        ]
        for suffix, action, arguments in cases:
            result = await gateway.invoke(ToolInvocation(action, f"heartbeat_{suffix}", arguments))
            assert not result.is_error, result.content
            args, kwargs = service.handle_operation.call_args
            assert args[0] == action
            assert kwargs == {"channel_id": "web", "session_id": "parent", "user_id": "alice", "source": "agent_tool"}
        call_count = service.handle_operation.await_count
        forged = await gateway.invoke(ToolInvocation(
            "forged", "heartbeat_create_job", {
                "name": "bad", "prompt": "check", "schedule": {"type": "interval", "interval_seconds": 60},
                "session_id": "forged", "channel_id": "forged", "user_id": "mallory",
            },
        ))
        assert forged.is_error
        assert service.handle_operation.await_count == call_count
        # A service replacement updates captured original tools without creating
        # another MCP endpoint or replacing the parent gateway.
        replacement = AsyncMock()
        replacement.handle_operation.return_value = {"jobs": []}
        adapter.set_heartbeat_service(replacement)
        await gateway.invoke(ToolInvocation("list", "heartbeat_list_jobs", {}))
        replacement.handle_operation.assert_awaited_once()
        assert adapter.execution_session._tool_gateway is gateway
        adapter.set_heartbeat_service(None)
        assert (await gateway.invoke(ToolInvocation("disabled", "heartbeat_list_jobs", {}))).is_error
    finally:
        await adapter.cleanup()
    assert not adapter.has_session_runtime()


async def test_no_service_retains_subagent_and_goal_tool_catalog(tmp_path):
    adapter = EngineAgentAdapter(_route(tmp_path, "codex"))
    try:
        await adapter.create_instance()
        names = {tool.name for tool in await adapter.execution_session._tool_gateway.definitions()}
        assert names == {
            "subagent_spawn", "subagent_wait", "subagent_list", "subagent_send_input",
            "subagent_close", "subagent_resume", "get_current_goal", "submit_goal_report",
        }
        assert not names & HEARTBEAT_TOOL_NAMES
    finally:
        await adapter.cleanup()


async def test_gateway_uses_authoritative_store_and_rejects_other_session_owner(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import Mock

    from jiuwenswarm.agents.harness.code.rails.heartbeat import runtime as heartbeat_module
    from jiuwenswarm.agents.harness.code.rails.heartbeat.runtime import HeartbeatRailRuntime
    from jiuwenswarm.server.runtime.session import session_metadata

    monkeypatch.setattr(heartbeat_module, "get_heartbeat_jobs_path", lambda: tmp_path / "jobs.json")
    monkeypatch.setattr(session_metadata, "get_session_metadata", lambda *args, **kwargs: {"user_id": "alice"})
    manager = SimpleNamespace(get_agent_for_session_nowait=Mock(return_value=None), get_agent_nowait=Mock(return_value=None))
    server = SimpleNamespace(get_agent_manager=lambda: manager)
    service = HeartbeatRailRuntime(server)
    service._available = True
    adapter = EngineAgentAdapter(_route(tmp_path, "codex"))
    adapter.set_heartbeat_service(service)
    other = EngineAgentAdapter(_route(tmp_path, "codex", subject="bob"))
    other.set_heartbeat_service(service)
    try:
        await adapter.create_instance()
        await other.create_instance()
        gateway = adapter.execution_session._tool_gateway
        created = await gateway.invoke(ToolInvocation("create", "heartbeat_create_job", {
            "name": "followup", "prompt": "check", "schedule": {"type": "interval", "interval_seconds": 60},
        }))
        assert not created.is_error
        jobs = await service.store.list_jobs()
        assert len(jobs) == 1
        assert jobs[0].session_id == "parent"
        assert jobs[0].metadata["user_id"] == "alice"
        denied = await other.execution_session._tool_gateway.invoke(
            ToolInvocation("denied", "heartbeat_get_job", {"job_id": jobs[0].id})
        )
        assert denied.is_error
        assert "PermissionError" in denied.content
        # Cold reconstruction reads the same authoritative file; composing
        # tools alone does not dispatch a run or create another job.
        cold = HeartbeatRailRuntime(server)
        recovered = await cold.store.list_jobs()
        assert [job.to_dict() for job in recovered] == [job.to_dict() for job in jobs]
        assert not cold.execution.active_session_ids()
        deleted = await gateway.invoke(ToolInvocation("delete", "heartbeat_delete_job", {"job_id": jobs[0].id}))
        assert not deleted.is_error
        assert await service.store.list_jobs() == []
    finally:
        await service.stop()
        await adapter.cleanup()
        await other.cleanup()
