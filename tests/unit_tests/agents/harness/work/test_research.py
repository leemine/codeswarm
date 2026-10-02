# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Work profile composition retains Native research and its boundary owners."""

from unittest.mock import MagicMock, patch

import pytest

from jiuwenswarm.agents.harness.work.research import (
    build_research_agent_config,
    work_research_instructions,
)


def test_native_work_research_preserves_factory_and_sys_operation(tmp_path):
    from openjiuwen.harness.rails.skills.skill_use_rail import SkillUseRail
    from openjiuwen.harness.rails.sys_operation_rail import SysOperationRail
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
        JiuWenSwarmDeepAdapter,
    )

    model, operation = MagicMock(), MagicMock()
    adapter = JiuWenSwarmDeepAdapter()
    adapter._workspace_dir = str(tmp_path)
    adapter._sys_operation = operation
    with patch.object(adapter, "_browser_runtime_enabled", return_value=False):
        specs, _ = adapter._build_configured_subagents(
            model,
            {"subagents": {"research_agent": {"enabled": True, "max_iterations": 7}}},
            {},
        )
    spec = next(s for s in specs if s.agent_card.name == "research_agent")
    assert spec.factory_name == "research_agent"
    assert spec.model is model
    assert spec.sys_operation is operation
    assert spec.workspace == str(tmp_path)
    assert spec.max_iterations == 7
    assert spec.tools is None  # Core owns tools; no second tool implementation.
    assert spec.system_prompt == work_research_instructions()
    assert [type(r) for r in spec.rails] == [SysOperationRail, SkillUseRail]


def test_explicit_native_overrides_are_preserved():
    rails = []
    spec = build_research_agent_config(MagicMock(), rails=rails, system_prompt="custom")
    assert spec.rails is rails
    assert spec.system_prompt == "custom"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode, enabled",
    [
        ("agent", True),
        ("agent.work.normal", True),
        ("agent.work.plan", True),
        ("code", False),
        ("agent.code.normal", False),
        ("agent.code.plan", False),
        ("team", False),
        ("unknown", False),
    ],
)
async def test_external_adapter_freezes_work_research_at_construction(mode, enabled):
    from types import SimpleNamespace
    from jiuwenswarm.server.runtime.agent_adapter.engine_adapter import (
        EngineAgentAdapter,
    )

    route = SimpleNamespace(
        provider_id="codex",
        bound=SimpleNamespace(binding=SimpleNamespace(host_session_id="parent")),
    )
    adapter = EngineAgentAdapter(route)
    with patch.object(adapter, "_build_session", return_value=object()):
        await adapter.create_instance(mode=mode)
        assert adapter._work_research_enabled is enabled
        with pytest.raises(RuntimeError, match="already exists"):
            await adapter.create_instance(mode="agent.code.normal")
        assert adapter._work_research_enabled is enabled
