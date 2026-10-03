# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Work parent acceptance guidance on existing subagent tools and Browser rail.

This is model-facing review policy, not a semantic verifier, approval grant or
second execution state machine. Reads, continuation and waiting remain owned by
the current tools, permissions, SubagentControl and execution budget.
"""
from __future__ import annotations

from openjiuwen.harness.prompts import PromptSection

from jiuwenswarm.agents.harness.common.rails.browser_task_prompt_rail import (
    BrowserTaskPromptRail,
)

_PARENT_SECTION = "work_research_parent_review"
_PARENT_POLICY_EN = """## Work research parent acceptance

Apply this after an authorized research_agent delegation; this policy does not
itself authorize a spawn. A completed child or structural_valid=true is a
candidate delivery, not accepted research.

1. After subagent_wait reports completion, use the existing read tools to inspect
   the saved report, evidence/claim table and review audit, then read the original
   cited sources yourself within the same admitted scope. Do not rely on the
   child's copied source text or its statement that it verified the report.
2. Check every factual clause, including Scope and Limitations, against the
   original source and its adjacent locator. A quotation matching one part of a
   compound claim does not support its other parts: comparisons need supporting
   spans for every compared alternative. Check full inspected ranges for omission
   claims, unknown versus absent, one record versus all runs, and exclusivity.
   Check the final artifact against the reviewed rendering and source mapping.
   Record your own claim-level acceptance or concrete defects; do not treat the
   child's structural review or completion message as your semantic acceptance.
3. If there is an evidenced defect and enough of the existing budget remains,
   send one targeted correction request with subagent_send_input to the exact
   existing child ID. Identify the claim/unsupported clause and the issue; require
   that child to reread the originals, revise its table, review/render again and
   rewrite/read back the artifacts. Never subagent_spawn a replacement or rewrite
   the child's report yourself to conceal a failed delivery. Use subagent_resume
   only if the existing tool status explicitly requires it for that same child.
   Use timeout_ms <= 45000 for each subagent_wait so the response fits the
   existing transport deadline; repeated waits while still running are not
   additional revisions. A failed child without artifacts is a failure to report,
   not a reason for blind send_input restarts.
4. Wait for that same child, then reread the changed report and original evidence
   and perform the same acceptance check. Allow at most one parent-requested
   revision for this delivery; the child's own bounded structural revision is
   separate. No new timeout, permission, source scope or iteration budget is granted.
   If the child fails, the defect remains, access is denied, continuation tools
   are unavailable, or time/iterations cannot cover revision and rechecking,
   report partial/unverified with the unresolved claims and artifact paths.
   Never call it accepted merely because files exist or execution completed.

Only after your source-based acceptance return the requested delivery. Report
execution completion separately from research quality when they differ.
"""
_PARENT_POLICY_CN = """## Work 研究父智能体验收

本规则只用于已获授权的 research_agent 委派，不自行授权 spawn。
子智能体 completed 或 structural_valid=true 只表示候选交付，不代表研究验收通过。

1. subagent_wait 返回完成后，父智能体使用现有读取工具检查报告、证据/声明表及审核记录，
   再在原授权范围内亲自读取被引用的原始来源。不能仅信子智能体复制的来源或自称已核验。
2. 对每个事实子句逐项核对原来源与邻接定位，包括 Scope 和 Limitations。
   引文支持复合陈述的一部分，不代表支持其余部分；比较必须有各被比较对象的对应证据。
   缺失声明需覆盖完整已读范围；核对未知与不存在、单条记录与全部实验、排他性推断。
   同时检查最终产物、被审核呈现及来源映射是否一致，记录父方逐声明结论或具体缺陷，
   不能把子方结构审核或完成消息当成父方语义验收。
3. 发现有证据的缺陷且现有预算足够时，仅用 subagent_send_input 向原 child ID
   发送一次定向返修，指出声明/不受支持的子句及问题，让原子智能体重读原资料、修改表、
   重新 review/render 并写入和读回产物。不得 subagent_spawn 替代实例，不得由父方偷改报告
   掩盖失败；仅当现有工具状态明确要求恢复时，才对同一 child 调 subagent_resume。
   每次 subagent_wait 的 timeout_ms <= 45000，为现有传输超时预留回复空间；
   仍在运行时重复等待不算返修。子方失败且无产物时应报告失败，不能盲目 send_input 重启。
4. 等待同一 child 后重新读取变更产物及原证据，执行同样验收。每次交付最多一次父方返修，
   与子方已有限次结构修订分开；不新增时间、权限、来源或迭代预算。子方失败、仍有缺陷、
   无读取权限、没有继续工具或预算不足以返修并复核时，明确报告 partial/unverified，列出
   未解决声明及产物路径，不能因文件存在或执行完成而声称验收通过。

仅在父方对原来源核验通过后交付所需结果；执行完成与研究质量不同时分别说明。
"""


def work_research_parent_instructions(language: str = "en") -> str:
    """Shared policy for Work Native and External parent composition."""
    return _PARENT_POLICY_CN if language in {"cn", "zh", "zh-CN"} else _PARENT_POLICY_EN


class WorkResearchTaskPromptRail(BrowserTaskPromptRail):
    """Keep Browser routing and mount review only for an actual research spec."""

    async def before_model_call(self, ctx) -> None:
        await super().before_model_call(ctx)
        builder = self.system_prompt_builder
        if builder is None:
            return
        subagents = getattr(getattr(ctx.agent, "deep_config", None), "subagents", None) or []
        has_research = any(self._extract_agent_meta(spec)[0] == "research_agent" for spec in subagents)
        if not has_research or not self.tools:
            builder.remove_section(_PARENT_SECTION)
            return
        builder.add_section(PromptSection(
            name=_PARENT_SECTION,
            content={"en": work_research_parent_instructions("en"), "cn": work_research_parent_instructions("cn")},
            priority=89,
            category="orchestration",
        ))

    def uninit(self, agent):
        if self.system_prompt_builder is not None:
            self.system_prompt_builder.remove_section(_PARENT_SECTION)
        super().uninit(agent)


__all__ = ["work_research_parent_instructions", "WorkResearchTaskPromptRail"]
