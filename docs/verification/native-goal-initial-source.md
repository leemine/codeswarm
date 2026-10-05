# Native 初始 Goal 的原始执行来源（2026-10-05）

本片只接 managed Native **idle `set`**：`submit_goal(action, *, request=...)` 必须收到原宿主 `SendInputRequest`，将其交给原生命周期工厂并保留在原 HostRequest/PendingTurn。不再把 `request` 当成 GoalManager 的业务参数。未受管调用的原签名、Goal 输出和回调仍兼容。managed active 控制、idle resume、重建 attach 继续明确拒绝，等待对应原所有者控制与 Runtime 接线；这不是完整 Goal 或 R2-B 验收。

Goal 工作由既有 GoalManager/EventManager 自动派生，不带普通输入的 `native.host_request`。仅缺少该字段的 Goal 可以走来源证明；存在但错误的 token 不允许回退。

- 外层实际 `InvokeInputs`：原 Round facade Task、原 DeepAgent/Session、规范化 `RunKind.GOAL`/`RunContext` 与原 Goal work 一致。
- 内层实际 `TaskIterationInputs`：原 TaskManager capture、原 scheduler wrapper/当前 Task、原 loop event 来源和工作参数一致。继承 ContextVar 的任意子 Task 无权重新绑定。
- 两者共同核对原已登记 PendingTurn、其**同一对象** ExecutionOrigin、HostRequest/lifecycle/owned 引用、Binding、GoalManager 原 `(session, goal, revision, origin)`、原 Session 的只读 GoalRecord。host_value 相同不代替来源对象相同；畸形记录只拒绝，不清理持久状态。
- 所有宿主 source checker 调用后，再纯读原事实复核；Task 输入/登记、Session、请求、上下文、Binding 变化均拒绝。证明成功才复用原 entry 的 tool/model/MCP/artifact 闭包，不新增凭据或队列。

## 验证边界

27 个新确定性用例通过实际 NativeHarness/DeepAgent/GoalManager/EventManager/TaskManager/TaskScheduler/TaskLoop/ScopeRail。外层辅助模型与内层模型调用真实 core Model/OpenAI SDK/HTTPX，网络由合成 HTTP 响应替代；工具经过真实 AbilityManager、Tool 和最终授权点。另验证两个自动 attempt 保留同一原 PendingTurn/来源/回调。Session IO、事件总线、模型输出和宿主允许决策为 fixture；未冒充真实 Runtime 授权目录、Provider 或渠道验收。

测试覆盖缺/异/失活来源、两个阶段的继承子 Task、错 token/revision、畸形 GoalRecord、原 entry 替换、最后一个 source checker 同步改变请求/参数/Binding、原 Task 输入和 scheduler 登记变化。与旧 Native、来源、scope、模型消费者合并共 122 项通过；ruff/diff 检查通过。既有 stable `codeswarm.python.execution-construction` 已 discover/运行整个 `tests/unit_tests/runtime/harness`，新增文件自动纳入，不增加白名单。

来源是 Swarm `17a006d1` 隔离候选源码配正式非 editable core `6dce7ab486035d7334e5e37c8b166ea7c8711b58`，Python 3.13。该运行时中实际 iteration Task 等于原 scheduler wrapper；不据此声称未经验证的其他运行时调度形态可用。原始诊断曾错误推断 `wait_for` 创建了不同执行 Task；实际身份诊断纠正为枚举/RunContext 解析错误，最终不需要也不依赖额外 core 接缝。完整命令、失败分类与来源在 `/tmp/r2b-native-goal-source-evidence/`。全候选 stable、Runtime/facade 初始 Goal 接线及真实必要验收由集成关口执行。

## 独立复核后的来源修正

捕获开始只读当前来源和所有原对象，第一轮静态检查之后才调用严格 live-source checker，避免第一次回调重入就把替换目标采为原事实。第 1 次及第 3 次 checker 的同步替换均被测试覆盖（该文件现 33 项）。允许原普通 user Pending 经独立获准的 live Goal control 转为 Goal：原 `entry.goal` 可以是 None，但其原值、Pending/record/work/Task/source 的精确证明保持不变。该调整不自行开放 facade/control 权限。
