# Native Goal 原所有者控制接线（2026-10-05）

本片在既有 `command.goal` unary/stream 入口消费 Runtime 注入的 `NativeGoalControl`，不创建新的执行路由、MCP 连接、Native PendingTurn 或输出消费者。cap 固定原 request/session、缓存 root/child、Native Session/agent；参数在首次 await 前快照。执行前验证 `check_current()`，dispatch、历史写入后及响应前验证 `check_result()`。后者只保留原能力定义的合法结果投递，不授予新操作权限。

active set/resume 与 get/pause/clear 都投影一次原格式快照、确认或错误。成功 set 仍通过原 child 的历史 helper 写入；写入等待后发生 owner、身份或参数变化，不发布旧结果。缺 cap 的 managed mutation 被拒绝，尤其 active 流式 set/resume 不进入第二次 attach。legacy 路径保持原 ensure、路由、历史与流式行为。managed idle resume/re建 attach、idle get 的完整授权读取及 Runtime 全生命周期由独立包负责，不能据本片宣称完整 Goal 或 R2-B 已验收。

初始 set 继续由原 Pending admission 执行。`NativeExecutionSession` 在 lifecycle/source 回调之前固定不可变 action/session/业务参数，拒绝未知字段、bool 冒充预算整数等不支持输入；旧模式参数能力不变。真实 DeepAdapter dispatcher 核对原 Task 属于 Pending 的 admission 集合、原 registry/entry/lifecycle/owned/result、未完成结果、原 manager/Binding/session、原 ExecutionOrigin，再调用 source checker 并纯读复核。Pending 的 core 包装来源与宿主 lifecycle 来源不是同一对象：分别固定它们的原对象、原 Pending/owned 注册关系及同一 host value，不把 host value 单独当作证明。

## 组件证据

新增 44 项位于 `tests/unit_tests/agentserver/test_native_goal_facade_control.py`。使用真实 facade、NativeHarness、GoalManager、Scheduler/TaskLoop、原控制 cap 与初始 dispatcher；初始正例继续实际 Model/OpenAI SDK/HTTPX 和 AbilityManager/Tool 最终授权调用。网络、Session IO、宿主允许决策与历史 writer 是明确 fixture，未运行真实 Provider 对抗探针。

与原 Goal 控制、adapter、来源及历史用例合并 **182 passed / 9.71s**，1 项既有 Authlib warning。该次将来源 fixture 临时恢复原 4 秒 terminal 等待，pytest timeout 35 秒未变；没有依赖此前 4→10 秒调整。测试宿主执行，保留 sandbox 挂起为环境差异，底层等待原因未定位，不能称为已证明的 SDK 冷启动性能问题。

来源：Swarm `17a006d1` 后的初始来源候选；正式 core `6dce7ab4` 环境配独立 core F37 `876eb058bb643c081ea4750d7c965c48a8f9d971` 源码，以及主工作树的 `native_goal_control.py` 私有模块覆盖。这是联合源码验证，**不是正式新锁安装、stable 或实际 Runtime UI 验收**。来源/hash、命令和日志在 `/tmp/r2b-native-goal-facade-evidence/`。新文件须由集成方追加既有 Goal stable shard 的 command/discover 列表，不删旧项。

触及的 Native Session、facade 和新增测试 ruff 通过；DeepAdapter 全文件与 baseline 同为 71 个既有 E402，未扩大格式化；`git diff --check` 通过。保留首轮确认 ACK 拒绝（主 cap 已修）、旧池化夹具缺缓存字段（只读 lookup 已兼容）和错误假设 lifecycle/source 必须同对象的失败记录。未修改 Runtime、Coordinator、锁或公共事件协议。
