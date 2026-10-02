# R1-12 Work 研究流程实施与验证

2026-10-02。任务状态以管理仓 DEVELOPMENT_TASK_TRACKER.md 为准；本记录仅陈述本地实现和证据，不代表远端发布。

## 代表流程与边界

Work 中委派一个研究问题：界定问题和比较维度 → 通过既有工具检查资料 → 比较事实、推论、冲突与缺口 → 交付带相邻引用、来源定位和局限的报告，文件交付须回读。用户提供的资料不产生额外授权；候选研究结果不自动分享、确权或进入共享知识。

政策位于打包资源 `resources/work_research/skills/evidence-research/SKILL.md`。Native 的 Work adapter 继续显式启用原 research_agent，保留 core factory、父模型、SysOperation 和 max_iterations，组合原 SysOperationRail / SkillUseRail。External 在宿主创建时冻结 Work mode，由原六项产品子 Agent 工具广告研究 profile，仍由原 same-provider factory / Binding / Session / 恢复与清理运行；给研究 child 注入同一资源正文。旧 Code generic research alias 保留，不注入新 Work 流程。浏览器、general-purpose 和自定义代理不受此政策注入影响。

没有新增研究调度器、协议、Session 状态机、模型服务商选择或共享项目数据存储。没有修改依赖锁。来源是工作区用户已提供的普通文件；共享项目资料的准入由既有/本轮治理边界负责，研究政策不是授权实现。Native Skill 文件读取继续遵守父 SysOperation；受限环境不得因为技能位于包目录而扩大文件权限。

## 来源与回归

基线 swarm `78c4529719932b785437747d0c2794d429ec4440`；实现工作树 `artifacts/r1-three-lines/research`。首批本地源码回归使用主 `.venv`，实际 core 为 editable `9b41889a…`，不能作为锁来源验收。正式本地验证使用 `/tmp/r1-three-locked-venv/bin/python`，实际 core 为非 editable site-packages、锁定 `7cc1dfa9a1fc2ed130dae92fb586b9a7332b3f9a`，只以 PYTHONPATH 指向 swarm 工作树，未引入本地 core。

- Native Work profile、External catalog/child binding/生命周期、原六工具/历史解析、Native SysOperation 继承和系统提示结构：131 passed（本地 editable 6.47s；锁定来源 9.41s）。
- External Adapter/Goal/Heartbeat 受影响回归：54 passed（锁定来源，5.77s）。
- 新增专属测试 `tests/unit_tests/agents/harness/work/test_research.py`，扩展 `test_external_subagent_profiles.py`、`test_external_subagent_execution.py`；没有用 RSI 测试替代研究验收。
- 最终版本合并运行上述范围：185 passed（锁定来源，6.55s），0 failed/error/skipped。
- 工程检查：受影响新增模块 ruff check、git diff --check。

确定性命令使用 `JIUWENSWARM_DATA_DIR=<isolated-dir> JIUWENSWARM_CONFIG_URL=off PYTHONPATH=<worktree> timeout 180 /tmp/r1-three-locked-venv/bin/python -m pytest --no-cov <listed-tests> -q --tb=short --junitxml=<evidence>`。

最初未设隔离数据目录的收集因用户日志目录只读而失败，随后改用隔离目录；干净环境第三方 pysbd 冷字节码在 warnings=error 下收集失败，按既有 CI 方式在 pytest 外预编译后通过，没有改源码/警告白名单。沙箱内现有文件锁用例卡住，超时结束；本机权限下同范围复跑通过。上述尝试不记作成功测试。

## 真实验证与产物质量

专属可选入口为 `tests/system_tests/test_work_research_remote.py`，环境开关 `RUN_WORK_RESEARCH_REMOTE=1`、`WORK_RESEARCH_API_BASE/API_KEY/MODEL`，可通过 `WORK_RESEARCH_EVIDENCE_DIR` 留存脱敏产物。采用真实 glm-5.2 远端模型，两个本地人造资料文件包含 42s/31s、网络要求差异和不可直接比较的条件；它们用于验证资料处理，不能作为现实性能结论。Codex 使用真实 CLI 和产品 EngineAgentAdapter/MCP 六工具；Native 使用产品 Work research spec 和原 core DeepAgent/SubagentControl，实际读取 evidence-research Skill。初始执行上限 240 秒；发现父执行收尾超时后，最终测试设执行上限 300 秒、清理上限 30 秒、外层超时 390 秒；不把 fake endpoint 计入真实远端结果。

初轮 Codex 1 passed（121.01s），Native 1 passed（119.45s）；Native 实际调用包含 subagent_spawn、subagent_wait、list_skill、read_file、write_file 与回读。Codex 父子调用及最终产物路径经原事件投影返回。人工复核发现首轮 Codex 把“未测网络需求”局部过度推断成不存在网络依赖，因此补充“未验证仍未知、局部观察不证明系统级独立性”的政策后重新验证。研究政策是模型行为要求，不是形式化证据正确性保证。

第二轮复跑：Codex 1 passed（97.40s），Native 1 passed（173.28s），各另有另一 Provider 用例 1 deselected（按 Provider 分开运行）。两者是执行/工具/基础产物断言通过，不是全面质量门禁通过。Native 最终实际调用为 subagent_spawn、subagent_wait、list_skill、read_file、glob、list_files、write_file、read_file，并停止该准确 session 的所有子执行。最终人工复核：Native 报告正确列出未测试网络需求与不可控比较的局限；Codex 报告仍把 source-b 网络需求定位到 line 3（实际 line 4），且部分表述过度推论。这次已知质量失败不是缺少环境条件，也不能以文件存在或数值出现推导质量验收完成。随后又收紧政策：未测只能作为 unknown，优先可核对短原文，行号必须实读核验，交付前逐项读回核对主张与引用。真测试增加 canary 专属 evidence JSON（模型自主提取而非提示正确答案）断言：A 网络结论必须 unknown；B 仅陈述 observed run requirement；精确原文必须存在于声明行；报告不得出现已发现的未测→无依赖转换。负例复查确认新增 gate 拒绝上一轮真实错误报告、unknown→absent 与 line 3 误引。源样本与各轮原报告保留，第三轮真实复验两报告都得到正确的 unknown / observed requirement 与原文行号，但测试 prompt 没明确 line 字段，模型使用 line_number 导致两个 pytest KeyError failure；修正 schema 后对原产物重放通过。第四轮 Native 在报告后段已解释 unknown，但 canary 要求每个重复原文段都重复 unknown，导致过严断言 failure；Codex 已完成 child 报告与 evidence JSON，父执行收尾超过 240 秒而 timeout failure。修正 checker 只要求存在对应引用的 unknown 解释（JSON 仍强制 unknown、原文与行号严格验证），原始未改产物重放双通过。这些 live 失败不改记成功；最终完整 live 待主集成树按 300 秒执行上限复验。

最后源码复核还修复了 Native 并行研究 child 的可变 rail 复用：Work 私有薄子类实现既有 fork_for_agent 接缝，core 每个 child 取得独立 SysOperationRail / SkillUseRail，父 SysOperation 权限边界保持同一。真实 core.create_subagent 两 child 的隔离/缓存回归通过；最终该专项与 External child 范围 35 passed（5.20s），最终政策专项 52 passed（5.25s）。实现提交 fb238e9e，修正提交 4246a245，均仅本地。

交付证据位于管理工作区 `artifacts/r1-three-lines/research-evidence/final-package/`，仅含两 Provider 原始报告、证据 JSON、源资料和结果/hash 统计，不含配置/环境/凭据。最终原始测试 log/XML 位于 `/tmp/r1-12-evidence/`（final-unit、final-policy、fork-final、remote、native；schema-failure另存 quality-schema-*）；日志已替换实际凭据，未纳入源码提交。最初与最终尝试分开保留。Native 覆盖产品研究 spec + 原子代理运行时，Codex 覆盖产品 EngineAgentAdapter；本测试不宣称完整 WebSocket/Gateway CLI、浏览器刷新/恢复或 Team 通道矩阵成功。OpenCode 仅确定性同引擎测试；其真实远端、Claude Code/DSH、受限 Native 包技能访问及全面引用质量评估另需验证。本轮未做远端推送或合并。
