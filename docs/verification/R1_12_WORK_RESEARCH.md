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

## 2026-10-03 闭合复验

本轮从已集成 swarm `2df3fa4a35e6c60ed7e4360306c43e494a0bf185` 的干净工作树创建 `codex/r1-12-closure-20261003`；仅改研究专项测试及本记录，生产研究政策/工厂与公共 Runtime、协议、Session、engine、锁保持不变。使用 `/tmp/r1-three-locked-venv/bin/python`，实际 core 非 editable `7cc1dfa9a1fc2ed130dae92fb586b9a7332b3f9a`，仅 swarm 工作树在 PYTHONPATH。真实模型仍为已授权 glm-5.2，输入只有本任务两份临时四行人工资料，没有用户资料；各 Provider 串行运行，HOME/data/tmp/Provider runtime 隔离，凭据只经内存和环境传递，证据包不包含配置、环境或凭据。

这次没有仅调大超时：将问题明确限定为两次记录的检索运行，要求报告最多 300 词、child 最后回复最多 40 词、parent 仅委派/等待/返回路径；Native 模型上限 4096 tokens，关闭本夹具无关的图片能力探测。保留真实读源、Skill、报告/JSON 读回及未知/观察范围/精确引用行号 gate。任务总预算 360 秒，清理 30 秒，外层 430 秒。Codex idle 100 秒且不自动重试，单次 child wait 60 秒并仅继续同一个 ID，避免合法 MCP 等待触发 idle；Native 没有该 Codex idle 限制，单次 wait 240 秒。增加测试专用分阶段时间线，不引入产品调度器。异常仅记录类型和既有非秘密错误码，失败/清理失败不写成通过。

- Native 完整 live：1 passed / 2 deselected，149.81 秒。真实 parent → 原 research_agent → list_skill/Skill 原文及来源 read_file → write_file → 输出 read_file → parent 返回路径 → 清理。质量检查通过：A unknown，B required_in_observed_run，两条精确短原文均在原文件第 4 行，报告 268 词并保留比较局限。时间线：23.115 秒 spawn；81.212 秒观测到两产物；84.357 秒完成输出读回；128.022 秒 child wait 返回；146.338 秒 parent 结束；146.369 秒清理结束。模型调用占主要耗时，文件工具毫秒级；child 最后一次模型调用约 43.65 秒，parent 收尾约 18.32 秒。parent 额外做了 glob 路径检查，未再次读取源文件。
- Codex 完整 live：1 passed / 2 deselected，100.93 秒。真实产品 EngineAgentAdapter/MCP 子工具与相同 Codex Provider child；报告 261 词，严格质量 gate、parent completed 与输出路径断言、清理均通过。时间线：11.657 秒 spawn；58.238 秒两产物；第一次 wait 在 78.200 秒返回，第二次在 91.398 秒返回；96.163 秒 parent completed，96.438 秒清理结束。人工读报告核对来源/局限与 JSON 一致。
- 受限 Native Skill 使用真实 SysOperation，不以 mock 代替文件权限：仅 workspace root 时包目录读取被拒绝，inline policy 保留；host 显式准入 workspace + 打包 Skill root 后可以读取 Skill，无关外部临时文件在两种配置下均被拒绝，父 SysOperation/根列表未被扩大。此处是路径准入验证，不声称底层根列表提供只读授权。新增 `tests/unit_tests/agents/harness/work/test_research_skill_access.py` 两分支，与工厂专项合跑 13 passed（5.80 秒）。
- 受影响回归：研究工厂、实际 Skill 边界、External profile/执行和远端用例收集，共 55 passed / 3 skipped（远端 opt-in 关闭）/ 1 既有 AuthlibDeprecationWarning，5.59 秒。未改警告白名单。ruff 与 diff whitespace 检查通过。

运行入口：隔离环境中 `RUN_WORK_RESEARCH_REMOTE=1 ... /tmp/r1-three-locked-venv/bin/python -m pytest --no-cov -q tests/system_tests/test_work_research_remote.py -k <native|codex|opencode> --tb=short --junitxml=<output>/run.xml`。可重现配置由测试中的 Provider 设置与环境参数定义；实际密钥不写入命令。受影响命令：同解释器 `-m pytest --no-cov -q tests/unit_tests/agents/harness/work/test_research.py tests/unit_tests/agents/harness/work/test_research_skill_access.py tests/unit_tests/runtime/harness/test_external_subagent_profiles.py tests/unit_tests/runtime/harness/test_external_subagent_execution.py tests/system_tests/test_work_research_remote.py --tb=short --junitxml=/tmp/r1-12-closure/affected.xml`（远端开关关闭）。原始 log/XML 与脱敏时间线位于 `/tmp/r1-12-closure/`，旧轮失败记录不改写为成功。

OpenCode 最小真实同引擎场景也通过：1 passed / 2 deselected，68.34 秒；实际 EngineAgentAdapter/MCP → OpenCode research child → 两份来源/报告/JSON → parent completed → 清理，严格质量 gate 通过。spawn 16.310 秒，报告 47.985 秒、JSON 48.989 秒可见，child wait 60.710 秒返回，parent 64.827 秒完成，65.183 秒清理完成。CLI 的 SHA256 与锁 core 指定值 `bb71f45b564f9234a97f54d6252a4a41d2f4388ae4b078918f691824cc3b3e54` 一致；本机非 root、cgroup v2、systemd 用户服务可用。首轮因测试未创建 Provider 要求的 0700 runtime 根目录而在模型调用前失败（1 failed，3.54 秒），补齐测试 setup 后重新通过；首轮保存在 `opencode-initial-startup-failure`，不计为环境缺失或成功。

Native 成功调用时与最终源码均为 wait 240000ms；曾在编辑期间短暂改为 60000ms，但未以此启动 Native live，随后恢复，避免把未运行配置冒充成功。Codex/OpenCode 为 60000ms。后续测试改动仅为 OpenCode 私有目录 setup、非秘密异常错误码记录与说明；Native/Codex 成功路径不变。主集成树的最终受影响回归仍应按新的集成 SHA 留证，不把本分支结果冒称另一 SHA 的成功。

本轮闭合的是三种已运行 Provider 的代表性研究流程与受限 Native Skill 文件访问边界；不是 Claude Code/DSH、完整渠道/UI 恢复矩阵或任意研究内容的普遍语义正确性保证。旧轮已知质量失败保留；当前三份报告的断言与人工核对均通过。没有推送、远端合并或依赖升级。

### 最终集成复验的失败与窄修（覆盖上文的完成结论）

集成 `07b035c2` 的 Native live 失败（162.28 秒）；不得把前一分支成功沿用为该 SHA 成功。日志第 276 行显示 child 第 3 次模型调用 `content_len=0, tool_call_count=0, output_tokens=4096`，恰好耗尽测试配置的 token cap；没有 write_file。core 将无工具的空响应结束为 completed，父收到空 result 后调用 glob/list_files，最后用 bash `find /` 寻找产物，父 6 次迭代耗尽。直接根因是模型预算耗尽后的空交付与过宽的测试工具/文件权限；不是 child 达到 12 次迭代，也不是缺少远端条件。这次真实失败保留在 `/tmp/r1-closure/research-integrated/native/run.log`，旧成功不能覆盖它。

仅收紧本次 Native canary：父沿用原六项 subagent 工具，移除其 SysOperationRail；真实运行到模型调用前的工具清单断言确认为六项，没有 bash/read_file/glob。child 与 parent 共享真实 SysOperation，其 fs roots 明确限定本次 workspace 与打包 Skill，restrict_to_sandbox=True。锁 core 的空 shell_allowlist 实际表示不限制，第一轮拒绝负例因此失败；已改用既有 host dangerous_patterns hook 拒绝所有非空 shell，并以无害 printf 的真实拒绝结果验证，不修改公共权限实现。root 路径准入不等于只读授权。测试监控检查 child wait 的真实结构：failed/cancelled、空 completed 或缺产物均记录 canary failure，并用既有 rail 终止接缝结束父模型循环；这不是修改生产错误码或补造成功结果。最终还必须断言父 result_type=answer、真实产物和质量 gate。模型 cap 改为有界 8192，保留 360 秒总预算/90 秒单模型请求预算，并记录实际 output_tokens/finish_reason；是否能完成须由后续最终集成 live 判定，本增量未调用远端。

同时撤销此前“OpenCode 原报告全部引用人工核对正确/三份报告质量均通过”的结论。旧 OpenCode 报告 Findings 首段把“all indexed locally”连同检索耗时引用为 `[source-a.md:3]`，本地索引事实实际在第 4 行；旧 gate 只检查 network ledger，漏过该复合事实。原报告不改写；新增 canary 检查本地索引断言的相邻定位必须包含第 4 行，原始真实报告作为固定反例，L3-L4 引用作为正例。原执行/父终结/清理成功仍成立，质量验收追溯改记失败。新增 `test_research_canary_guards.py` 覆盖真实误引、失败/空结果/缺产物、实际父工具表面、真实 shell 与 fs 拒绝。未来报告仍须人工核对；此夹具检查不是通用语义评测器，不以手工更正旧报告替代新 live。

该窄修定向回归结果：60 passed / 3 skipped（远端未启用）/ 1 既有弃用警告，5.43 秒；日志与 XML 为 `/tmp/r1-12-closure/guards-final.{log,xml}`。第一轮空 allowlist 拒绝负例为 1 failed / 4 passed，14.74 秒，保留 `/tmp/r1-12-closure/guards.{log,xml}`；这是测试配置预期与既有 API 语义不符，不在本轮宣称或修复 core 缺陷。最终远端验证由主集成树运行；本增量目前状态为待真实验证。

### 引用语法与提示契约修正

集成 `86e79bb3` 的 Native 复验完成产物生成、读回、父 answer 和清理，但 pytest 在 132.48 秒失败：报告网络事实使用合法的 `[source-a.md, L4]`，旧 regex 不识别逗号加 L；索引事实自身只附 `[source-a.md]`，随后一句的网络引用不能倒借给它。原任务仅要求 adjacent citations，而新增 gate 要求每个索引事实精确行号，二者契约不一致。本次失败记为提示/gate 契约与语法不匹配，不记为模型明确误引第 3 行，也不改成 live 通过。

窄修在代表任务中明确每项事实后给文件名及精确行定位；合句包含不同行的事实时分引或给覆盖范围，行号必须从原文读取，不把正确答案写进提示。仅针对本次已知“indexed locally”事实，gate 检查它之后最近的 source-a 引用，支持 `file:4`、`file:L4`、`file, L4`、`file L4`、`file line 4` 及 `3-4`/`L3-L4` 范围；不跳过缺行号/错误定位去借下一句的正确引用。保留旧 OpenCode 原始误引反例，增加 9 种语法正例和跨句/无定位/超出真实来源范围反例，不扩展为通用语义解析器。新增要求不追溯冒称旧报告满足，也不以手工改报告替代 live；本增量没有远端调用，由最终集成树重新验收。

引用窄修最终定向回归：73 passed / 3 skipped（远端关闭）/ 1 既有弃用警告，5.56 秒；`/tmp/r1-12-closure/citation-final.{log,xml}`。ruff/diff whitespace 检查通过，真实验证状态仍为待最终集成复验。

### 忠实改写与缺失信息的证据边界

`3f4ecb1c` 的 Native live 已由主线通过并独立人工核对。该版本 Codex 在 95.12 秒失败：报告忠实改写“Pilot A did not test any network requirement…unknown…not an absent dependency”，JSON 仍保留正确精确原文与第 4 行，旧报告 gate 却要求出现逐字原句，属于 checker 契约误拒。现允许报告忠实改写，精确短原文仍由 JSON 强制校验；保留 unknown 改为 absent/not required 的拒绝负例与原始 Codex 报告正例。这里的正例仅针对网络结论 gate，不代表全报告通过。

该 Codex 原报告另有真实质量缺陷：Limitations 引两文件标题第 1 行来支持无 protocol/controls/replication，定位不充分；材料未报告这些信息也不能证明实验本身没有，单条记录不能证明仅运行一次。原报告保留不改，整体不记质量合格。Work 共用 Skill 窄补通用规则：缺失信息只陈述 inspected sources do not report；单条观察不等于只运行一次；全篇缺失判断引用实际检查过的完整范围；复合事实分引或覆盖全部支持行。不含 Pilot 名称/正确行号，不扩展 Runtime。Native/External 继续复用同一政策正文。

这是新的生产政策改动，不能把 `3f4ecb1c` 的 Native 成功沿用为新政策成功；由主线在最终集成版本核源、stable 及三 Provider live 重新留证。本增量未自行调用远端。

共用政策更新后的定向回归：76 passed / 3 skipped（远端关闭）/ 1 既有弃用警告，5.04 秒；`/tmp/r1-12-closure/policy-final.{log,xml}`。checker 先行回归为同数量 5.18 秒（paraphrase-final）。ruff/diff 检查通过；未运行新的真实模型验证。

### OpenCode 等待期限与固定模型预算的实证

主线 `b75ab79e` 的新政策 Native（约 115 秒）与 Codex（约 150 秒）live 和独立人工审阅均通过。这些成功属于该运行 SHA，不能冒称随后提交已运行。相同版本 OpenCode 本次为 TimeoutError，1 failed / 2 deselected，373.54 秒；保留 `/tmp/r1-closure/research-policy-final/opencode/run.{log,xml}`，不改记环境未运行。

只读检查本任务 OpenCode SQLite 的 message/part 元数据：child 先完成 glob、两次 read，第三次模型在 01:25:11.068–01:26:00.591 UTC 返回 finish=length，正文 output=0、reasoning=4096，没有 write 产物。父每轮 subagent_wait 的 60 秒等待均遇到 MCP -32001 Request timed out，而非正常 running 返回。锁 core 的 OpenCodeModelConfig 只支持 model/api_base/api_key/provider；options.py 将输出限制固定为 4096，mapping.py 只对 completed 且 finish=stop 设 final_id，harness.py 要求 idle+final_id 才完成。当前没有可配置 token/reasoning 字段，本轮不修改锁 core 的预算或 length 完成出口。

仅把本 canary 的 OpenCode 单次 wait 改为 45000ms，为 MCP 60 秒请求期限留余量；Codex 60000ms、Native 240000ms 保持不变，模型/政策/360 秒整体预算不变。这能避免已确认的等待期限冲突，不能声称解决 child 的 length 耗尽。主线只对 OpenCode 再做一次有界复验；若再次 length 或真实失败，明确保留 OpenCode 研究验证待验证及已有失败证据，不扩大 core/锁范围。

脱敏证据为 `/tmp/r1-12-closure/opencode-limit-evidence.json`：只包含两 scope 的消息完成时间、finish、token 数、工具名/状态/时间和固定 core 源码行号/hash；不包含正文、思考文本、工具参数/输出、完整配置、环境或凭据。本增量未运行真实模型。

等待参数窄修回归：21 passed / 3 skipped（远端关闭），3.46 秒，`/tmp/r1-12-closure/opencode-wait.{log,xml}`；ruff/diff 检查通过。
