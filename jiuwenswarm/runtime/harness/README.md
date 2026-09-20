# R1-01 execution construction

Use `parse_execution_config` for a dedicated execution configuration, not model-provider settings. Pass complete snapshots through `ExecutionConfigSource` (explicit > project > default) to `prepare_execution`, together with a host-owned `ExecutionBindingStore` and an authorized subject/session/absolute workspace.

`ExecutionConfigCatalog` freezes server-owned profiles and exposes only their
identifiers for selection. A chat request may name a profile ID; it must not
supply raw `provider_config`. The caller validates the subject and any Project
candidate before calling `catalog.source(...)`. Unknown explicit IDs fail
without changing to a different engine. The catalog is a selection primitive;
the current Runtime request route does not yet load or use it.

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

When the section is absent the loader returns `None`; malformed configured
sections fail validation. Reading this section alone does not enable the new
chat route or expose a new selection UI.

The result is an unstarted core `HarnessEngine`. No existing chat or Team routing is changed in this slice. The Runtime caller will own `start`, the single event consumer and `stop` in R1-02; it must validate HarnessContext identity against the binding at that integration boundary. No provider instances are cached here.

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
`io.outputs()`; the eventual Web route must choose the envelope iterator and
keep one consumer for the entire session. The current default chat route has
not yet switched to it.

`NativeExecutionSession.enable_turn_outputs()` opts into a session-level
`TurnOutputRouter` before the first input. `send_request` registers its Turn
while submitting to the provider; `turn_outputs(receipt.turn_id)` yields the
original projected chunks and one terminal envelope, then ends. The mailbox
is bounded; closing a request iterator releases that mailbox while the sole
session reader continues. Output not attached to a live request still reaches
the protocol event observer, which the eventual product route must connect to
its durable history/UI projection. If a request fails before opening its
iterator, call `abandon_turn_output(turn_id)` to release the mailbox. Do not
also read `io.outputs()` or
`io.output_envelopes()` after opting into this router.

The binding store is still owned/released by the Runtime caller. Runtime
must enforce authorization and session generation before passing answers;
this class is neither a new persistence layer nor a replacement for generation.

The session adapter also has `start_native_interaction` and an exclusive legacy
`start_interaction` path. Its `stop_interaction` releases the selected binding
after the protocol session stops. The current warm pool still starts the legacy
path unconditionally, so this is an **explicit integration entry**, not a
change to the default Web/TUI chat route. The warm pool selection and request
route, output handoff across Web disconnect, historical replay and real Native
preservation acceptance remain before R1-02 completion. Passing deterministic
adapter tests is not live-model E2E.
