# 项目权限与请求准备

项目权限保存在 AgentServer 当前数据目录的 `project_extensions.json`，旧
`projects.json` 继续拥有项目 ID、名称、目录、工作模式与 Git 字段。项目存储
中的 `access_managed` 标志使扩展文件丢失时保持拒绝；旧字段写回不删除该标志。

## 宿主身份

`AgentRuntime` 支持 `trusted_identity_resolver` 与 `project_authorizer` 构造参数。
可信身份包含请求者 `actor_id`、执行主体 `subject_id` 和来源 `authority`。
宿主必须从自己的认证上下文提供它；请求的 `user_id`、params、metadata 和
permission_context 不构成认证证明。

默认 loopback AgentServer 使用操作系统用户与注入数据目录确定的单用户实例身份。
它不是远端个人身份。绑定非 loopback 地址时不自动提供身份；受保护项目需要
由部署的认证边界显式注入 resolver。不得将请求中的用户 ID 包装成可信身份。

SDK 直接访问受保护项目时同样需要注入身份；没有 resolver 不会绕过已迁移 ACL。
普通未迁移项目保持旧行为。受保护 External Session 使用可信执行主体建立
Binding；旧 Binding 主体不一致时拒绝继续，不原地改绑或继承旧凭据。

## 迁移与接口

迁移是宿主操作，不开放“凭路由 ID 认领项目”的客户端接口。例如，由宿主完成
项目所有者核实后调用：

```python
from jiuwenswarm.server.runtime.session.project_access import ProjectAccessStore

store = ProjectAccessStore()
store.migrate_legacy({"proj_verified_id": "verified-owner-id"})
```

迁移遍历旧项目；没有可靠 owner 映射的项目保存未知 owner 并拒绝访问。再次运行
不会覆盖已有 owner、ACL 或修订。迁移前应保存原数据备份并确认映射完整。

项目 API 沿用 Web/E2A 请求/响应格式，新增：

| method | 授权 | 参数 |
|---|---|---|
| `project.extensions.get` | read | `project_id` |
| `project.extensions.update` | write | `project_id`，可选 `goal`、`extensions` |
| `project.acl.update` | admin | `project_id`、`acl`、`expected_revision` |

ACL 形如 `{"reader": ["read"], "worker": ["read", "execute"]}`；四个动作
read/write/execute/admin 分别授权，read 不包含执行。owner 拥有全部动作。
ACL 修改要求当前修订，冲突返回 `CONFLICT`；每次授权重新读取持久存储。
扩展读取仅向拥有 admin 的调用者返回成员 ACL。旧项目详情和列表格式保留，
授权后附加扩展摘要。

## 提交与兼容边界

请求准备保存输入快照、可信身份、原授权决策和既有 Runtime generation；提交前
重新校验当前权限及 generation。准备失败通过原 Session/AgentManager 生命周期
释放本次拥有的资源。重复请求或提交结果未知不会在同一 Runtime 内再次发送。
accepted 仅代表输入被接受，不代表模型执行完成。

去重回执属于当前 Runtime 生命周期，容量 4096，满后明确拒绝新准入；未知回执
不被淘汰后重新发送。没有新增跨进程持久去重或完整活动恢复。Provider 队列、
事件消费者、原 Team 调度与旧历史 codec 保持原权威。

旧项目生命周期、会话历史、文件、记忆和 HarmonyOS 入口也检查已迁移项目。
目前没有受众过滤能力的无范围历史/文件接口，在可能暴露不可访问项目时拒绝。
受保护会话的旧项目改绑入口关闭，避免将历史迁成未保护数据；后续授权衔接属于
R2-B3。目录别名不能绕过已存在项目的权限。

这不提供会话分享、活动工具逐操作撤权、远端身份认证、跨节点执行，或项目删除后
工作目录的永久资源 ACL。上述能力分别由后续共享、资源与部署任务验收。
