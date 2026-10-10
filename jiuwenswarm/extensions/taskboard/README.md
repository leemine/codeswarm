# Taskboard MVP

内置 `ApplicationPluginExtension`，前端随 Web bundled 构建。Code 会话侧栏提供入口，路径为 `/taskboard`、`/taskboard/<task_id>`。Core 无改动。

## 边界与配置

- `taskboard.enabled: true`（默认）。改配置后重启当前 Demo；禁用后隐藏入口并拒绝 RPC，数据保留。插件整包热安装不在首版范围。
- Gateway 插件只通过原 E2A 代理转发四个 `taskboard.*` 方法；不打开数据库、不提供实例离线 fallback。
- 每个 AgentServer 的 `get_agent_root_dir()/taskboard.sqlite3` 为唯一任务权威，SQLite WAL、FULL synchronous、schema v1。容器必须持久挂载实例数据目录。
- 已认证模式用宿主 `TrustedIdentity` 作为 owner；本地单用户模式用 `local`，不信任客户端 user_id/owner。项目准入沿用 ProjectAccessStore；组织会话准入沿用现有 SharingHostService 的 owner_revision。
- 只关联当前实例自己的未归档 Web Code 会话。每次读取重新投影引用标题；撤权、删除、归档后保留关联 ID，显示不可用，不级联删除任务。
- 状态 `todo/doing/done` 由人修改；关联、打开会话、流结束均不会自动改变状态。无自动派发、附件、依赖图、独立验收、跨实例共享或新执行队列。

## RPC

| 方法 | 参数 | 返回 |
| --- | --- | --- |
| taskboard.create | title, client_create_id；可选 description, priority, project_id | task（同 owner 同创建键幂等；异内容冲突） |
| taskboard.list | status；可选 query, project_id, limit=30（≤100）, cursor | tasks, next_cursor |
| taskboard.get | task_id | task |
| taskboard.update | task_id, expected_version, patch | task（版本递增，冲突不覆盖） |

patch 白名单：title、description、status、priority、project_id、linked_session_id、result_note。清空引用用 null。标题≤120字符；描述/结果≤20000字符。搜索按标题或 `TB-001` 编号字面匹配。

## 宿主接缝

AgentServer 显式装配 TaskboardAdapter；插件 SDK 没有自动装配实例后端。ApplicationPluginOutlet 提供可选窄导航 props，复用原 handleRestoreSession，tab 内保存返回上下文与未提交结果草稿。共享 Button/Input/Textarea/Dialog、主题 token、现有 Web 客户端与双语文案。

开发与实际证据见 [Demo说明](../../../docs/taskboard/README.md)。
