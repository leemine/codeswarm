# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Packaged research skills obey the actual Native filesystem sandbox."""

import asyncio
from unittest.mock import MagicMock

import pytest

from openjiuwen.core.sys_operation import (
    LocalWorkConfig,
    OperationMode,
    SysOperation,
    SysOperationCard,
)
from openjiuwen.harness.rails.skills.skill_use_rail import SkillUseRail
from jiuwenswarm.agents.harness.work.research import (
    _RESEARCH_SKILLS,
    build_research_agent_config,
    work_research_instructions,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("admit_packaged_skill", [False, True])
async def test_research_skill_reads_respect_explicit_roots(
    tmp_path, admit_packaged_skill
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("unrelated private marker")
    roots = [str(workspace)]
    if admit_packaged_skill:
        roots.append(str(_RESEARCH_SKILLS))
    operation = SysOperation(
        SysOperationCard(
            id=f"research-skill-access-{admit_packaged_skill}",
            mode=OperationMode.LOCAL,
            work_config=LocalWorkConfig(
                sandbox_root=list(roots), restrict_to_sandbox=True
            ),
        )
    )
    spec = build_research_agent_config(
        MagicMock(), workspace=str(workspace), sys_operation=operation
    )
    rail = next(rail for rail in spec.rails if isinstance(rail, SkillUseRail))
    rail.set_sys_operation(operation)
    skill = _RESEARCH_SKILLS / "evidence-research" / "SKILL.md"
    async with asyncio.timeout(10):
        if admit_packaged_skill:
            text = await rail._read_skill_text(skill)
            assert work_research_instructions() in text
        else:
            with pytest.raises(FileNotFoundError):
                await rail._read_skill_text(skill)
        with pytest.raises(FileNotFoundError):
            await rail._read_skill_text(outside)
    # Missing package access keeps the inline policy, not an implicit permission grant.
    assert spec.system_prompt == work_research_instructions()
    assert spec.sys_operation is operation
    assert operation._run_config.sandbox_root == roots
