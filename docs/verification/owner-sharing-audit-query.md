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

ShareSessionDialog 复用当前会话 owner 管理区域，提供默认折叠的“最近分享审计”。只有现有
owner 列表查询成功后才显示入口；受共享者收件箱不触发 owner 审计。展开显式读取最近 50 条，
有更多记录时可重新查询最近 100 条；仍有更早记录时明确提示只展示最近记录，不提供虚假完整分页。
客户端严格校验原 session_id、字段白名单、倒序唯一序列、修订和固定 coverage，仅以纯文本显示
时间、动作、actor/target actor、share 和修订。失败清空结果并显示固定安全文案，不展示服务端错误正文。

关闭/折叠、换 Session、凭据变化（含登出）和断线会清空审计内容，取消请求并使旧 generation 失效；
迟到列表/审计响应不能恢复入口或旧记录。连接恢复不会自动复用旧数据，需再次刷新当前授权。
原继续执行 pane 的断线同 input/token 重试保持不变。此界面没有撤权推送或后台审计轮询，也不能撤回
用户已经看到的信息；每次读取仍由服务端独立授权，UI 可见性不是权限。

前端行为测试纳入原 test:session-continuation 窄脚本，覆盖 owner/inbox、限额、schema/纯文本、
显示后清理、迟到响应、取消和失败固定文案；相邻原分享/继续/历史测试、构建与双语言宽窄屏合成组件
视觉检查保留证据。合成组件 API 不是实际双用户服务或 Provider 验收。当前仍不包含 admission、
consume、denied、delivery 全量事件，不能据此宣布完整 R2-B4 审计完成。正式集成后仍需 stable、
新锁来源核验和按影响面真实验证。

## Owner exit observation foundation (2026-10-04)

The existing schema-1 `sharing_audit` event stream now accepts a narrow typed
`OwnerLifecycleAuditFacts` union alongside the unchanged sharing mutation and
continuation publication facts. This foundation has no Runtime, deletion receipt,
publication transaction, lock or Session state-machine changes. Actual Runtime
call sites must still be integrated before claiming exit lifecycle coverage.

The host constructs frozen facts from its original owner/cleanup receipt:
`source_session_id`, `source_project_id`, `owner_revision`, `source_revision`,
`action` (`cancel` or `delete`), `operation_id`, `generation`, and one of:

| phase | result | Meaning |
| --- | --- | --- |
| `exit_requested` | `requested` | The original owner requested resource exit. |
| `cleanup_retry` | `retrying` | A new attempt is retrying the same owned cleanup. |
| `exit_unconfirmed` | `unconfirmed` | Resource exit remains unconfirmed. |
| `exit_confirmed` | `confirmed` | The host has verified actual resource exit. |

Generation is a bounded integer; only cancel without a known execution may use
`None`. The context contains the original full TrustedIdentity and request/attempt
correlation. Its method is restricted to `chat.cancel`/`chat.interrupt` for cancel
or `session.delete` for deletion; absent method denotes a real host API call.
Neither the facts nor phases may be accepted from wire params. A producer
terminal, EOF, successful UI event, or audit record never establishes exit.

`append_sharing_audit(data, context, facts)` still changes only the caller's
original locked sidecar snapshot. It does not save, acquire another lock or
perform cleanup. The host must save using the original store and notify only
post-save. An append/save failure cannot undo a completed exit: the host must
retain the original receipt, surface audit-pending status and retry the original
facts. This foundation deliberately raises rather than silently degrading such
an observation. It does not manufacture missing past events or infer lifecycle
transitions from event order.

Idempotence scans the same validated event list, without another index/store.
The key is `(session, action, operation_id, generation, phase)`, plus attempt ID
for requested/retry/unconfirmed observations. Thus new attempts remain visible,
while repeated confirmation of the original operation returns its original
event. All immutable facts and the full actor must match; a new owner/source
revision cannot borrow that operation to rewrite history. Confirmed retries may
carry a new request/attempt context but retain the first event's correlation.
Non-terminal retries of one attempt require identical context. Operation IDs
must come from the original receipt; no lookup of the latest owner is allowed.

Current-owner query permissions are unchanged. Retired/deleted, unknown, or
currently unauthorized owners cannot read these records through this query.
The UI projection omits operation IDs, generations, paths, credentials, seed,
other Session IDs and full subjects. Lifecycle entries use null share/target
fields. The existing panel renders the four states explicitly; unconfirmed exit
cannot become a success label. A page containing lifecycle entries declares
`confirmed_mutations_publications_and_owner_exit_observations_only`; the old
coverage remains accepted for legacy pages. Neither label claims complete
admission, denied, consumption, delivery, or activity auditing.

Foundation validation uses an isolated worktree based on swarm `bcbd700f` with
noneditable installed core `c7fa3781` and explicit swarm source overlay:
**94 Python tests passed** (16.46 s), including real sidecar/query and concurrent
idempotent append tests; **56 actual React/Node tests passed**; frontend build
passed (36.71 s). Dependencies were installed into this worktree using the
unchanged lock with `npm ci --offline --ignore-scripts`; no shared main cache or
dist was written. Ruff/diff checks and `testctl plan --profile pr-stable` passed.
The new server test is included in both existing governance suite discovery and
command lists. Evidence: `/tmp/r2b-owner-lifecycle-audit/`. The initial sandboxed
Python run hit its original 180-second timeout at threaded query IO; the unchanged
suite completed outside the sandbox. No Provider/UI network acceptance or new
Runtime lifecycle integration is claimed by these component tests.
