# Execution selection and Single integration

Use `parse_execution_config` for a dedicated execution configuration, not model-provider settings. Pass complete snapshots through `ExecutionConfigSource` (explicit > project > default) to `prepare_execution`, together with a host-owned `ExecutionBindingStore` and an authorized subject/session/absolute workspace.

`ExecutionConfigCatalog` freezes server-owned profiles and exposes only their
identifiers for selection. A chat request may name a profile ID; it must not
supply raw `provider_config`. The caller validates the subject and any Project
candidate before calling `catalog.source(...)`. Unknown explicit IDs fail
without changing to a different engine. `session.create` now resolves the
catalog before prewarm, stores the selected profile ID and revision in Session
metadata, and bypasses the legacy warm slot for selected sessions. The
approved request binds the server-owned snapshot after Workspace admission and
before Agent construction. A chat turn cannot change the Session's profile.

The optional server `config.yaml` section is parsed by
`load_execution_catalog(get_config())`:

```yaml
execution:
  default_profile_id: native
  profiles:
    native:
      provider_id: native
      config_revision: r1
```

An OpenCode Single profile uses the same public selection and authorization
fields. Provider-specific process and model details remain in the server-owned
snapshot and are compiled only inside the core OpenCode Provider:

```yaml
execution:
  default_profile_id: opencode-local
  profiles:
    opencode-local:
      provider_id: opencode
      config_revision: opencode-1.18.18-r1
      authorization:
        full_access: false
      provider_config:
        cli_path: /absolute/path/to/opencode
        runtime_root: /absolute/private/path/to/opencode-runtime
        model:
          model: configured-model
          api_base: https://model-endpoint.example/v1
          api_key: server-owned-secret
```

The `runtime_root` is a private host storage root, not the project directory;
the admitted project/cwd still comes from the immutable Session binding. New
OpenCode profiles must declare the public `authorization` object if global
product permission settings should control them. Omitting it preserves the
Provider's legacy configuration byte-for-byte rather than silently changing an
old Binding or cold archive.

When the section is absent the loader returns `None`; a present but malformed
section (including `execution: null`) fails validation. A configured default
selects new sessions; existing sessions without a selection retain the legacy
route. There is no selection UI or packaged default `execution` section yet.

The generic `prepare_execution` result is an unstarted core `HarnessEngine`.
The Native Single route below uses the already assembled DeepAgent instead of
building a second instance. An admitted External binding instead selects the
shared `EngineAgentAdapter` before facade/SDK construction and creates an
unstarted `ExecutionSession`; it never constructs DeepAgent. Team routing
remains on its original Runtime.

A bound scope retains its original snapshot when defaults change. An explicit attempt to change it fails rather than switching an active execution. `release` removes only the same binding object, protecting a replacement from stale cleanup. The store is process-local and contains provider configuration: never log it or treat it as durable session restore. Persistence, policy adaptation and UI integration remain separate tasks.

## Native host session integration

`prepare_native_session` accepts an authorized, already assembled **session**
adapter and the same configuration/binding source. It calls the adapter's
`build_native_execution`, reusing its original Native instance, product
Session factory (including KV-cache runtime), input guard and permission
resume dispatch. The selected provider must be Native; the host assembly
currently accepts no extra provider_config or requested_mode overrides.

The returned `NativeExecutionSession` owns one core HarnessEngine and one
HarnessIOAdapter event pump:

- `start(context)` verifies session/workspace against the binding; core owns
  pre_run/start/stop/post_run. Do not concurrently start the legacy adapter.
- `send_request(SendInputRequest)` keeps the host request and permission handoff
  objects in an execution-local transient table. Only a random reference and
  user text enter HarnessInput. Terminal events and stop release those objects.
- `answer_request(request)` answers only a currently pending interaction;
  repeated/stale answers do not become new inputs. Multiple questions resume
  once. Single-answer dispatch preserves the exact original request object.
  Multi-answer dispatch combines query answers and rejects conflicting host
  metadata instead of silently dropping it.
- `submit_goal("set" | "resume", **kwargs)` starts a protocol Turn while idle,
  or controls the current Turn immediately while work is running. It returns
  the receipt plus a Future for the original Goal control response. A
  confirmation or error does not wait for nonexistent output. If the current
  output reaches EOF during a running Goal replacement, an active Goal gets
  one new output attachment through the same protocol scheduler.
- `control_goal("get" | "pause" | "clear", **kwargs)` calls the original manager
  directly so control cannot be queued behind the work it needs to stop.
- `io.outputs()` is the sole projected output reader. Native cards come only
  from the authoritative interaction handler, retaining their original JSON
  shape. Observations do not create additional approvals.

For a host serving multiple Web requests over one session, core also exposes
`io.output_envelopes()`: each chunk carries its protocol Turn ID, followed by
that Turn's terminal marker. This shares the same single-consumer queue as
`io.outputs()`; the selected Native route keeps one consumer for the entire
session.

`NativeExecutionSession.enable_turn_outputs()` opts into a session-level
`TurnOutputRouter` before the first input. `send_request` registers its Turn
while submitting to the provider; `turn_outputs(receipt.turn_id)` yields the
original projected chunks and one terminal envelope, then ends. The mailbox
is bounded; closing a request iterator drains queued and in-flight output to
the detached projection before later envelopes for that Turn. The sole
session reader continues. Output without a live request owner is handed to an
optional detached projection callback by the same router. The Native product
route parses, records and pushes Goal continuation output after request EOF;
it registers detached questions in the existing Runtime Session coordinator
before pushing them, so subsequent control input has a live owner. The raw
Native host request ID is retained while its Turn is active, including when
the Web reader closes, so targeted Runtime cancellation still finds it. The raw
protocol observer remains for lifecycle/telemetry. If a request fails before opening its
iterator, call `abandon_turn_output(turn_id)` to release the mailbox. Do not
also read `io.outputs()` or
`io.output_envelopes()` after opting into this router.
An accepted STEER joins its active Turn and never creates a new mailbox; it
cannot reopen a mailbox abandoned after a client disconnect.
An idle Goal set/resume uses the same mailbox path; a control-only result
releases its mailbox without waiting for a nonexistent output stream.

The binding store is still owned/released by the Runtime caller. Runtime
must enforce authorization and session generation before passing answers;
this class is neither a new persistence layer nor a replacement for generation.

## External Single route (R1-03A1/A2)

`bind_admitted_request_execution` resolves one `RuntimeWorkspacePaths` snapshot
and freezes it with the selected Binding in `AdmittedExecutionRoute`. External
facade cache identity includes channel, subject, host session, workspace,
provider, revision and configuration fingerprint. Native cache keys remain
compatible. Global Native rebuilds do not restart an active External binding;
session cleanup removes only the matching External root and Binding.

All non-Native Providers use one `EngineAgentAdapter`. Its
`ExecutionSession` combines the core `HarnessEngine`, one `HarnessIOAdapter`
event consumer and one `TurnOutputRouter`. Tool approval is derived from the
same effective public execution authorization compiled by the selected
Provider: normal mode surfaces the existing product approval card, while
`full_access` suppresses that conflicting host prompt.
It validates the bound runtime root and cwd before start and stops only its own
resources. It lazily starts the Provider on the first Turn, using only the
admitted runtime paths and immutable binding; construction and execution never
fall back to DeepAgent.

OpenCode uses this shared route without a dedicated Swarm state machine. Its
fixed CLI/service lifecycle, native approval/question exchange and checkpoint
resume remain Provider-owned; project context, authorized attachments,
interaction answers, history/UI projection, artifacts and cleanup remain on
the existing product paths. Product MCP and same-engine child-agent tools are
separate follow-up capabilities and are not implied by selecting an OpenCode
Single profile.

`context_bridge` freezes project/personal context once per External Provider
process cycle, then builds each Turn input from that same snapshot plus the
current authorized attachments. It reads bounded project rules only when their
resolved paths remain under the admitted runtime root.
Session uploads are accepted only from that session's upload directory (or
when already inside the runtime root), bounded by count and byte limits, copied
to `.jiuwenswarm/session-inputs/<session>/<request>`, and removed on Session
cleanup. Arbitrary request paths and symlink escapes fail closed. The existing
fixed PersonalContext publication is read only when its product switch is on;
the External path does not construct Native rails.

## Work/Code cold-start Surface policy (R1-11B)

The admitted `SessionSurfaceIdentity` remains the Session/Binding identity.
Immediately before the first Provider start, `compile_surface_policy` combines
that identity, the current cold-start product switches and the frozen public
`ExecutionAuthorization` into a provider-neutral `HarnessRuntimePolicy`.
Work requests knowledge/document/web/artifact semantics; Code requests
repository/filesystem/terminal/Git/diff/test/review/LSP semantics. These are
requirements for later capability cataloging, not claims that every tool is
already installed.

Normal state receives workspace-write unless the admitted authorization already
permits full access. Plan always narrows to read-only. Project root, cwd and
outputs come only from the admitted `RuntimeWorkspacePaths`; project rules,
PersonalContext and attachments are read only from their existing authorized
sources. Missing enabled cold-start context is recorded as unavailable instead
of silently mixing old and new bytes. Context and policy remain unchanged for
every Turn in that Provider cycle; a saved switch or policy update is read only
when the next cold cycle starts, including recovery. The policy revision and
fingerprint are audit fields and do not alter Surface or Binding identity.

The core Provider owns private translation and effective verification. Codex
Surface profiles must disable process-env inheritance and declare trusted
`startup_source_roots`; full access does not re-enable ambient AGENTS/Skills or
plugins. OpenCode keeps its sealed runtime root and exact generation permission
readback. The public layer never writes vendor TOML/JSON. Native retains its
existing runtime rails and hot-update behavior.

## Effective Tool/subagent catalog (R1-11C)

Each External Provider cycle now compiles one `EffectiveCapabilityCatalog`
before allocating Provider or child resources. It merges the authoritative
`ProductToolGateway` namespace, the Codex/OpenCode private configured inventory
and the product `subagent_runtime` profiles. Work mounts general/research
delegation; Code mounts general/explore/plan/code delegation; Browser is added
only when the host admission adapter exists. Reverse-Surface requests and a
Provider-native tool that shadows an authoritative product tool fail closed.

The catalog is immutable for the cycle, is projected into parent and child
contexts with one fingerprint, and is rebuilt from admitted configuration and
mounted adapters on recovery instead of trusting a persisted record. Missing
required semantics retain a structured `not_installed` or
`provider_unsupported` explanation; available actions separately record when
host authorization is still required. These records do not grant permission
and do not replace the Provider's native Skill/plugin/MCP loader validation.

Typed output projection remains R1-11D, and UI/real Provider matrices remain
E/F. External Team and Work↔Code task conversion are not opened by this slice.
Programmatic legacy adapters without a product Surface retain their previous
context and subagent compatibility behavior.

The IO adapter remains the sole Provider event consumer and the Turn router
keeps exactly one output owner. Request-owned output uses the shared stream
parser and the existing facade history/UI path. If the Web reader disappears,
the same router transfers later envelopes to `ExternalEventProjection`, which
uses the existing history writer and runtime push service. A raw event observer
retains normalized terminal failure detail for projection; it does not read a
second event stream. Connection loss releases output ownership but does not
cancel the Provider Turn.

Tool approvals and user questions surface through the existing ask-user event
shape. `chat.answer` resolves the pending core interaction on the same Turn;
stale answers do not become new input. Pause, resume and cancel delegate to the
Provider IO adapter. Session stop owns Provider/CLI cleanup plus staged-input
cleanup. The legacy fork path rejects External sessions before target
allocation or Native context/history copying; persistent resume and supported
External fork remain R1-04/A5 work.

The session adapter also has `start_native_interaction` and an exclusive legacy
`start_interaction` path. Its `stop_interaction` releases the selected binding
after the protocol session stops. The facade selects the admitted Native route
before its first MCP reconciliation can create the child. The existing Deep
adapter keeps request setup and request-owned UI/history projection; the
detached sink reuses its chunk parser and product history/push services. Only
lifecycle, dispatch and the Turn output reader change. A question temporarily detaches
the same reader and its answer resumes it, without starting another Turn.

The configured Native path has been exercised through real Web Single text,
tool, ask-user, Goal, cancel, subagent and cold-restore flows. A separate
cutover check showed an unbound legacy session and a new default-bound Native
session both continuing after service restart. This does not change the
packaged default configuration or existing user config files. Team selection
remains on its original route; Product sub-Agent inheritance and persistent
cold recovery are later R1-03B and R1-04 scopes.

## Codex native plugins and extensions (R1-03C1/C2)

Native plugins are selected only inside the server-owned Codex profile's
`provider_config.native_plugins`. The frozen profile records the prepared local
marketplace identity, source path, exact version, package SHA-256, enabled
state, required Skills/MCP components and native MCP names. A request can only
select the profile ID; it cannot submit or mutate a plugin snapshot. An active
Binding keeps its original snapshot, while a changed profile applies to a new
Session and receives a different configuration fingerprint.

Core remains responsible for fail-closed package and native-loader validation,
approval routing, checkpoint binding and process cleanup. Swarm supplies the
authorized isolated HOME/CODEX_HOME and source roots, then reuses the existing
interaction/history/UI path for plugin tool events. C1 does not install or
update packages, add a plugin UI, expose hooks/commands/agents/apps, or route
product ToolGateway calls; deployment and the B1 product-tool path remain
separate concerns.

C2 additionally admits package-snapshotted command hooks. The server profile
must freeze each hook's native key, event, command definition, timeout, current
hash and related settings. Codex remains the sole trust owner: enabling a
plugin is insufficient, the user must review the exact hash in the native
hooks UI, and startup fails closed for untrusted, modified, missing or extra
hooks. The host never writes trust state and never uses the dangerous bypass
flag. Prompt/agent handlers, `commands/`, `agents/`, `apps` and
`appTemplates` remain unsupported; command/agent content is supplied as
portable Skills where appropriate.

Hook lifecycle notifications stay Provider-private and bounded; hook stdout is
not copied into chat history. Codex internal subagent notifications reuse the
existing single event consumer and product activity/history projection, with a
`codex:` identity namespace and `codex_internal` type. They do not create a
product subagent Binding or registry entry. Locked Codex 0.144.4 exposes no
host-callable internal-agent control RPC, so these rows are explicitly
read-only (`can_send_input=false`, `needs_resume=false`, `controllable=false`).
The existing six product subagent tools, lifecycle and controls are unchanged.

## Product ToolGateway and managed MCP (R1-03B1)

`ProductToolGateway` adapts an explicit catalog of existing product tool
instances. Definitions, invocation, output rendering and tool callbacks remain
owned by those instances; the gateway adds no duplicate tool implementation.
Every gateway is bound to one subject, parent Session and absolute workspace.
Those values come from the admitted ExecutionBinding and cannot be replaced by
model arguments. Non-parallel-safe tools share a per-gateway serialization
lock, while an optional host admission callback can reject an invocation.

An External provider with native ToolGateway support receives the gateway
directly. Codex and OpenCode receive one Session-owned Streamable HTTP
MCP endpoint bound to 127.0.0.1 with a random Bearer credential. The endpoint
is required and uses the Provider's native prompt approval. OpenCode accepts only
this authenticated loopback HTTP shape and disables OAuth. Readiness completes
before provider startup; startup failure, normal stop and router failure close
only that Session's endpoint. The reserved product server namespace cannot be
overridden by request MCP configuration.

B1 deliberately stops at this injectable boundary. B2 later supplied the
provider-neutral child port, B3 the Codex child composition, and B4 the six-tool
product/channel acceptance. This module is not a general MCP registry and does
not import Team runtime or reuse Team operator permissions.

## External Heartbeat tools (R1-08)

The shared External adapter receives the existing AgentServer Heartbeat service
through the facade and adds its nine original `HeartbeatRuntimeBridge` tools to
the same parent `ProductToolGateway` as the six subagent tools. The admitted
channel, parent Session and subject remain fixed in the tool context; the
original Heartbeat service validates the authoritative Session owner. Child
executions do not inherit this parent gateway. Store, Controller, Scheduler and
Execution remain AgentServer-owned; there is no Provider-specific scheduler.

Heartbeat admission reads active owners from the existing Session coordinator,
including queued Goal work and detached Goal output/interaction owners in the
current generation. Both scheduler busy checks and dispatch admission consult
that state. Goal `set/resume/pause/clear` enter the existing user-priority
preemption path; `get` remains read-only. Heartbeat does not write or advance
Goal records. After a cold restart, a persisted Heartbeat run without its exact
live execution owner is marked failed and its schedule disabled, including any
queued run. Unknown side effects require explicit resume through the existing
controls. Successfully completed runs with a remaining schedule recover normally.

Cancelling an External Heartbeat retains its Runtime owner until the original
`ExecutionSession.stop()` confirms exit, then closes the existing child runtime.
Abort requests, synthetic ABORTED events, failed output and EOF are not exit
confirmation. An unconfirmed stop retains the exact instance and admission;
a later explicit cancellation retries cleanup. Only after parent, product
transport and children have stopped can the original binding/checkpoint recovery
construct a replacement execution and fresh product gateway. Ordinary browser
disconnection continues to detach output without stopping the Provider.

External Goal product wiring is described below. External Team entry remains
subject to its existing routing scope.

The local Codex regression drives a real CLI and product MCP endpoint against
loopback model responses, creates a job, advances the scheduler clock, and
checks that the same External Session handles the automatic follow-up and
releases its pin/transport. It is not remote-model or browser-channel acceptance.

The persistent AgentServer owns scheduling for Web and Gateway CLI sessions.
Process CLI uses a worker per request and closes that Runtime when the request
ends; it currently has no idle Heartbeat host. A passing Provider CLI test or
Gateway CLI test does not establish automatic follow-up in Process CLI.

## External Goal (R1-09B)

The existing Runtime stream producer drives `GoalAttemptDriver`; its original
Session coordinator grants one External execution at a time. Ordinary user work
gets priority at attempt boundaries. Controls and interaction answers use the
existing control path without acquiring another execution permit. Native keeps
its original supervisor and scheduling. No observer starts work, settles a Goal,
or consumes a second Provider event stream.

The parent has one `ExternalSubagentParentSession`, shared by Goal and the
existing child registry. `GoalManager` remains the sole GoalRecord writer.
`get_current_goal` and `submit_goal_report` join the original product gateway;
reports require the exact root subject, Session, goal/revision/attempt and an
ephemeral attempt token. An old report cannot be rebound to the current attempt.
TUI slash controls use the original user text before prompt rendering, excluding
cross-session messages. Web slash text remains ordinary chat. Unary set followed
by explicit attach is supported without a second producer.

Each admitted owner retains its configured assessor model snapshot. Assessment
uses the shared GoalEvaluator and a separate no-tool `Model.invoke` call; it
cannot trust a terminal self-report as completion evidence. Request model names
select only server catalog entries. Login/request-credential model factories
are explicitly unavailable in this initial External slice; no default model is
silently substituted. Provider configuration and assessor configuration remain
separate.

Provider cumulative/delta/final usage is deduplicated by attempt, generation,
turn and event sequence. The assessor's separate usage is accounted once, even
when a late result must be rejected after cancellation. Missing input/output
counts are not zero and are not reconstructed from total-only usage: External
Goal stops with an explicit usage-unavailable result. Attempt and token budgets
are checked at settlement, not a promise of an exact provider-side token cutoff.
Unknown costs, including cancellation without usage, persist an accounting
marker that rejects further set/resume in that Session. A confirmed process
exit alone does not make an incomplete budget safe to resume.

Attempt finals remain one root output stream until Goal assessment settles it.
The original history writer stores the objective and stable completion card;
refresh does not create another copy. Provider completion does not release the
next execution until the original
Facade history consumer finishes; detached completion waits for its durable
terminal projection. A history write failure retains the permit.
Goal stream loss conservatively confirms
Provider exit and pauses. Explicit resume may start a new attempt only after a
safe boundary; an attach never resends an unknown accepted input. Cold records
with an unmatched attempt marker, unassessed attempt without a matching safe
boundary, or failed persistence remain read-only and explain the recovery
restriction. Delete that Session and create a new one when prior execution
cannot be verified. This does not claim transparent cold resumption of an
unknown pending tool or question. Ordinary chat keeps its existing detached
output behavior.

The local Goal CLI test exercises real Codex/MCP and a separate model client on
loopback fixtures, including report identity, usage and unique history. Remote
Native/Codex WebSocket/Gateway CLI and browser acceptance are separate opt-in
gates; local unit or CLI success does not substitute for them.

## Same-engine External child execution (R1-03B3 / R1-05 OC4)

`ExternalSubagentExecutionFactory` is the Swarm composition for the core B2
`SubagentExecutionFactory` port. It accepts one admitted, product-verified Codex
or OpenCode parent route and
captures that route's exact `AgentExecutionSpec`, Binding, subject, Session and
`RuntimeWorkspacePaths`. Each child receives its own `subagent:<id>` subject,
host Session ID, Binding, same-engine harness and Provider session while retaining the
parent configuration fingerprint, workspace root and task cwd. There is no
child Provider, profile, model, workspace or cwd override input.

The host resolves `subagent_type` through a fixed trusted catalog before child
Binding or Provider construction. `general-purpose` is the advertised generic
profile; existing built-in type labels remain unadvertised compatibility aliases
and do not gain their Native preset tools. `browser_agent` is advertised only
when the host installs an action-admission adapter; it then receives the same
Provider in a distinct child Binding plus an identity-bound core Browser
gateway and never nests the Native Browser worker. The default product wiring
keeps this profile unavailable until the R1-10D interaction policy is installed.
Unknown Browser capabilities, Browser capabilities on a generic profile, and
unknown type names fail before Browser, MCP, or Provider side effects.

Construction checks the parent subject and Session again before allocating a
child. An existing child Binding for another Provider or configuration fails
closed. The child reuses `ExecutionSession` and its single event consumer;
projected chunks flow through the B2 `SubagentExecution` callbacks and the
Provider terminal event settles exactly one `SubagentTurnResult`. Child cancel
aborts only the child Provider; close stops it and releases only its exact
Binding. Durable child checkpoint lookup remains R1-04 rather than guessing
from the parent checkpoint.

`ProductToolGateway` binds the server-owned parent `ExecutionSubject` while an
existing product tool runs, so B2 obtains the admitted parent lineage instead
of a caller-supplied identity.

## External product sub-Agents (R1-03B4)

`ExternalSubagentRuntime` is the Codex/OpenCode parent composition root for the existing
six product tools. It builds those original tools with the B3 execution factory,
binds their gateway to the admitted parent subject/Session/workspace, and uses a
minimal live parent Session port for product state and `OutputSchema` events. It
does not own a second registry, queue or status machine.

`subagent_updated`, `subagent_activity` and `subagent_message` use the shared
`server.runtime.agent_adapter.subagent_projection` path. Native and External
therefore persist the same roster/activity/transcript history records and feed
the same browser stores and panels. JSON arrays frozen at the protocol boundary
are thawed before invoking legacy product tools, preserving list-valued inputs
such as `subagent_wait.subagent_ids` without weakening the admitted scope.

Parent cleanup releases the exact cached control, cancels live children, closes
each child execution and its Binding, flushes product state, and stops the
activity emitter. Durable child checkpoint lookup and cold resume still belong
to R1-04; B4 intentionally fails `subagent_resume` closed when B3 reports that
the execution cannot be restored.

## Output capacity (R1-04C)

The router's `queue_size` now bounds the number of RAM entries per mailbox,
not the entire staging capacity. Unclaimed output, live mailboxes and draining
mailboxes share an `OutputBudget`: 8192 serialized entries, 4 MiB of RAM,
256 MiB of anonymous private spool files, and 64 MiB per serialized item.
There are at most 128 owner/unclaimed Turn slots. Receipt handoff transfers
the same buffer without copying or increasing its RAM threshold. Reads do not
remove an item until the live owner wins the close race.

`HarnessIOAdapter` has its own finite budget with the same defaults. Its
output-prefix cache stores at most 2048 SHA-256 prefix digests and character
counts, keyed by Turn/output ID, instead of retaining complete strings. Turn
terminal events release these entries. The adapter's pending-interaction count
is limited to 128; this does not replace the Provider interaction state machine.

Native/External detached projections and request text accumulators retain full
UTF-8 text, spilling after 256 KiB, with a 64 MiB text limit. Each detached
projection allows 128 active Turns and 256 MiB total text. Terminal error details
are limited to 128 entries of at most 64 KiB; correlation IDs to 1 KiB. Native
trace text and External unary text no longer accumulate unbounded lists; unary
responses keep only the first error and last terminal metadata.

Accepted large values are read back losslessly; final content still goes to the
existing durable history path. Spools are temporary delivery staging, not a new
history store or cold-recovery mechanism. A disk quota includes consumed extents
until that file has no unread spilled entries; at that point the file closes
and releases its full reservation. All router buffers and projection text close
on Session cleanup. The core adapter preserves the old ability to drain output
after `stop`; its spool closes when drained, when replaced on `start`, or when
the adapter is released.

No producer waits for a consumer to free a mailbox slot. Exhaustion or spool I/O
failure raises `OutputBudgetExceeded` (`OUTPUT_BUDGET_EXCEEDED`), attempts abort
through the existing control channel and reports delivery failure. It is not a
Provider FAILED/FINISHED event. Queued accepted output remains ordered; no final
or question is silently evicted. Subsequent input to a failed IO/router is
rejected; it is never automatically replayed. Limits account for serialized
payload bytes; interpreter overhead and producer/consumer-owned in-flight
objects are outside that byte counter. They are not a whole-process RSS limit.

## 公共授权与旧配置兼容

新 profile 可显式声明 `authorization: {full_access: false}`。如果服务端
`permissions.enabled` 是布尔值，catalog 将其转换为该新 profile 的最终公共授权：
`true` 表示普通审批，`false` 表示 full-access。模型/chat 请求不能设置该值。
`bridge` 通过 core 公共解析器决定产品工具审批，私有参数转换由 core Provider 完成。
Codex 和 OpenCode 支持显式授权；其他尚未适配的 Provider 会在构造前明确拒绝。

旧 profile 缺失或设置 `authorization: null` 时保持旧语义：普通配置不补字段；
旧 full-access 仅对 Codex 使用 core 中的兼容投影，JSON 和指纹与迁移前一致。
已绑定实例不跟随默认值变化。给旧 profile 补显式授权（包括 false）会改变指纹，
metadata/冷归档严格拒绝恢复；应使用新 profile 与新 Session。归档无需迁移，
身份/Workspace/Provider/版本/指纹检查均继续执行。

本地源码集成需要包含公共授权接口的 core；正式发布必须先合入 core，再更新声明和锁，
经干净安装验证。本次联合开发不把 editable 测试当作锁定版本验收。
# R1-11F Team integration candidate

The Team construction port receives the original member backend and role. The
member tool gateway must use core's `create_team_tools` and the existing product
gateway/transport: Task, Review and message operations remain owned by Team.
Member identity comes from the host construction request, never MCP arguments.
Every call requires a host admission callback; role filtering does not replace
runtime authorization. Native-only async/fork/Swarmflow tools stay unavailable
until their execution paths have an explicit External implementation.

This construction component does not admit product Team sessions. The existing
External Team Surface rejection remains until product controls, capabilities and
history/UI projection are connected and verified. Local source tests
are not locked-dependency or real Provider E2E acceptance.

The construction candidate attaches an External member factory in TeamManager
only when it receives an admitted Team route. It uses the root's exact execution
snapshot and workspace, derives a stable member Binding, and returns the core
member runtime with one Provider and one managed product MCP transport. It never
constructs a Single ExecutionSession or consumes a second event cursor. Transport
creation is deferred until start and its exit is confirmed after Provider exit.

The existing BuildContext seed carries only the profile, binding fingerprint,
Surface identity and paths; rehydration resolves the selected profile from local
host configuration and compares authoritative Session metadata. Changed config,
identity, workspace or missing metadata fails closed. Ordinary tool calls also
check the current host profile against the frozen binding. This slice accepts
normal Team execution only; plan, worktree relocation and distributed bootstrap
remain explicit errors pending their own integration evidence. Product routing
stays gated until capabilities and control/history/UI projection are connected.

The selected profile must be explicit; a missing profile must not pick up a new
default. Native capability declarations (tools, rails, subagents, MCP, skills,
agent templates) are rejected until their Team adapter is implemented.

Local verification: core 308 passed; host/Surface/Team regression 585 passed and
2 existing skips. Real Codex and OpenCode CLIs each run leader and teammate
Turns, call original view_task through authenticated MCP, restore the leader
checkpoint and confirm owned transport cleanup. Model endpoints are test-owned
loopback fixtures. This is not remote-model Team six-cell or UI acceptance.

## F3 Team product adapter candidate

A dedicated External Team adapter delegates streaming to the existing
`process_team_message_stream` and keeps TeamManager/Runner as the sole owner.
It does not construct DeepAgent or the Single execution session. The facade
validates the frozen External Team binding before using its existing Team
pause/cancel path, including controls whose params omit the mode. UI capability
fallback must not label an External Team as Native; unfinished product areas
remain unavailable until their real Team catalog is connected. This candidate
does not remove the global Team admission gate. Unary execution, Goal and other unconnected operations fail explicitly.
Scoped member answers now delegate to original Runner interaction futures;
remote-provider and channel acceptance is still required before opening admission.


The F3 candidate also requires confirmed Runner exit before releasing External
Team local registrations, and propagates stop failures/cancellation for retry.
The facade retains its External adapter when cleanup fails. EOF without a Team
terminal is the existing unknown_terminal_payload outcome; it cannot synthesize a
Team completion snapshot. Native's historical fallback is unchanged.

Active iteration pause remains unavailable for these Team Providers. The facade
returns a negative interrupt result before the legacy pause fallback can swallow
the unsupported operation. Disconnect uses the original cancel path with paused
workflow disposition and strict exit confirmation; it does not claim to park an
active Provider iteration. Normal/plan mismatches, including request metadata,
are rejected before Team construction. Goal remains explicitly unavailable;
member answers require the exact live scoped address. Admission remains closed.

## R1-11F F3 成员交互与产品投影续作（2026-09-30；先设计后实现）

成员审批使用原 IOAdapter pending 与 Runner interact；产品地址携带成员和周期，回答必须精确匹配。External 非 leader 的问题不可再过滤，隐藏成员输出也不能隐藏待答卡。仅先准入 inprocess，process/Cluster 需另补跨进程确认。

能力由原 provider inventory、Team 工具和产品 subagent 目录编译；Browser 仅在原开关启用且权限接通时可用。产品子 Agent 与成员身份分开，持久状态依托原成员 Session；根 Goal/Heartbeat 不派生给每个成员。typed 事件在原单消费者投影中关联成员 Session/Turn/tool/Artifact，沿原 Team 历史与 UI 消费链。活动暂停不支持。

并发成员问题不能覆盖原 Runtime handle 的单个 waiting_control_id：在现有 handle 内维护有界待答 ID 集合，保留旧主 ID/snapshot 兼容字段；精确回复仅清除对应 ID，最后一个回复后才允许结束等待，generation/claim/取消仍由原 coordinator 管理。不会增加第二个交互存储或审批决策者。


候选已接通原 ExternalSubagentRuntime、BrowserAdmission 和 Artifact 投影，逐次工具调用重新校验冻结 scope/开关，成员 Session 保留产品状态。原 Runtime execution handle 保留多个并发待答 ID；连续 Heartbeat 审批清掉已回答 ID，根调度所有权不变。根 Goal driver 的 Team 产品接入仍不支持。

生产者持久化后经原 Team stream/UI 消费，不再由 facade 重写历史。工具及 typed item ID 带成员 Session/Provider Session/Turn；Work 输出只认 typed 路径归属。历史写入失败沿原流报告 HISTORY_PERSISTENCE_UNCONFIRMED，后续成员终态及 Team idle 不能覆盖为成功。无第二 Provider cursor。

恢复限制：未确认交互/产品活动的成员 checkpoint 明确拒绝冷恢复，标记仅在 Provider + 子 Agent/Browser + MCP 确认退出后清除。活动产品恢复、远端模型六格、真实 Web/CLI 渠道与跨进程/跨机器验收仍待完成；活动暂停不支持。最终 core 437 / swarm 830 / 真实 CLI 本地模型 2 passed，具体环境与告警见管理仓 R1_11F_TEAM_CLUSTER_E2E 第 9 节。


## R1-11F 根 Goal/Team 控制接缝（2026-10-01，编码前）

源码事实：现有 ExternalGoalRuntime 的一个 attempt 绑定单 ExecutionSession/Turn，GoalAttemptEvidence 只统计该来源。原 Team 流跨多个成员/Turn 且 idle 后还可再唤醒；原 TeamManager 没有根 Goal attempt 端口。不能直接继承 Single adapter、将 leader Turn 或 Team idle 当根 Goal 成功，也不能复制一套 GoalManager/调度器给每个成员。

本批先收紧原控制契约：External Team pause/resume 均返回 interrupt_result.success=false，取消仍走严格退出确认；原 Native resume ack 保持兼容。结构化 Goal 结果提供原 facade 读取的 error_code，保持既有 code 别名；所有 Goal 操作显式拒绝，不写目标记录、不调用 Team/Single Provider，也不伪造目标已暂停/恢复。活动暂停与停止未来 Goal attempt 是不同语义。

后续完整根 Goal 的最小接入顺序：先在原 Team Round 所有者中固定 root Session/generation/request/goal revision/attempt 对应；经现有单消费者汇总该 Round 内所有成员及产品子 Agent 的 Turn 身份、usage 和终态，重复/迟到不得重复计量，缺 usage 明确阻塞。再由原 Runtime GOAL_STREAM producer 驱动同一 GoalAttemptDriver/GoalManager，沿原 Team 调度执行并由独立 assessor 结算；观察器不推进执行。取消/替换须等所有自有资源退出，未知状态持久拒绝重放。完成这些前继续拒绝根 Goal，不以本批拒绝响应测试冒充 Goal 执行验收。


## Team 根 Goal 证据接缝（2026-10-01，编码前）

在原 _ActiveTeamRound 上可选挂载一个 TeamGoalAttemptEvidence；绑定必须精确匹配原 Runtime GOAL_STREAM/GOAL_ATTACH handle 的 Session/request/execution/generation，持有原执行许可且该 Round 延迟释放。它只观察并复用 GoalAttemptEvidence 的单 Turn 计量，不创建 GoalRecord、队列、future 或执行任务。

每个成员和产品子 Agent 沿原唯一事件消费者接入一个周期固定的观察器，按 host Session/agent/Provider Session/Turn 关联，在 STARTED 时捕获所属 Round，后续不重新指派给新目标。序号去重、周期隔离与有界已见 Turn 保证旧事件不能计入新 attempt。结束/释放原 Round 封闭其观察能力，不从 EOF/idle/全部当前 Turn 结束自动推断 Goal 成功。只有未来原 producer 确认团队回合边界并显式 seal 后才可取一次完整用量；缺 usage、失败/未结束 Turn、归属异常或预算超限均不可通过成功评估门槛。

完整根 Goal 执行仍拒绝：此批是运行时已接线、可注入验证的只读前置端口；GoalManager 单写者、GoalAttemptDriver、独立 assessor、状态持久化及实际团队回合结束仲裁在后续接入。不得将证据收集测试算作目标执行完成。

候选实现已接线：原 TeamManager 的 Round 可绑定精确 GOAL_STREAM/GOAL_ATTACH 所有者；许可释放、generation 变化、取消或 Round 替换使其不再接受新 Turn。成员与原产品子 Agent 分来源/周期计量，重放不重复记账，取消中的旧 Turn 终态仍可归属旧 attempt，释放后不再收集。所有已观察 Turn 完成仍需 producer 显式 seal，缺输入/输出 usage、归属异常、失败/取消或超限不可进入成功评估。计量只取一次，不写 GoalManager。

本轮受影响 Python 975 passed，真实 Codex/OpenCode CLI＋本机模型 2 passed；成员＋产品子 Agent 的双 Provider 组合使用脚本化 Provider 和真实原 Runtime/Session/事件泵，不能算远端模型 Goal E2E。两仓 Ruff/diff 通过，core 本轮源码未变；完整 Goal producer/评估/持久化、真实 UI/远端六格和 Cluster 仍待验证，全局准入与活动暂停/继续未开放。详情见管理仓 R1_11F_TEAM_CLUSTER_E2E 第 11 节。

## 根 Team Goal producer 接入（2026-10-01，编码前）

复用 ExternalGoalRuntime 的原 GoalManager/SessionGoalStore、控制口和记账方法，Team 子类只提供原 Runtime producer 内的团队 attempt 执行，不增加任务/队列/Provider cursor。原 Team helper 的 bounded Round 增加内部 Goal 所有者接缝，在任何提交前绑定证据并保留 Round 直到 producer 结算。结束信号仅为候选边界；经原 Runner 确认所有自有资源退出、检查各 Turn 终态及完整用量后 seal，独立无工具 assessor 由宿主授权模型构建。失败/取消保留已花用量，写入失败/缺计量/退出未确认保留原恢复标记，不自动重放。必须有原根 Session 恢复档案才装配该候选；无持久档案继续显式拒绝。全局准入未开放。

本轮候选已实现根 Team Goal producer：仅有原 root recovery 档案时装配，继承既有 ExternalGoalRuntime 的 Manager/控制/记账端口而覆盖 Single 执行循环。原 Team helper 在提交前绑定 Goal Round；leader final 不结束 Goal waiter，Team 边界之后经原 Runner 退出确认并等待原历史消费者排空，Round 保留到评估/持久化完成。每次回合复用原 Team 构造/任务/Review/成员恢复链；不构造 Single adapter，也不创建另一个调度器。

每 attempt 使用独立、宿主授权、无工具的 transcript assessor，Provider 与 assessor 用量仅各记一次；沿原 GoalAttemptDriver/GoalManager 写入同一加密根档案。成员不新增 GoalManager 或报告工具。取消/断连先停止原资源；缺用量、评估取消且费用不明、退出或写入未确认保留恢复拒绝。历史回执前保留原执行许可。Goal pause 表示不继续下一次尝试，允许当前回合正常评估；活动 Provider pause/resume 仍不可用。Goal owner 活动时普通 Team steer 与第二个 Goal stream 显式返回 goal_owner_busy，避免另一来源混入当前证据；需先取消当前 Goal。

最终影响面 1178 passed、真实 CLI＋本机模型 2 passed。根 Goal 原 helper/Runner 的单/多回合实际组合使用脚本化 Provider；CLI 项验证原成员/工具/恢复，不是根 Goal 真模型验收。初始真实接缝失败（流退出提前释放 Round）、无档案时 assessor 预检兼容失败，以及测试 DB 未关闭造成的线程 setup error 均保留并修复。全局 External Team 准入未开放，真实根 Goal Provider/渠道/UI、多成员任务与 Review 跨回合、远端六格、活动产品恢复、Cluster/跨机器仍须验收。两候选未提交，未升级锁。管理仓第 12 节记录准确命令和源哈希。

## 根 Goal 本机 CLI 与跨回合验证（2026-10-01）

新增根 Goal 系统用例使用真实 Codex/OpenCode CLI、原 Runtime/Team helper/Runner 和加密档案；模型端点与无工具 assessor 受控。两次 attempt 各调用原 view_task，验证用量只结算一次、历史 objective/completed 各一条、历史回执前保留 owner，以及退出和冷读取。最终与既有成员恢复回归合计 4 passed。

原 Runner 三成员组合使用脚本化 Provider/checkpoint 和 assessor，在原 SQLite TaskManager 中预置 reviewer，再走真实成员工具完成 claim/submit/verify；同一 Task/Review 和三条 roster 在下一 Goal attempt 保留，禁止作者伪造 reviewer；有 checkpoint 继续，无 checkpoint 明确阻塞且不重做任务。它不覆盖 scheduled dispatcher 分配 reviewer，也不是三成员真实 CLI 联合验收。受影响 Python 398 passed，生产代码/core/锁未修改；初始 fixture 隔离、Session 上下文、checkpoint 和 DAO 格式断言失败保留。远端模型/原渠道 UI/Cluster、活动产品恢复仍待验证，全局准入与活动 Provider pause/resume 仍关闭。详见管理仓第 13 节。


## 三成员真实 CLI 与 scheduled 准入边界（2026-10-01）

根 Goal 的真实 Codex/OpenCode CLI 本机模型测试新增三成员 Task/Review 双 attempt。原 TaskManager 预置 reviewer，原 backend 自动启动成员，实际 MCP claim/complete/verify/view_task；第二 attempt 从真实 Provider checkpoint 恢复三个成员并保留任务、review_round、roster，结算含全部模型请求和 assessor 用量，目标历史不重复。该正向用例不注入活动消息抢占；此前带额外 send_message 的 Codex 组合出现成员中断、根 Goal 保守阻塞，原日志和复现 fixture 保留，并发抢占仍待闭合。

源码与真实 CLI 验证确认：原 scheduled scheduler 的 `_spawn_temp_reviewer` 直接使用 `TeamHarness.build`/`run_once`，绕过已选择的成员 Provider。External factory 现明确拒绝 `dispatch_mode=scheduled`，覆盖 attach、build、冷恢复 spec 和启动前漂移，且在资源分配前失败；即使 verification flag 关闭也拒绝，避免历史待评审任务进入这条分支。Native 路径不变。该拒绝是当前能力边界，不代表 scheduled 已适配；后续需复用原 scheduler 完成 reviewer Provider 构造、用量/事件归属与退出管理，不能临时降级 Native。

最终结果及初始失败见管理仓 R1_11F_TEAM_CLUSTER_E2E 第 14 节。全局准入、活动 Provider 暂停/继续仍关闭；scheduled/pending review/消息抢占、远端模型/渠道 UI/Cluster 仍待验收。


### F3 活动消息回执修复（2026-10-01）

Codex 早到 steer 改为等待原生接受；仅精确“无活动回合、未接受”的拒绝委托原队列
投递 FOLLOW_UP，并返回新 Turn 回执。原 Turn 不被错误中断，根 Goal 的成功/用量
要求不变；未知错误禁止自动重发。真实 Codex/OpenCode CLI＋本机模型重新加入两条
成员消息，最终 8 passed；core 581 passed / 1 既有 DSH timing skip，swarm 408 passed。
第 14 节所记录的接受竞态已有修复和对应验证，历史失败保留。全局准入、scheduled、
活动 Provider pause/resume 仍不开放；远端模型/渠道 UI/Cluster/干净锁安装未验收。
详见 R1-11F 验证第 15 节；唯一状态源仍为任务跟踪表。


## scheduled 临时 reviewer 宿主候选（2026-10-01）

team_review 通过同一个成员工厂承接 core F_116 端口。独立 invocation Binding 复用原
ExternalHarnessMemberRuntime/IO/TeamMemberProjection，原 Verify/View MCP 与授权核验；
verify 调用须匹配 task/review_round/IN_REVIEW。审批从原 Runner/scheduler owner 精确定位，
不加名册、交互 registry、事件消费者或第二套前端。root Goal 标注 reviewer 来源并汇总
原 typed 事件用量；历史/产物沿原投影。

原 Team Session 在派发前 commit pending，成功终态/完整用量/历史/MCP 与 Provider 退出
全部确认后 commit closed；未知/取消保留 blocked，leader 冷恢复及同轮再派发拒绝重放。
清理失败保留同一 runtime 和 transport 重试；不把流关闭当成功。

两真实 CLI 的本机模型 scheduled 正向通过：原调度交给 worker，member_complete_task
提交评审，临时 reviewer Verify 投票，退出后原 CAS 结算；两次 Goal attempt 只评审一次，
名册不包含临时 reviewer，用量/历史/冷读状态与资源退出均核对。系统测试专用覆盖
scheduled guard；产品 scheduled/global guard 保留，活动 Provider pause/resume 不支持。
在途 pending review 冷恢复、部分票据跨 attempt、真实 CLI 失败/交互、远端渠道/Cluster
与干净锁安装仍待验收。完整初始失败与最终结果见管理仓验证第 17 节。


### F3 评审持久恢复与部分票据候选（2026-10-01）

leader 启动在 Provider/MCP 前检查原评审记录，不依赖 history_restored；非法空值拒绝，
closed 提交失败/取消回滚内存 pending 并保留清理重试。原 SQLite Checkpointer 的
pending/blocked 在独立进程冷读时拒绝重放，closed 才允许；原 scheduler/票据 SQLite
关闭重建后只补缺票，已投票者不重跑。部分票据保持 IN_REVIEW，不自动推进 Goal attempt。

新增恢复组件 26 passed，Swarm 受影响回归 460 passed，真实 Codex/OpenCode CLI＋本机
模型 10 passed（scheduled 已用持久 Checkpointer）；core 本轮未改未重跑。恢复故障用例
使用脚本 Provider；部分票据重建为同进程新对象，不冒充真实 CLI 崩溃恢复。生产准入和
活动 Provider pause/resume 仍关闭，真实 CLI 故障/交互/进程重启、远端渠道/Cluster/锁安装
继续待验。详见 R1-11F 验证第 18 节；唯一任务状态源仍为任务跟踪表。


### F3 真实 reviewer CLI 故障验证与候选恢复（2026-10-01）

原临时工作树缺失后，已将 core 41＋swarm 38 个变化文件逐字节恢复到持久
artifacts/r1-11f-recovery/{core,swarm}，与第 18 节哈希及变化集合一致，并备份完整
变化文件。恢复后 Swarm 回归 460 passed，真实 CLI Team/Goal 正向 10 passed；新增
Codex/OpenCode reviewer 的模型拒绝、活动取消共 4 passed，验证原终态、资源退出、
SQLite pending/blocked 的独立进程冷读拒绝、新 invocation 不发起模型重放。

本轮无生产行为改动；这是 reviewer 宿主故障验收，完整 Runner/Goal 故障结算、真实
审批/迟到回答、部分票据跨宿主进程重启仍待验证。生产 scheduled/global guard 与
活动 Provider pause/resume 限制保留，远端模型/渠道 UI/Cluster/锁安装未验。详见
R1-11F 验证第 19 节；任务状态仍只以任务跟踪表为准。


### F3 reviewer 审批与跨宿主进程恢复（2026-10-01）

真实 Codex/OpenCode CLI＋本机受控模型新增 6 项审批/迟到回答、2 项跨进程恢复通过；
与既有 Team/Goal/故障用例合并运行 22 passed。批准、拒绝、审批中取消及错 team/member/
root/cycle、重复/退出后回答均核对。OpenCode 拒绝沿原 interaction_declined 失败终态，
记录 blocked；不把用户拒绝记成评审成功。

两个独立 Python 宿主进程共用原 SQLite board/Checkpointer：已完成 Turn 的 leader 保持
原 Provider Session；A 已投票、B 无票时保持 IN_REVIEW，第二进程只补 B，原 CAS 完成。
这是正常退出后的 reviewer scheduler 重建；消息总线/owner 使用测试夹具，不代表完整
Runner/Goal 进程恢复、强杀恢复或原渠道审批验收。Codex 首次并发启动 SQLite 失败和
未执行 Turn 的空线程无法 resume 已留档，不能按本轮正常恢复结果宣称这些边界已修复。
本轮仅增加测试/说明，无生产行为修改；scheduled/global guard 与活动 Provider 暂停继续
限制保留，远端模型/渠道 UI/Cluster/干净锁安装仍待验。详见 R1-11F 验证第 20 节。


### F3 原 Runtime/Runner 评审审批与根 Goal 取消（2026-10-01）

在持久候选修复已绑定 External Team 的回答分类、CHAT_SEND/CHAT_ANSWER 控制路由和
路由复用；回答仍先过原 generation/interaction ledger。原 scheduler 为临时 reviewer
绑定当轮 Team 输出队列和 Session，宿主使用既有 TeamOutputSchema，拒绝换轮后输出；
仅写 Session stream 无法进入原 Runner 消费链。没有新增队列、审批 registry 或事件 reader。

新增真实 Codex/OpenCode CLI＋本机受控模型允许/取消 4 passed，原 Runtime.stream 经
facade→TeamManager→Runner→scheduler owner；错 generation/owner、重复/迟到回答核对。
取消后根 Goal PAUSED、退出/历史释放、持久状态冷读和未知 reviewer 重放拒绝通过。
原 22 项 CLI 回归、core 52 项、Swarm/Runtime/Single 712 项组合均通过，无失败/错误/跳过。
隔离测试修复数据路径缓存污染，原始失败保留，无白名单扩张。

仍是候选配置/受控 root admission 与 assessor；未验 Web/TUI 传输、远端模型、Cluster、
完整 Runner 进程崩溃恢复或干净锁安装。F3/F4 待验证，生产 scheduled/global guard 和
活动 Provider pause/resume 限制保持。证据见 R1-11F 验证第 21 节。


### F3 Web 授权/历史与根 Goal 故障结算（2026-10-01）

持久候选修复 reviewer 失败后根 Goal 等待 IN_REVIEW 不结束：收到明确失败后关闭本轮
等待器，仍经原 Runner 退出确认、用量核算和 Goal driver 结算，缺用量保持 BLOCKED/
禁止恢复。补 Team 历史筛选的 scheduled reviewer 工具结果/结束文本，保留 invocation/
task/round/Provider 归属；临时 reviewer 不进入同名普通成员私有历史，不加入 roster。

真实 Codex/OpenCode CLI＋本机模型新增根 Goal 故障 2 passed、Web 授权 DOM/历史 2 passed；
原 CLI 26 passed，Swarm/Runtime/Single 720 passed，历史过滤/分页 52 passed，无最终
失败/错误/跳过。Web 测试复用原 WebChannel/WebSocket、React 授权控件/hook 和历史解析器；
Gateway→AgentServer 为进程内桥接，root admission/registry/assessor 为受控夹具。
不等同完整浏览器展示或真实远端部署。临时 reviewer 详情的 UI 展示仍未验证。

core 本轮未改未重跑。F3/F4 仍待验证，scheduled/global guard 与活动 Provider pause/resume
限制保留。独立 Gateway/浏览器、远端模型、Cluster、完整崩溃恢复与干净锁安装待验。
用户 checkout/锁未动，无提交/推送/CI/发布；证据见 R1-11F 第 22 节。


### F3 Gateway 早回执与真实浏览器审批（2026-10-01）

ExternalTeam 审批沿用原 webClient 的 awaitRuntimeAccepted：Gateway 接收回执不消费
pending，Runtime 原控制通道确认后才关闭授权卡；回执补外层 request_id 与本次回答
关联，交互地址和 generation 仍由 Runtime 校验。匹配失败保留原卡供重试，无新审批
注册表或控件。真实 Codex/OpenCode CLI＋本机模型，经独立 Gateway/app_web 进程与
Chrome 146 的原授权控件测试页各完成五次审批，2 passed；原 Runtime 4 passed、
Web DOM/历史 2 passed、前端回执 12 passed、Swarm/Runtime/Single 720 passed，构建通过。

AgentServer 使用原 E2A handler，但服务 owner/root admission/registry 仍由测试夹具
装配；测试页不是完整应用。reviewer 三类历史事件及来源均读回，现有成员解析器只
恢复 worker，reviewer 详情展示/恢复尚未实现，不能算 UI 验收完成。后续应按任务/
review invocation 关联到现有详情视图，不把临时 reviewer 加入 roster 或混入同名成员。
F3/F4 待验证，生产 scheduled/global guard 与活动 Provider pause/resume 限制不变；
用户 checkout/锁未动，无提交/推送/CI/发布。完整证据见 R1-11F 第 23 节。


### F3 reviewer 任务详情与浏览器恢复（2026-10-01）

临时 reviewer 的工具调用/结果、输出/终态和文件沿用原 teamMemberExecutionEvents，
携带 task/round/invocation/Provider 来源；不增加 roster 成员或第二套审批/执行状态。
实时投影可能使用 teammate 角色，按 execution_kind 与审查来源分流；历史与实时使用
相同 ID，迟到空终态不覆盖已有正文。按被审查任务关联到原成员任务 ProcessListCard，
展开显示审查者、轮次、Provider、标识及结果；同名成员和不同 invocation 不混合。

真实 Codex/OpenCode CLI＋本机模型，经独立 Gateway/app_web 与 Chrome 原成员面板
测试页的详情展开、实时/历史对应和页面刷新恢复 2 passed。前端影响面 61 passed，
生产 tsc/Vite 构建通过。历史顶层消息 ID 误作工具 ID 的配对问题已修复并补回归。
core/Python 生产代码本轮未变，不重复引用旧 720 项结果冒充新验证。AgentServer owner/
root admission/registry/assessor 和浏览器外壳仍为夹具，完整产品部署、独立 AgentServer、
远端模型、Cluster/完整进程崩溃恢复/干净锁安装待验。F3/F4 不关闭，生产 guard 与
活动 Provider pause/resume 限制保留；用户 checkout/锁未动。证据见 R1-11F 第 24 节。

## 完整产品候选入口（2026-10-01）

本候选已接通原 App、独立 Gateway 和独立 AgentServer 的同 Provider inprocess Team
normal 路径。首次 Team 名称只允许在执行绑定前冻结；External 命名不分配 Native TinyAgent。
空 Skills 选择为无操作，非空选择仍拒绝。scheduled reviewer 复用原调度/任务管理器与
所选 Provider；活动 Provider pause/resume、process/distributed spawn、worktree、HITT、
Swarmflow 与 Native 专属声明仍明确拒绝，不表示跨机器已实现或验收。

普通 External Team 请求现在进入原 Runtime CHAT_STREAM/CHAT_UNARY 所有权登记，审批继续
使用原 generation/interaction ledger；控制复用已冻结 route，Gateway 只在执行端确认后
推送成功回执。原停止按钮通过实际能力清单识别 Provider：Native Team 保持暂停，External
Team 使用取消。能力查询携带已保存 Work/Code 上下文，首次 runtime_ready 与刷新均加载。

成员通过现有 product MCP 的 send_file_to_user 显式交付工作区内文件，复用 Surface Artifact
与 SendFileToolkit 下载/历史通道，保留成员/Turn 来源与稳定交付 ID。禁止相对路径、越界
和符号链接；批量调用在发送前校验所有文件。Shell 目录变化不用于推断成员归属。

本轮真实模型/故障记录见管理仓验证文档第 25 节。源码联合运行与正式锁定安装分开记录；
跨机器部署及正式配对未闭合时，整个 R1-11F 保持待验证，不以本候选声明生产发布。

旧 Web `team`/`agent` 模式迁移需与已保存 `work_mode` 合并，不能把 Code 历史改成 Work；
能力探测读取持久事实且不回写默认值。已是三段 canonical 的冲突仍由准入拒绝。
远端验收同时检查 Leader 实际输出及刷新后的原回复气泡，用户提示词中的标记不算结果。
OpenCode 验收配置显式使用 `context_window=131072`、`max_output_tokens=16384`，避免默认
小预算只产生推理而截断；运行时仍由 Provider 报告真实截断失败，不静默重放。
