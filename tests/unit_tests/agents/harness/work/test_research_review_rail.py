# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Native research review uses existing ability ownership and explicit overrides."""
from unittest.mock import MagicMock

import pytest

from jiuwenswarm.agents.harness.work.research import (
    _ResearchReviewRail,
    build_research_agent_config,
)
from jiuwenswarm.agents.harness.work.research_review import build_research_review_tool


def test_default_research_adds_forkable_owned_review_without_replacing_core_tools():
    spec = build_research_agent_config(MagicMock())
    assert spec.tools is None
    original = next(rail for rail in spec.rails if isinstance(rail, _ResearchReviewRail))
    first, second = original.fork_for_agent(), original.fork_for_agent()
    first_agent, second_agent = MagicMock(), MagicMock()
    first.init(first_agent)
    second.init(second_agent)
    first_tool = first_agent.ability_manager.add_ability.call_args.args[1]
    second_tool = second_agent.ability_manager.add_ability.call_args.args[1]
    assert first_tool is not second_tool
    assert first_tool.card.name == second_tool.card.name == "review_research_report"
    first.uninit(first_agent)
    first.uninit(first_agent)
    first_agent.ability_manager.remove_ability.assert_called_once_with("review_research_report")
    second_agent.ability_manager.remove_ability.assert_not_called()
    assert second._tool is second_tool
    assert original._tool is None


def test_explicit_native_rails_and_tools_keep_their_override_contract():
    rails = []
    spec = build_research_agent_config(MagicMock(), rails=rails)
    assert spec.rails is rails
    custom_tools = []
    spec = build_research_agent_config(MagicMock(), tools=custom_tools)
    assert spec.tools == custom_tools
    assert not any(isinstance(rail, _ResearchReviewRail) for rail in spec.rails)


@pytest.mark.asyncio
async def test_real_local_function_invocation_preserves_data_and_returns_cited_rendering():
    tool = build_research_review_tool()
    output = await tool.invoke({
        "sources": [{"id": "source.txt", "text": "Measured 4 items.\n", "start_line": 1, "complete": True}],
        "claims": [{
            "id": "measurement", "section": "Findings", "kind": "fact", "text": "Measured 4 items.",
            "refs": [{"source_id": "source.txt", "start_line": 1, "end_line": 1, "quote": "Measured 4 items."}],
        }],
    })
    assert output["structural_valid"] is True
    assert "Measured 4 items (source.txt:1)." in output["rendered_markdown"]
    assert output["input_fingerprint"]
