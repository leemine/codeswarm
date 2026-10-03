# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Work research policy composed with the existing core research agent."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from openjiuwen.harness.rails.base import DeepAgentRail
from openjiuwen.harness.rails.skills.skill_use_rail import SkillUseRail
from openjiuwen.harness.rails.sys_operation_rail import SysOperationRail
from openjiuwen.harness.subagents.research_agent import (
    build_research_agent_config as build_core_research_agent_config,
)

_RESEARCH_SKILLS = (
    Path(__file__).resolve().parents[3] / "resources" / "work_research" / "skills"
)


class _ResearchSysOperationRail(SysOperationRail):
    def fork_for_agent(self):
        return type(self)()


class _ResearchSkillRail(SkillUseRail):
    def __init__(self):
        super().__init__(skills_dir=str(_RESEARCH_SKILLS), include_tools=False)

    def fork_for_agent(self):
        return type(self)()


class _ResearchReviewRail(DeepAgentRail):
    """Register the pure review tool on each research child through existing APIs."""

    def __init__(self):
        super().__init__()
        self._tool = None

    def fork_for_agent(self):
        return type(self)()

    def init(self, agent):
        from jiuwenswarm.agents.harness.work.research_review import (
            build_research_review_tool,
        )

        self._tool = build_research_review_tool()
        agent.ability_manager.add_ability(self._tool.card, self._tool)

    def uninit(self, agent):
        if self._tool is not None:
            agent.ability_manager.remove_ability(self._tool.card.name)
            self._tool = None


def work_research_instructions() -> str:
    """Load the packaged policy, also exposed through Native SkillUseRail."""
    content = (_RESEARCH_SKILLS / "evidence-research" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    return content.split("---", 2)[2].strip()


def build_research_agent_config(model: Any, **kwargs: Any):
    """Preserve Native research tools, identity and iteration configuration."""
    # Preserve explicit rail/tool overrides; default research still uses core's
    # filesystem owner and adds only a pure research review capability.
    if "rails" not in kwargs:
        rails = [_ResearchSysOperationRail(), _ResearchSkillRail()]
        if kwargs.get("tools") is None:
            rails.append(_ResearchReviewRail())
        kwargs["rails"] = rails
    kwargs.setdefault("system_prompt", work_research_instructions())
    return build_core_research_agent_config(model, **kwargs)


__all__ = ["build_research_agent_config", "work_research_instructions"]
