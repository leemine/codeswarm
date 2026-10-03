# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Actual Native rail assembly keeps Browser while Work adds parent acceptance."""
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.core.single_agent.schema.agent_card import AgentCard
from openjiuwen.harness.prompts import SystemPromptBuilder
from openjiuwen.harness.schema import SubAgentConfig

from jiuwenswarm.agents.harness.common.rails.browser_task_prompt_rail import BrowserTaskPromptRail
from jiuwenswarm.agents.harness.work.research import build_research_agent_config
from jiuwenswarm.agents.harness.work.research_parent import (
    WorkResearchTaskPromptRail,
    work_research_parent_instructions,
)


def _parent(*, research=True, browser=True):
    specs = []
    if research:
        specs.append(build_research_agent_config(MagicMock()))
    if browser:
        specs.append(SubAgentConfig(agent_card=AgentCard(name="browser_agent"), system_prompt="browser"))
    return SimpleNamespace(
        card=AgentCard(name="parent"),
        deep_config=SimpleNamespace(subagents=specs),
        system_prompt_builder=SystemPromptBuilder(language="en"),
        ability_manager=MagicMock(),
    )


def _context(parent):
    return AgentCallbackContext(
        agent=parent, inputs=None,
        session=SimpleNamespace(get_session_id=lambda: "research-parent-fixture"), extra={},
    )


@pytest.mark.asyncio
async def test_actual_parent_rail_preserves_browser_tools_and_research_review():
    parent = _parent()
    rail = WorkResearchTaskPromptRail(enable_subagent_runtime=True)
    rail.init(parent)
    try:
        await rail.before_model_call(_context(parent))
        by_name = {tool.card.name: tool for tool in rail.tools}
        assert by_name["task_tool"]._allowed_subagent_types == frozenset({"browser_agent"})
        assert by_name["subagent_spawn"]._allowed_subagent_types == frozenset({"research_agent"})
        assert {"subagent_wait", "subagent_send_input", "subagent_resume"} <= set(by_name)
        browser = parent.system_prompt_builder.get_section("task_tool")
        assert "Browser Capability Routing Rules" in browser.content["en"]
        policy = parent.system_prompt_builder.get_section("work_research_parent_review")
        assert policy.content["en"] == work_research_parent_instructions()
        assert "at most one parent-requested" in policy.content["en"]
        assert "exact\n   existing child ID" in policy.content["en"]
        assert "original\n   cited sources yourself" in policy.content["en"]
        assert "structural_valid=true" in policy.content["en"]
        assert "partial/unverified" in policy.content["en"]
    finally:
        rail.uninit(parent)
    assert not parent.system_prompt_builder.has_section("work_research_parent_review")


@pytest.mark.asyncio
async def test_research_policy_is_load_aware_and_removed_when_spec_is_unloaded():
    parent = _parent()
    rail = WorkResearchTaskPromptRail(enable_subagent_runtime=True)
    rail.init(parent)
    try:
        await rail.before_model_call(_context(parent))
        assert parent.system_prompt_builder.has_section("work_research_parent_review")
        parent.deep_config.subagents = [spec for spec in parent.deep_config.subagents if spec.agent_card.name == "browser_agent"]
        await rail.before_model_call(_context(parent))
        assert not parent.system_prompt_builder.has_section("work_research_parent_review")
        assert "Browser Capability Routing Rules" in parent.system_prompt_builder.get_section("task_tool").content["en"]
    finally:
        rail.uninit(parent)


@pytest.mark.asyncio
async def test_absent_research_never_injects_parent_review():
    parent = _parent(research=False)
    rail = WorkResearchTaskPromptRail(enable_subagent_runtime=True)
    rail.init(parent)
    try:
        await rail.before_model_call(_context(parent))
        assert not parent.system_prompt_builder.has_section("work_research_parent_review")
    finally:
        rail.uninit(parent)


def test_production_work_and_code_assembly_use_explicit_work_gate(monkeypatch):
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter
    from jiuwenswarm.server.runtime.agent_adapter.interface_code import JiuwenSwarmCodeAdapter

    config = {"react": {"subagent_runtime": {"enabled": True}}}
    for adapter_class, expected in [
        (JiuWenSwarmDeepAdapter, WorkResearchTaskPromptRail),
        (JiuwenSwarmCodeAdapter, BrowserTaskPromptRail),
    ]:
        adapter = adapter_class()
        monkeypatch.setattr(adapter, "_auto_permission_enabled_for_config", lambda *args, **kwargs: False)
        monkeypatch.setattr(adapter, "_permission_interrupt_rail_infos", lambda *args: [])
        # Exercise the real production rail recipe and its real subagent builder,
        # while avoiding unrelated filesystem/model/permission rail setup.
        def build_only_subagent(infos, _config):
            info = next(info for info in infos if info.attr_name == "_subagent_rail")
            return [info.build_func(**(info.params or {}))]
        monkeypatch.setattr(adapter, "_instantiate_rails", build_only_subagent)
        rail, = adapter._build_agent_rails({}, config)
        assert type(rail) is expected
        assert rail.enable_subagent_runtime is True
        # Defaults in the shared builder must never imply a Work Surface.
        assert type(adapter._build_subagent_rail(config)) is BrowserTaskPromptRail
