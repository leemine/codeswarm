# Native owner-only Goal 只读快照（2026-10-05）

私有接口 `runtime.goal_read.read_native_goal` 不调用 Runtime preparation、Binding 分配、执行器工厂、MCP、Native start/attach 或 Goal mutation。调用者提供原 owner/principal/read 权限检查及只读 cache/metadata lookup；本模块不另建 ACL、Session、执行队列或 Goal 存储。

`NativeGoalReadDescriptor` 包含 `session_id / provider_id / execution_profile_id / config_revision / config_fingerprint`，均为非空字符串且 Provider 必须 Native。`lookup_descriptor` 必须在核对真实当前 owner/Session/route 元数据后返回原描述对象，不能用永远返回原值的闭包冒充来源核验。`lookup_facade` 只查询已有缓存。首次宿主 checker 前固定描述的引用/值、原 facade/root/child、Native Session/Binding/原 Session；回调后和恢复 await 后纯读复核，结果的 `final_check()` 供既有 permit/sink 在最终投递时再次使用。

- 热读取：必须提供原 `ExecutionBinding`，与实际 Native owner/engine 的对象相同；从原 core Session 的 `harness.goal.record` 深复制再 `GoalRecord.from_dict`。原 NativeHarness 的实际 agent/Session、稳定 card 身份与缓存关联必须一致。
- 冷读取：无需也不创建 Binding。只使用已经配置的精确 `PersistenceCheckpointer`/`AgentStorage` 及其原 KV/serializer/recover 方法。创建内存 detached Session，使用原稳定 `jiuwenswarm` card 与明确 sid，仅调用原 storage recover，不 `pre_run`、初始化持久化或写回。允许原 Native 已确认退出且释放 owner 后读取现有 checkpoint；部分启动、未知退出、无法证明的 legacy 热实例或未知 checkpointer 显式拒绝。
- 返回既有 Goal-control DTO，只含解析后的 Goal 或 None，不返回完整 checkpoint。DTO 为副本；畸形或异 Session 的已恢复 Goal 字段报错且不清理原状态，不调用会修复 malformed 数据的 `SessionGoalStore.load`。

## 保留的存储限制

core 现有 recover 会把整个 blob 反序列化失败与缺失 checkpoint 都视为空。此包不复制其 KV key/codec，也不以第二次非原子读取伪造严格诊断；**None 不是严格证明没有 Goal**。已恢复 Goal 字段的格式错误仍严格拒绝。该区别有显式组件测试。冷快照是读取时的数据；同步 final check 复核当前权限/来源/映射，不声称原子重新读取磁盘。

## 验证与后续关口

新增 38 个确定性测试使用真实 core Session、GoalRecord、PersistenceCheckpointer/AgentStorage 与序列化 KV roundtrip；facade/Native 缓存图及 owner checker 是显式 fixture，不声称实际 Runtime/Sink 已接通。覆盖 ACTIVE 热/冷不执行不写、缺失与 malformed、外部 Session、回调重入、异步恢复期间 owner/配置/存储/缓存替换，以及结果最终检查。与旧 NativeSession/Goal控制用例共 **138 passed / 6.81s**，ruff 和 diff 检查通过，1 项既有 Authlib warning。

使用正式非 editable core `fa3490c08c93f32bba8fbfa0efdb499b9050624c`，解释器 `/tmp/r2b-core-fa3490c0-locked-venv/bin/python`；仅 Swarm 候选源码在 PYTHONPATH，无 core overlay。本候选祖先锁仍旧版本，因此这是明确新 core 的受影响源码验证，不是新 Swarm 锁安装或 stable。来源/hash/命令在 `/tmp/r2b-native-goal-read-evidence/`。首次 collection 因全新环境未完成既有 pysbd 预热失败，保留日志；按仓库既有 workflow 预热后正常 pytest 门禁未降低。

Runtime/SessionBoundary/Gateway 最终 sink 接线、stable manifest 追加本测试及正式配对由集成方负责。未运行真实 Provider、权限撤销/延迟/取消探针，未修改主 Runtime、锁或用户环境配置。
