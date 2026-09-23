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
event consumer and one `TurnOutputRouter`, with tool auto-approval disabled.
It validates the bound runtime root and cwd before start and stops only its own
resources. It lazily starts the Provider on the first Turn, using only the
admitted runtime paths and immutable binding; construction and execution never
fall back to DeepAgent.

`context_bridge` builds a per-Turn input snapshot. It reads bounded project
rules only when their resolved paths remain under the admitted runtime root.
Session uploads are accepted only from that session's upload directory (or
when already inside the runtime root), bounded by count and byte limits, copied
to `.jiuwenswarm/session-inputs/<session>/<request>`, and removed on Session
cleanup. Arbitrary request paths and symlink escapes fail closed. The existing
fixed PersonalContext publication is read only when its product switch is on;
the External path does not construct Native rails.

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

## Codex native plugins (R1-03C1)

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
product ToolGateway calls; deployment, C2 and the B1 product-tool path remain
separate concerns.

## Product ToolGateway and managed MCP (R1-03B1)

`ProductToolGateway` adapts an explicit catalog of existing product tool
instances. Definitions, invocation, output rendering and tool callbacks remain
owned by those instances; the gateway adds no duplicate tool implementation.
Every gateway is bound to one subject, parent Session and absolute workspace.
Those values come from the admitted ExecutionBinding and cannot be replaced by
model arguments. Non-parallel-safe tools share a per-gateway serialization
lock, while an optional host admission callback can reject an invocation.

An External provider with native ToolGateway support receives the gateway
directly. A provider such as Codex receives one Session-owned Streamable HTTP
MCP endpoint bound to 127.0.0.1 with a random Bearer credential. The endpoint
is required and uses prompt approval in the Codex profile. Readiness completes
before provider startup; startup failure, normal stop and router failure close
only that Session's endpoint. The reserved product server namespace cannot be
overridden by request MCP configuration.

B1 deliberately stops at this injectable boundary. B2 later supplied the
provider-neutral child port, B3 the Codex child composition, and B4 the six-tool
product/channel acceptance. This module is not a general MCP registry and does
not import Team runtime or reuse Team operator permissions.

## Codex child execution (R1-03B3)

`CodexSubagentExecutionFactory` is the Swarm composition for the core B2
`SubagentExecutionFactory` port. It accepts one admitted Codex parent route and
captures that route's exact `AgentExecutionSpec`, Binding, subject, Session and
`RuntimeWorkspacePaths`. Each child receives its own `subagent:<id>` subject,
host Session ID, Binding, Codex harness and SDK/CLI session while retaining the
parent configuration fingerprint, workspace root and task cwd. There is no
child Provider, profile, model, workspace or cwd override input.

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

`ExternalSubagentRuntime` is the Codex parent composition root for the existing
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
