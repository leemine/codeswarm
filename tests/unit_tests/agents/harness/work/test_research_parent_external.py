# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""External Work research preserves the existing parent context and startup path."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from jiuwenswarm.server.runtime.agent_adapter.engine_adapter import EngineAgentAdapter
from jiuwenswarm.runtime.harness.context_bridge import build_external_context
from tests.unit_tests.runtime.harness.test_external_execution_route import _route


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["agent.work.normal", "agent.work.plan", "agent.code.normal"])
@pytest.mark.parametrize("mounted", [True, False])
async def test_research_does_not_change_parent_context_or_cold_start(tmp_path, mode, mounted):
    adapter = EngineAgentAdapter(_route(tmp_path))
    with patch.object(adapter, "_build_session", return_value=object()):
        await adapter.create_instance(mode=mode)
    adapter._subagent_runtime = object() if mounted else None
    binding = adapter._route.bound.binding
    expected = build_external_context(
        paths=adapter._route.runtime_paths,
        host_session_id=binding.host_session_id,
        channel_id=adapter._route.channel_id,
        provider_id=binding.provider_id,
        surface=adapter._surface,
        context_snapshot=adapter._context_snapshot,
    )
    assert adapter._external_context() == expected
    session = SimpleNamespace(
        started=False, binding=adapter._route.bound.binding, start=AsyncMock(),
    )
    with patch.object(adapter, "_compile_cold_surface_policy"), patch.object(
        adapter._projection, "replay_product_artifacts", new=AsyncMock(),
    ):
        await adapter._ensure_started(session)
    context = session.start.call_args.args[0]
    assert context == expected
    session.start.assert_awaited_once()
    assert "Work research parent acceptance" not in context.system_prompt
