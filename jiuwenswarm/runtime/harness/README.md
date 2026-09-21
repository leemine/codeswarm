# Execution selection and Native Single integration

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
building a second instance. Team routing remains on its original Runtime.

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
packaged default configuration or existing user config files. External
providers and Team selection have no product route in this slice.
