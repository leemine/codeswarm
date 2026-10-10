# Taskboard MVP Demo 验证记录

日期：2026-10-10；执行者：Codex。产品代码提交：`eb7697a03644e444c507d7e05d69d4a1db2a03ff`（后续交付证据整理不改产品代码）。本记录针对本分支实际实现与隔离 Demo，不沿用正式3bbcacda的旧CI结果作为本次改动成功证据。

## 基线、锁和隔离

- 管理仓保留原有修改；用户 Swarm checkout `78c4529719932b785437747d0c2794d429ec4440`、Core checkout `9b41889a5d8cd3b8bd11376e1c41a5892c51fe57` 保持 clean。
- 开工 fetch 后 Swarm origin/develop `3bbcacdae3c44aba74263040550c770019795c63`，与19100正式部署记录及运行时镜像标签一致；包含PR45选择器与握手修复，故选为最新开发基线。
- 独立 worktree `artifacts/taskboard-mvp-demo`，分支 `codex/taskboard-mvp-demo`。Core无代码改动，无Core候选分支，无依赖升级。
- `uv sync --locked --group test --python 3.13`；Core实际安装为Git固定 `91943c6b6d2aa518eb8c5cd03a382c5afdc38e06` 的 noneditable发行包。Swarm editable只指向本隔离worktree。无本地Core路径替代。
- uv.lock SHA256 `c8dacc8b525d7d81d94debbb0737451f033e4895965dcfe0bea161587bd6f353`，npm锁未变；实际导入与原checkout状态见 [source-snapshot.json](evidence/source-snapshot.json)。
- 本地真实分离栈使用127.0.0.1的19240～19243；持久化在本任务 `.taskboard-demo/agent/taskboard.sqlite3`。原19100及现有运行时默认镜像未修改。

## 实现对应

| 范围 | 实际实现 |
| --- | --- |
| 应用插件 | extensions/taskboard，ApplicationPluginExtension＋bundled前端，默认启用，禁用拒绝RPC并保留库 |
| 权威 | Gateway仅原E2A转发；TaskboardAdapter显式装配在AgentServer；实例根目录SQLite唯一任务库 |
| 人工闭环 | 三列、新建编辑、搜索项目筛选、拖动/选择状态、优先级、结果保存、完成/重开 |
| 会话 | 当前实例自己的Web Code已有会话；正常打开/历史恢复，返回任务和详情深链接 |
| 一致性 | 创建幂等、事务编号、expected_version冲突、字面搜索、每列游标分页、有界参数 |
| 权限 | 宿主可信identity归属；ProjectAccessStore原权限锁下验证引用，组织会话用原owner_revision；撤权读取不暴露引用标题 |
| 兼容接缝 | 应用插件包装器新增可选user_id转发，保留旧四参数handler；公共页面props可选，不强迫其他插件修改 |
| 体验 | 复用原Button/Input/Textarea/Dialog、主题token与Code侧栏；中英文；tab内结果草稿；不复制执行控件 |

## 已执行验证

| 项目 | 结果与证据 |
| --- | --- |
| 后端受影响确定性回归 | **139 passed / 0 failed**，含18项Taskboard用例；[backend-tests.log](evidence/backend-tests.log) |
| 数据/并发/隔离 | 创建幂等、不同owner/实例、重启保留、20并发唯一编号、10更新仅1成功、冲突不覆盖、游标、撤权/损坏拒绝、旧handler兼容；上述确定性测试，不等同组织端到端 |
| 前端路由 | **2 passed**，Taskboard详情与chat路由往返、非法路径拒绝；[frontend-routing.log](evidence/frontend-routing.log) |
| 生产构建 | tsc＋Vite成功；[frontend-build.log](evidence/frontend-build.log)。既有大chunk警告保留，无构建错误 |
| 真实本地浏览器闭环 | 新建、编辑、关联实际session.create会话、跳转返回、保存刷新、人工完成/重开、搜索/项目筛选、拖动状态、导航草稿恢复；[browser-result.json](evidence/browser-result.json)、[browser-first-pass.log](evidence/browser-first-pass.log)；网络未mock |
| 发行包 | 构建32.8MB wheel，私有target安装后实际import来自wheel-site，插件／SQLite持久化烟测及bundled JS／metadata完整；[packaging.json](evidence/packaging.json)。锁定安装仍按uv.lock，不以wheel依赖解析代替锁源核验 |
| 独立容器真实E2A | 两个任务专属容器各自创建/列出任务，跨实例get返回NOT_FOUND；[container-before-rebuild.json](evidence/container-before-rebuild.json) |
| 容器重建持久化 | 删除并重建本任务两个容器，沿用各自数据挂载；task_id、version和完整task对象一致；[container-after-rebuild.json](evidence/container-after-rebuild.json)、[container-persistence.json](evidence/container-persistence.json) |
| 容器实际来源 | 现有镜像sha256:5b8f6fc8aeb62b51124d52874324ec662d9e624022e1a4f6d761eb91b3430a99，源码只读挂载本候选；Core实际direct_url为91943c6b，Swarm导入/app/swarm/jiuwenswarm。镜像标签保持原值 |

首次交付时，真实Provider尝试被现有Native运行时模型授权拒绝（181006），**该次未通过**；[provider-attempt.json](evidence/provider-attempt.json)、[provider-attempt.log](evidence/provider-attempt.log)。没有放宽权限、改Core或静默切引擎，当时私有Demo恢复为无模型默认配置；后续用户指定火山后的成功补验见文末。

最终补验已通过：[followup-result.json](evidence/followup-result.json)，本地三进程重启、第二真实RPC写者导致冲突及UI恢复、640px窄屏无水平溢出、英文；浏览器未捕获异常0。该轮真实Provider阻塞，后续火山成功补验见文末。

## 未运行与限制

- **组织模式双用户 → 单Gateway → Router → 容器实例的完整验收未运行**。已做两个独立容器E2A与可信owner确定性测试，不能替代组织身份、Router冷创建、元戎回收全链路。未把候选部署到19100。
- **Chrome107实机未运行**，本机真实现代Chrome版本见followup结果；沿用Chrome107规约与既有控件，详情高度提供100vh回退。未安装旧浏览器。
- **390px手机不作为通过项**：宿主现有html最小宽584px，首次390截图可见裁切；本次未扩大到全站响应式改造。640px窄屏单列为补验范围。原型390px目标保留为后续体验工作。
- 宿主应用默认light主题；本次验证默认主题token和英文，未新增全站dark主题，不宣称暗色实机验收。
- **首次本地交付时远端stable CI与每日full-python本分支未运行**：当时未推送、未建PR、未合并。完成本地必需关口；合入仍需按工程门禁执行，不引用旧SHA的5979项成功作为本分支结果。
- 禁用插件后入口隐藏、RPC拒绝并保留库；直接访问旧Taskboard URL目前为空白主体，未增加禁用提示页。
- 没有自动派发、依赖图、附件、独立验收、跨实例共享、任务删除、同列自由排序或第二套Provider队列。

## 首轮失败与修正

1. 首次AgentServer启动错误使用了不支持的--host参数；改用现有AGENT_SERVER_HOST。Gateway辅助端口19001与既有服务冲突，改为独立19243；未停止原服务。
2. 分发config占位模型触发多模态探测；新启动配置默认models.defaults=[]并关闭探测。首次日志留在私有Demo日志，不当作Provider成功。
3. 常规sandbox内async pytest阻塞，终止本任务该进程；在允许本地套接字的环境中有界重跑139项通过。两次回归参数引用不存在路径、收集0项，已纠正，未计为成功。
4. project.get_sessions响应的SessionInfo不带channel_id；前端重复过滤导致空选择器，已根据现有后端Web过滤修正，真实关联闭环通过。首轮截图 [preflight-session-projection-failure.png](evidence/preflight-session-projection-failure.png)。
5. 停止后端口TIME_WAIT导致启动前误判，端口检查增加SO_REUSEADDR，仍拒绝已监听端口；重启恢复另留实际证据。
6. Native尝试在进程cwd生成memory.db，已在停止本任务进程后移动到私有Demo目录；启动器随后把cwd固定为该目录，避免运行缓存散落源码根。
7. Provider首轮既有用户模型配置缺credential_encoding，被当前宿主模型绑定拒绝，未发出有效模型请求。仅在本任务0600私有配置显式声明plain及有界超时；后续实际请求被运行时模型授权181006拒绝，未获模型HTTP成功证据。保留失败记录，随后移除Demo复制的凭据，未放宽运行时权限。

8. 重复准备项目时原project.create按现有权限规则拒绝重叠目录；准备脚本改为先用原project.list／get_sessions查找可读的同目录项目和同名会话，再在缺失时创建，重复执行返回原项目与会话ID。未绕过ACL。
9. 英文截图首次仅等待标题而仍处于Loading；脚本补等真实任务卡片后重新生成截图，未把Loading截图作为最终界面证据。

## 清理与交付

本地Demo保留运行供体验；停止用README命令。专属容器验证后已核对任务标签并回收（[cleanup.json](evidence/cleanup.json)），持久化目录保留于被忽略的 `.taskboard-containers/`。源码、脚本、截图和本记录纳入独立Swarm分支提交；原checkout、配置与在线部署保留。回退只需停止本任务Demo并回到原入口，不涉及远端合并或线上回滚。

## 证据复现说明

受影响测试、构建与浏览器命令见README；容器验证使用下列实际脚本，前提是本机已有报告中固定digest的镜像和未占用的19252／19262端口：

```bash
.venv/bin/python scripts/taskboard_container_start.py
.venv/bin/python scripts/taskboard_container_check.py
```

重建验证在核对`codex.task=taskboard-mvp-demo`标签后，仅删除`codex-taskboard-mvp-alice`／`codex-taskboard-mvp-bob`容器，保留`.taskboard-containers/`数据，再重复上述命令；逐项比较重建前后task对象及version。日志文本仅去除行尾空白，未删除失败、警告或测试结果。源文件与交付资料`git diff --check`通过。

## 火山真实模型补验（2026-10-10）

用户确认“是火山”，指定复用“分析元戎启动配置”的配置来源。仅读取用户既有api-key文件中的火山段，将地址／密钥写入本任务被忽略的0600私有配置；不改19100、原用户配置、Core或依赖锁，不新增请求次数／有效期预算或转发。当前本机Demo的模型为glm-5.2、别名火山，已保留供继续使用。

先仅添加模型配置，两次未配置显式执行profile的Native请求返回181006：第一次尚无项目模型资源，第二次在精确资源登记后仍被拒绝。[失败记录](evidence/volcano-native-preflight.json)保留。随后补齐参考配置中的显式Native profile，在新会话中真实调用成功；此次是配置补齐，不宣称修复了无profile的Legacy Native路径。

- 宿主资源登记沿用ProjectAccessStore.register_resource，先确认项目owner等于本地安装可信身份、admin授权通过；仅给该主体登记单一火山credential的use动作，不可委托，不授予其他资源或关闭检查。[资源证据](evidence/volcano-resource.json)与[实际宿主登记脚本](evidence/volcano-host-provision.py)。脚本是本次部署配置证据，不是新RPC。
- 显式Native profile为taskboard-volcano-v1；新会话web_1a12685c46c_15752f4e09a0真实页面输入／发送，约8.24秒返回TASKBOARD_VOLCANO_OK；历史中保留同样的assistant回复和执行配置指纹。无mock、无响应替换。[结果](evidence/volcano-provider-result.json)、[真实回复截图](evidence/08-volcano-reply.png)。
- TB-002通过现有会话选择器关联“火山 Native · Taskboard 联调”，已从任务打开该会话并显示返回任务入口；状态仍为doing。独立只读浏览器确认回复可恢复、详情刷新后关联与状态保留。[页面补验](evidence/volcano-ui-result.json)、[关联截图](evidence/09-volcano-linked-task.png)。未自动完成人工任务。
- 私有配置还准备了同源OpenCode direct profile及独立runtime_root，本次没有切到OpenCode执行，未宣称其本地真实验证通过。原有会话与失败记录保留。

产品代码仍为eb7697a0，新增改动仅配置／隔离数据及本轮证据文档；无需重复记一轮未运行的139项测试。先前组织双用户完整路由、Chrome107实机等未验证范围保持不变。


## 远端合入准备（2026-10-11）

用户授权将本分支合入远端 develop。fetch 核对 develop 仍为3bbcacda，无上游新增提交；Core依赖及锁不变。现有 stable workflow 已包含干净 uv sync --locked 和实际 Core 来源核验，本次增加 Taskboard 139项受影响范围及2项前端路由的显式 CI 步骤，沿用原 stable 套件，不逐提交触发 full-python。PR／合入 SHA 和真实 CI 结果以管理仓专项交付记录为准；既有本地成功不代替新 SHA 的CI。
