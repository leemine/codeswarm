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
- 受管 STEER、活动 Goal 提交及 Goal get/pause/clear 控制明确拒绝，等待原 owner control selector。没有来源的 EOF Goal handoff 也拒绝。拒绝发生在创建新 admission 工厂之前；不会把已执行控制伪报为未接纳。
- idle Goal/显式 attach 只有宿主明确允许 `request=None` 的独立 admission 时才可创建新 Turn；本片不提供这种 service 授权策略。受管 idle Goal 不使用 STEER，避免竞态借到另一个 active Turn。
- legacy Goal 的原直接 IO handoff、输出 detached projection 和旧控制行为保留。
- 父逻辑 EOF 不代表后台子操作资源退出。F33 生命周期载体与后台子操作独立权限由后续配套；legacy session_spawn 的未知来源不得绕过 core 退出屏障。Team 不在本片范围。

## 验证

开发基线 swarm `29fdfea475929e05637e99493821c7cb59a3bded`；正式非 editable core `835d9ebb74b2aa3344e8b4b7a4bdeec849c51e54`，环境 `/tmp/r2b-core-835d9ebb-locked-venv`，direct_url 与实际 site-packages 导入核对。测试使用本隔离 swarm 的显式 PYTHONPATH 和独立 JIUWENSWARM_HOME/CONFIG_URL=off，属于源码覆盖验证，不能替代新提交的干净锁安装/stable 或真实渠道验收。

`tests/unit_tests/runtime/harness/test_native_request_origin.py` 使用实际 core DeepAgent→TaskLoop→TaskManager/TaskScheduler→NativeHarness/IO；React/model 和 Session IO 为合成 fixture，无外部 Provider/模型网络。覆盖顺序独立来源、词法继承失效、cached legacy 拒绝、fast terminal、send 后 caller cancellation、queued 精确取消、原句柄重试、控制拒绝/原回答来源、非接纳与 unknown 区别、无 exit 证明的 terminal 拒绝。该新文件已被既有 stable runtime/harness 目录 discover/command 覆盖，无需新增测试白名单。

19 项新测试与 50 项原 Native、principal/control 相邻回归共 107 项通过（23.36s）；原 Goal detached 输出回归保留。既有 tool/model/MCP/artifact 词法回调与辅助模型邻近测试另 155 项通过（12.75s），两组共 262 项互不重复。stable 原 `codeswarm.python.execution-construction` 的 discover/command 均选中整个 `tests/unit_tests/runtime/harness`；实际 collect-only 确认新增 19 项已纳入，不重复追加文件，不改变预算。完整命令与日志在本任务 `/tmp/r2b-native-origin-tests`。未运行真实 UI/Provider，不宣称后台能力或原凭据撤销全链路完成。
