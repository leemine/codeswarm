# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Work parent acceptance uses one parent-only context construction path."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from jiuwenswarm.agents.harness.work.research_parent import work_research_parent_instructions
from jiuwenswarm.server.runtime.agent_adapter.engine_adapter import EngineAgentAdapter
from tests.unit_tests.runtime.harness.test_external_execution_route import _route


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["agent.work.normal", "agent.work.plan", "agent.code.normal"])
@pytest.mark.parametrize("mounted", [True, False])
async def test_parent_policy_requires_work_and_mounted_research_at_start(tmp_path, mode, mounted):
    adapter = EngineAgentAdapter(_route(tmp_path))
    with patch.object(adapter, "_build_session", return_value=object()):
        await adapter.create_instance(mode=mode)
    adapter._subagent_runtime = object() if mounted else None
    expected = mode.startswith("agent.work.") and mounted
    policy = work_research_parent_instructions()
    assert (policy in adapter._external_context().system_prompt) is expected
    session = SimpleNamespace(
        started=False, binding=adapter._route.bound.binding, start=AsyncMock(),
    )
    with patch.object(adapter, "_compile_cold_surface_policy"), patch.object(
        adapter._projection, "replay_product_artifacts", new=AsyncMock(),
    ):
        await adapter._ensure_started(session)
    context = session.start.call_args.args[0]
    assert (policy in context.system_prompt) is expected
    assert context.system_prompt.count(policy) == int(expected)

