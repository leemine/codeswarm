# Governed Single OpenCode continuation

This slice connects the existing B3 private-Session transaction to OpenCode's
managed model gateway. It adds no RPC, protocol field, persistent state store,
Provider queue, or source-Session fork.

## Admission and projection

ContinuationTargets accepts a bounded OpenCode Single normal combination only
when the existing compiler accepts the exact profile. The Provider's requested
mode must be absent: core does not compile explicit External mode overrides,
including `normal`. This is independent of the product's `agent.work.normal`
or `agent.code.normal` Surface. Native retains its previous absent/normal modes.

The OpenCode model name and endpoint must exactly match the recipient's selected
catalog binding. Its Provider configuration contains no upstream API key,
portable skill or additional native plugin. Effective full access is rejected.
The installed core source-proof methods and Session model-gateway binding port
must exist. Current target project read/execute, Workspace/read and the selected
credential/use remain required. This admission does not grant unknown tools,
process isolation, product tools, MCP or delegation resources.

Safe continuation options now report the validated target Provider instead of
hardcoding Native. The existing profile/model fields, final delivery checks,
creation token claim and owner-sidecar publication are reused unchanged. No
credential reference, endpoint or seed is added to options or result payloads.
The frontend options decoder accepts only the supported `native` and `opencode`
Provider ids. Both use the original project/profile/model selector and exact
returned tuple; Codex and unknown Provider ids are rejected. The frontend does
not infer capability, substitute profiles, or authorize an omitted candidate.

## Seed consumption

Runtime already supplies a sealed ContinuationContext for each admitted request.
EngineAgentAdapter captures that original object before the ordinary Turn's
first await, checks target Session/request and complete identity against its
captured model authority, and associates the digest with the exact private
ExecutionSession and immutable Binding. A known continuation without its handle,
a changed identity/digest/request/Session, or adding a seed to an already-running
unseeded context is rejected.

Cold startup renders only the approved user/assistant plaintext as quoted JSON
inside the existing HarnessContext.system_prompt. Delimiter characters in
historical text are escaped. The text explicitly carries no new instruction,
tool result, credential, or permission. No source history file, checkpoint,
source Binding, tool transcript, or child-Agent history is imported. No callback
or authorization proof is serialized to Provider metadata.

The existing system context is retained across Turns. New user input remains
only the new user input; the seed is not appended as another user message each
Turn. Preparation/startup awaits and the final send boundary revalidate the same
request handle and digest. Existing interaction answers continue their original
Turn and model authority; they do not create or reseed a new Turn. Model HTTP
consumption additionally revalidates the original B3 source/target/model approval
through the already-bound Runtime factory and ResourceGuard on every request
and response chunk.

A new B3 target receives its own Provider Session. The existing recovery path
may resume only that target's compatible, confirmed-idle checkpoint; this change
does not import the source checkpoint or implement full activity recovery.

## Evidence and remaining gates

The deterministic test `tests/unit_tests/runtime/test_opencode_continuation.py`
uses real project/source sidecars, ContinuationCompiler, Runtime transaction,
creation-token handling, Coordinator CHAT_STREAM admission, Runtime resource
factory, context scope, adapter and private ExecutionSession. Provider startup,
send and outputs are synthetic. It checks safe options, exact model selection,
ordinary two-Turn context composition, recipient credential use, unchanged source
and Binding, repeated-token identity, and rejected source-handle drift.

The test is appended to discovery and execution in the existing strict stable
continuation suite. These component tests do not establish a real browser B3
story or real OpenCode seed consumption. The separate c4254440/fba69354 ordinary
read/write/edit model-gateway story predates this seed change. A new integrated
locked-source pairing, affected stable checks and ordinary independent-login UI
continuation/seed/model story are required before marking this slice accepted.
Team, Codex's mandatory native-tool blocker, complete activity recovery and
remote deployment remain separate and are not closed by this change.

The first real B3 UI attempt on swarm `29fdfea4` / core `835d9ebb` reached a
successful OpenCode options RPC but exposed the frontend's Native-only decoder.
It stopped before target creation or any model/CLI operation; owned services
were cleaned. The decoder follow-up includes actual API decoding and the
existing selector component submitting an OpenCode tuple, plus Native and
unknown/Codex rejection regressions. That fix still requires a rebuilt,
integrated candidate and rerun of the ordinary UI story; component/build checks
do not replace it.

### Cold capability reads and the first protected Turn

`surface.capabilities.get` reads a retained adapter's manifest when present.
Before the first actual Turn it validates the persisted execution profile
revision/fingerprint and Surface metadata, then projects the configured Provider
inventory through the existing capability/UI compilers. The temporary projection
values never enter the Binding store, Workspace allocator, recovery store or
Agent cache. No model gateway, credential authority, Provider or Session is
constructed by this read.

This preserves the first real chat's Runtime-owned factory scope. A legacy
adapter without the original protected model gateway still fails closed if it
is later submitted under mandatory authority; capability reads do not upgrade
such adapters. Cold manifests describe configured capabilities, not resource
permission grants or proof of successful Provider execution. Required ordinary
B3 browser/CLI validation remains separate from the deterministic cold-read →
real AgentManager/Runtime factory regression.

### Web terminal projection

The ordinary Web send path preserves the existing typed `terminal_status` on
`chat.final`, using the shared terminal-outcome parser. External FINISHED sends
an empty final packet: the preceding `chat.delta` carries the answer, and the
existing Web UI closes that same request's stream without replacing accumulated
text. A final packet is not required to repeat the answer. No arbitrary Provider
fields are added to the Web text-event projection.

`chat.error` already uses the full-payload path; failed/cancelled/unknown status
and error codes were not lost there. The deterministic regression runs actual
EngineAgentAdapter output through MessageHandler and WebChannel.send into a
synthetic socket, covering the completed packet plus existing error paths. It
also checks that invalid terminal values and private fields do not leak through
the final text projection. This is transport-contract evidence, not a browser
or real Provider acceptance result.

The ordinary 4144dc9d / core 8040687b B3 attempts retained separate evidence for
the original manual-approval assumption, the final-text-only probe assumption,
and the subsequent observed loss of completed metadata. None completed the
required second Turn and normal UI deletion story. A newly integrated candidate
must still pass that full ordinary UI story; this projection fix does not close
B3, change approval policy, or authorize new real negative probes.
