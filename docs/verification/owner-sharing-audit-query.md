# 当前会话所有者的分享审计查询

`session.share.audit.list` 是组织模式只读查询。请求只接受
`{session_id, limit?}`；limit 默认 50，范围 1–100（不接受布尔值）。不接受 cursor、
share/subject、历史范围、文件或用户提供的权限字段。当前版本返回最近记录，没有分页游标。

响应为 `{session_id, events, has_more, coverage}`。events 按 sequence 倒序，has_more
按当前读取快照中匹配事件精确计算；coverage 固定为
`confirmed_mutations_and_publications_only`。每项仅包含：sequence、event_id、recorded_at、
action、phase、result、share_id、share_revision、before_revision、after_revision、actor_id、
target_actor_id、request_id、method。直接宿主调用的 method 为 `host_api`，request_id 为 null。
不返回主体内部标识、原始参数、路径、历史文件边界、其它 Session/Project ID、seed、publication
指纹或凭据。

权限由当前完整 TrustedIdentity、原请求凭据、原 Session owner/source/Project ACL 和 lifecycle
共同决定。SessionBoundary 捕获原 owner_revision；Adapter 只接受原精确 permit，在读取前后、
线程返回后及 AgentServer 原 send_lock 之后核对原请求 ID/方法/channel/session/params、
原 principal 对象、原 permit 和原修订。Gateway 只走此方法的 unary handler，不建立历史订阅、
处理文件或进入聊天队列；queue/writer 继续验证原连接 principal、request ID 和同一 permit。
客户端响应、view/manage 分享或 Session 路由 ID 均不能建立 owner 权限。

仅选取 `source_session_id` 为当前会话、`owner_revision` 等于当前持久 owner 登记修订的事件。
拥有当前源会话的用户可以查到已撤销分享的历史；新登记 owner 不读取旧登记的历史。查询期间
完整 owner authority 修订变化会撤回缓冲结果。已删除/退役、未知 owner、失效 source/Project
权限一律拒绝；cleanup/delete receipt 不授予查询权限。目标私有 Session 的 owner 不能以其
Session ID 反向查询源会话审计；无匹配源事件返回空。此规则不要求 JSONL 文件仍存在。

存储复用原 project sidecar/锁。读取不会创建或修复审计域，不追加 query 事件；原 append 与
读取共用 schema 校验。旧数据没有审计域时返回空，不补造过去事件；损坏域返回固定失败，
不输出部分事件、不重置内容。每次 sidecar 读取与校验仍为 O(N)，limit 仅约束响应，未引入
第二审计存储、后台索引或保留策略。

验证覆盖真实临时组织凭据、ProjectAccessStore、SharingHost、grant/revise/revoke、原 Adapter，
AgentServer send_lock 与 WebChannel handler→E2A proxy→queued writer；运输桥为合成对象，
不是实际 Provider/UI 验收。新增查询测试已纳入原 stable governance-projects 的 discover 和
command；未增加超时或例外。原分享、审计写入、cleanup 与 Gateway 相邻回归保留。

本片不包含审计 UI；后续复用 ShareSessionDialog 的当前会话区域，不另建界面。当前也不包含
admission、consume、denied、delivery 全量事件，不能据此宣布完整 R2-B4 审计完成。正式集成
后仍需 stable、新锁来源核验和按影响面真实验证。
