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
    assert isinstance(spec.rails[0], SysOperationRail)
    assert isinstance(spec.rails[1], SkillUseRail)


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
    ],
)
async def test_external_adapter_freezes_work_research_at_construction(tmp_path, mode, enabled):
    from jiuwenswarm.runtime.harness.surface import SurfaceAdmissionError
    from jiuwenswarm.server.runtime.agent_adapter.engine_adapter import (
        EngineAgentAdapter,
    )
    from tests.unit_tests.runtime.harness.test_external_execution_route import _route

    adapter = EngineAgentAdapter(_route(tmp_path))
    with patch.object(adapter, "_build_session", return_value=object()) as build:
        await adapter.create_instance(mode=mode)
        assert adapter._work_research_enabled is enabled
        # Repeated construction and a different product Surface both fail before
        # another Provider is allocated or the admitted research flag changes.
        with pytest.raises(RuntimeError, match="already exists"):
            await adapter.create_instance(mode=mode)
        with pytest.raises(SurfaceAdmissionError):
            await adapter.create_instance(mode="agent.code.normal" if enabled else "agent.work.normal")
        assert adapter._work_research_enabled is enabled
        build.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["team", "unknown"])
async def test_single_external_adapter_rejects_non_single_surface_before_allocation(tmp_path, mode):
    from jiuwenswarm.runtime.harness.surface import SurfaceAdmissionError
    from jiuwenswarm.server.runtime.agent_adapter.engine_adapter import EngineAgentAdapter
    from tests.unit_tests.runtime.harness.test_external_execution_route import _route

    adapter = EngineAgentAdapter(_route(tmp_path))
    with patch.object(adapter, "_build_session") as build:
        with pytest.raises(SurfaceAdmissionError):
            await adapter.create_instance(mode=mode)
        build.assert_not_called()
    assert adapter._work_research_enabled is False


def test_two_native_research_children_do_not_share_mutable_rails(tmp_path):
    from openjiuwen.harness import create_deep_agent
    from openjiuwen.harness.rails.skills.skill_use_rail import SkillUseRail
    from openjiuwen.harness.rails.sys_operation_rail import SysOperationRail

    from openjiuwen.core.sys_operation import (
        LocalWorkConfig,
        OperationMode,
        SysOperation,
        SysOperationCard,
    )

    model = MagicMock()
    operation = SysOperation(
        SysOperationCard(
            id="research-fork",
            mode=OperationMode.LOCAL,
            work_config=LocalWorkConfig(),
        )
    )
    spec = build_research_agent_config(
        model, workspace=str(tmp_path), sys_operation=operation
    )
    parent = create_deep_agent(
        model=model,
        workspace=str(tmp_path),
        sys_operation=operation,
        subagents=[spec],
    )
    first = parent.create_subagent("research_agent", "first")
    second = parent.create_subagent("research_agent", "second")
    for rail_type in (SkillUseRail, SysOperationRail):
        first_rail = next(r for r in first._pending_rails if isinstance(r, rail_type))
        second_rail = next(r for r in second._pending_rails if isinstance(r, rail_type))
        template = next(r for r in spec.rails if isinstance(r, rail_type))
        assert first_rail is not second_rail
        assert first_rail is not template
        assert second_rail is not template
    first_skill = next(r for r in first._pending_rails if isinstance(r, SkillUseRail))
    second_skill = next(r for r in second._pending_rails if isinstance(r, SkillUseRail))
    first_skill.skills.append(object())
    assert second_skill.skills == []
    assert first.deep_config.sys_operation is operation
    assert second.deep_config.sys_operation is operation
