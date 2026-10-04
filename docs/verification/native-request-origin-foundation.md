# Native Single 原请求来源与精确退出接缝

本片只提供 NativeSession 的原始来源、精确 Turn 引用及退出通知。Runtime/Coordinator 尚需绑定原 admission、原凭据监视和取消完成门槛；不能据此宣布凭据撤销后的完整退出或 Native 原功能保留验收完成。

## 宿主端口

`ExecutionResourceAuthorities` 末尾可选 `native_lifecycle_factory(native, request)`，同步返回 `NativeRequestLifecycle`。原 `tool_authority_scope` 对工厂读取采用词法活跃句柄，退出时主动失效；继承任务不能在 scope 退出后发起新 admission。已经捕获的 `ExecutionOrigin` 继续由其原 checker 验证，不受词法退出永久撤销。

`NativeRequestLifecycle` 是不可序列化、copy/deepcopy 保持身份的 live-only 对象：

- `source: ExecutionOrigin`：由 Runtime 原 handle/full principal 构造；建议 `host_value` 为原 `SessionExecutionHandle`。Native 不从 wire/metadata/当前用户推导来源。
- `on_bound(owned: NativeOwnedTurn) -> None`：同步通知原 core PendingTurn 对象已经捕获。
- `on_terminal(owned, kind: TurnEventKind) -> None`：原唯一 observer 见到该 Turn 的终态，且验证原 core 退出屏障或未进入 Provider 的 queued abort 证明后通知。
- `on_not_admitted() -> None`：可选；只用于宿主 source 初始检查或 core 创建 PendingTurn 前 source 检查明确同步拒绝。任意跨 await 异常、调用者取消不属于未接纳证明；失败保留原未确认提交，不用异常类型猜测结果。

`NativeOwnedTurn` 保留原 NativeSession、HostRequest、PendingTurn 及 source；公开 `request_id`、`turn_id`。不复制 Provider 状态机或队列，不进入 JSON。`capture_owned_request_turn(token=..., turn_id=...)` 幂等取得同一引用；`abort_owned_request_turn(owned)` 只调用该 PendingTurn 的 core 精确退出端口，随后等待原 observer 终态。queued abort 的提前 ACK 不算退出完成；超时/调用者取消不丢弃原句柄，可用同一引用重试。

## 接纳与终态时序

1. 每请求 `_register_host_request` 首个 await 前捕获 lifecycle，并放入原私有随机 token 对应 entry。
2. core `capture_execution_origin` hook 只解析此原 token，不接受客户端 origin。初始 source 检查发生在 PendingTurn 创建前；源 checker 不能提前要求 PendingTurn 已存在。
3. 实际 IO 返回 receipt 后立即绑定原 PendingTurn，早于原输出 router finally 的锁等待。调用者在此后取消也不能把已接纳 Turn 丢成未接纳。
4. 原单一 observer 与 receipt 共享同一 entry，保留 fast-terminal 的精确证明；不以 Turn ID 列表命中或对象不存在当作确认。
5. 只有原 core `exit.confirmed`，或原 queued ABORTED 且没有 admission/stream/execute body，才能进入 terminal 回调。调用者必须再把该证明纳入原 Coordinator 取消/关闭门槛；普通 Task.done 不替代它。

通知回调必须同步返回 `None`。未接纳通知失败记录固定警告，不覆盖原拒绝异常，不假装宿主门槛已清理。绑定/终态通知失败保留原引用，可重试通知；不重新分配 Turn。

## 尚未开放的受管控制与兼容

- `require_execution_origin=False` 默认保持 legacy；实际 deep adapter 构造处仅在当前提交工厂存在时启用。已经构造为 legacy 的 cached NativeSession 遇到受管工厂明确拒绝升级，不能静默降级。
- 受管普通 Single 请求必须每次提供有效工厂。后续无工厂不回退 legacy。
- 受管 `answer_request` 沿原 entry/source，不创建新 lifecycle、不更换 parent credential。Runtime 仍必须独立验证回答者及原控制 claim。
- 没有下述 exact control capability 的受管 STEER 仍拒绝；活动 Goal 提交及 Goal get/pause/clear 控制明确拒绝，等待各自原 owner control selector。没有来源的 EOF Goal handoff 也拒绝。拒绝发生在创建新 admission 工厂之前；不会把已执行控制伪报为未接纳。
- idle Goal/显式 attach 只有宿主明确允许 `request=None` 的独立 admission 时才可创建新 Turn；本片不提供这种 service 授权策略。受管 idle Goal 不使用 STEER，避免竞态借到另一个 active Turn。
- legacy Goal 的原直接 IO handoff、输出 detached projection 和旧控制行为保留。
- 父逻辑 EOF 不代表后台子操作资源退出。F33 生命周期载体与后台子操作独立权限由后续配套；legacy session_spawn 的未知来源不得绕过 core 退出屏障。Team 不在本片范围。

## 验证

开发基线 swarm `29fdfea475929e05637e99493821c7cb59a3bded`；正式非 editable core `835d9ebb74b2aa3344e8b4b7a4bdeec849c51e54`，环境 `/tmp/r2b-core-835d9ebb-locked-venv`，direct_url 与实际 site-packages 导入核对。测试使用本隔离 swarm 的显式 PYTHONPATH 和独立 JIUWENSWARM_HOME/CONFIG_URL=off，属于源码覆盖验证，不能替代新提交的干净锁安装/stable 或真实渠道验收。

`tests/unit_tests/runtime/harness/test_native_request_origin.py` 使用实际 core DeepAgent→TaskLoop→TaskManager/TaskScheduler→NativeHarness/IO；React/model 和 Session IO 为合成 fixture，无外部 Provider/模型网络。覆盖顺序独立来源、词法继承失效、cached legacy 拒绝、fast terminal、send 后 caller cancellation、queued 精确取消、原句柄重试、控制拒绝/原回答来源、非接纳与 unknown 区别、无 exit 证明的 terminal 拒绝。该新文件已被既有 stable runtime/harness 目录 discover/command 覆盖，无需新增测试白名单。

19 项新测试与 50 项原 Native、principal/control 相邻回归共 107 项通过（23.36s）；原 Goal detached 输出回归保留。既有 tool/model/MCP/artifact 词法回调与辅助模型邻近测试另 155 项通过（12.75s），两组共 262 项互不重复。stable 原 `codeswarm.python.execution-construction` 的 discover/command 均选中整个 `tests/unit_tests/runtime/harness`；实际 collect-only 确认新增 19 项已纳入，不重复追加文件，不改变预算。完整命令与日志在本任务 `/tmp/r2b-native-origin-tests`。未运行真实 UI/Provider，不宣称后台能力或原凭据撤销全链路完成。


## Runtime 原执行接线候选（待集成验收）

在原 SessionExecutionHandle 私有字段保留 NativeExecutionAdmission，仅记录原 Coordinator record、generation、完整 principal、原 producer、原 Native/Binding/适配器引用及 observer 回执，不创建第二套 Turn 状态机或队列。Runtime 工厂验证 AgentManager 当前持有的原 facade/root/Session child/native 与固定 Workspace/subject；组织模式必须使用原认证 principal，显式非组织 SDK trusted resolver 保持原兼容。

原 Session authority monitor 同时检查各 Native admission，Session 撤销检查先执行。每轮先封锁所有失效的原 handle，再等待其精确退出；首次超时不能使其他原执行继续获得授权。迟到 Provider 回执仍须取消并等待原 producer 收尾；未知提交保留至原接纳/未接纳证明。原 watcher、exit task 和 handle 在超时后保留供重试，不能选择后来的 Turn 或同 actor 的其他凭据。

terminal 的前提是原 Provider observer 已确认退出，且原 producer 及控制消费者已收尾。成功消费错误输出不能把 Provider FAILED/ABORTED 改成成功。正常回答沿原 parent source，不能用回答者的新 token 替换 parent credential；原控制 ACK/暂停不是 Provider 终态。cancel/close 先确认 Provider 再等待原 producer，未知退出不能释放 Session 资源。

Provider 取消 fence 与 producer 实际收到 cancel 分别记录。调度入口与直接入口共用原 admission 的一次取消事实；即使 producer 在 finally 中 uncancel，重试也只等待原清理。managed scheduler close 移除原 lane 后等待原 processor 自然收尾，避免取消 processor 沿 await 再次打断子任务；legacy scheduler 行为保持。

新增实际 Coordinator/Authenticator 回归使用合成 Provider receipt，实际 Runtime/Manager root-child 组合使用合成模型/IO。前一候选受影响105项在隔离host通过（14.11s）；受限沙箱 continuation 超时保留。随后发现并修正 scheduler 重复取消，最终候选回归另记。开发使用正式安装core804加显式swarm源码，未把它当新锁配对验收；主集成后的stable/UI仍需补齐。

本候选不关闭上述managed Goal/STEER/EOF、后台子操作、Team及真实对抗验收缺口；实现与失败关闭基础不等于完整能力保留。


调度器修正后的主独立受影响回归111 passed / 1既有Authlib warning，15.37s，进程退出0；`/tmp/r2b-native-runtime-scheduler-final.log`。新增组织公开invoke/stream对空/default/unmanaged项目拒绝、非组织SDK兼容，以及SESSION_MESSAGE/CHAT_UNARY取消重试与close的原cleanup gate。旧scheduler取消与旧close的独立进程overlay各稳定2失败，保留`/tmp/r2b-native-full-runtime-review/{cancel-red-overlay.log,close-red-overlay.log}`；未修改生产获取红证据。相关新增测试纳入stable，不降低预算或改变历史白名单。


## 原 Turn STEER 控制接缝（Runtime 接线/正式新 core 配对待验证）

`NativeExecutionSession.capture_steer_control(*, source, request_id, check_current)` 同步返回 live-only `NativeSteerControl`。`source` 必须是原 parent admission 的同一 ExecutionOrigin，不能凭相同 host_value 或最新 active Turn 冒认。capability 固定原 Native、Binding、HostRequest、PendingTurn 与 ActiveInteractionRound；`request_id` 是临时 SESSION_INPUT 的真实请求 ID。copy/deepcopy 保留对象身份，禁止 pickle。`check_current(native, request_id)` 同步复核这些引用并调用宿主原 checker；checker 必须返回 None，不接受异步授权。

Runtime 负责在首 await 前从原 SESSION_INPUT task/handle 定位 parent，捕获原临时凭据及其 Project execute 检查，并仅用私有 `request._native_steer_control` 传入 adapter。checker 在 core 原 admission 子任务中执行，须校验捕获的原 input task 是否仍存活，不能把 current_task 当成 input task，也不能借当前环境主体替换根凭据。后续 Runtime 接线见下节；仅 Native capability 组件本身不自行打开组织 STEER。

`send_request(request, *, control=...)` 对受管 STEER 只允许精确 request_id 的一次 attempt。临时 entry 沿用原 guarded tool/model/MCP/artifact 引用，不建立 lifecycle、Turn 或输出所有者，不重写 `_turn_requests`。向 SDK 注入的是原 entry token。经原 NativeHarness.send(STEER) 调用，避开 IO 旧 STEER→AUTO 回退；原 output router/observer 保持唯一消费者。权限 guard 前后和 SDK 实际入队前重验；guard 未发送、重复发送或改变 query/request/mode 均不能伪装为正常成功。

core 必须提供 `DeepAgent._send_owned_steer(expected_round, request, *, check_current)`：复用原两把输入锁、锁后检验原 Round/source 及当前临时授权，然后只向原 steer queue 写入；不进入 keep-open/new-Round fallback。缺少该端口明确拒绝。宿主没有直接拿 SDK 锁或写 SDK 队列。取消/失败后能力失效，shield 保留的旧 admission 子任务也须通过最终 checker，不能凭调用者 Cancel ACK 宣称未投递。未知结果不自动重试或改投新 Turn；原根执行的权限与生命周期保持。

新增 `test_native_steer_control.py` 使用真实 core DeepAgent/TaskLoop/NativeHarness 与实际新入队端口，React/Session IO 合成。17 例覆盖合法原队列、原 token/资源闭包、两锁等待期间身份失效、同来源新 Round、caller cancellation、旧 capability/replay、缺 core 端口、guard 丢弃/改参/重复及 adapter prepare 后重验。与旧 Native/adapter/session_input 相邻测试共107通过（6.73s）；这是 core-owned-steer draft + swarm 源码联合验证，不是正式锁验收或真实渠道 STEER 证明。命令/来源/日志见 `/tmp/r2b-native-steer-tests`。既有 stable 的 runtime/harness discover/command 已包含此新文件，未增加预算或历史白名单。Goal/EOF、后台独立授权和 Team 不属于本片。


## Runtime 原临时输入与根执行分离接线

原 Coordinator 在 SESSION_INPUT producer 首次 dispatch 前固定实际 input handle/task/principal 与原 parent Native admission。检查原 record/generation、registry identity、parent linkage、任务完成/取消及原凭据有效性；core admission 子任务执行 checker 时仍检查捕获的 input task，不使用当前 task 冒认。Runtime 固定原请求 ID/Session/channel、params 引用/query/mode、可信身份、Project、组织 host/source owner revision 和原 execute decision。跨锁期间 source epoch 或 ACL revision 变化即拒绝，即使相同用户仍被允许 execute。

capability 在实际 _stream_session_input_started→facade→cached child 入口传递；既有 facade 合法补齐 equipment 字段不受整个 params 哈希限制。根 tool/model/MCP/artifact 闭包及 principal 不被临时输入替换，无新 Turn、资源重装配或队列。原根执行退出、输入 producer 结束、改变 query/mode、凭据撤销和 Binding 更换都不能向后来 Round 回退。

正式测试文件 test_native_steer_host.py 的18项与 Native capability17项，在 core941d9375 联合源码环境35 passed / 1既有Authlib warning（9.02s）；其中一项通过真实 Coordinator.stream_session_input、Runtime、facade、cached adapter、Native 和实际 core steer queue。React 与 IO 为合成组件，没有外部模型或真实权限对抗。新增18项在 stable discover 与 command 两侧显式登记。两项 owner epoch/ACL revision 红测试与修复证据在 /tmp/r2b-native-steer-host-review；正式锁定配对、完整 stable 与真实渠道尚待集成。
