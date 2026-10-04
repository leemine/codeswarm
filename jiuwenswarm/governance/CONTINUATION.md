# Authorized continuation source contract

This is a private swarm source compiler, not the B3 Session transaction or an
execution endpoint. It does not allocate a Session, configure a Provider, bind
credentials, copy historical events, run tools or change the current UI Session.

`ContinuationInput.from_wire(params)` accepts exactly the source Session/share,
expected share revision, create token, target project, execution profile, Single
mode, explicit model selection and optional title. No identity, directory, range, cursor, native Session,
credential or arbitrary metadata may be asserted through this input. Session
allocation and token scoping still belong to Runtime/AgentManager.

Construct `ContinuationCompiler(host, identity_resolver=..., project_authorizer=...)`
with the existing `SharingHostService`, trusted current identity resolver and
instance project authority. Its synchronous `compile(input)` returns an immutable
`ContinuationSeed` with original proof, ordered pure-text messages and SHA-256
of canonical version/proof/messages. Constructing these values is not authority:
never deserialize an untrusted request or persisted payload into a valid seed.
The hash is an integrity comparison, not an authentication signature.

The exact source share must currently grant both view and execute. Source host
execute is available only when the trusted source owner currently has project
read, admin and execute. This does not give the recipient source-project rights.
The recipient's target-project execute is checked independently. Full trusted
identity, owner/source/share revisions, parent revision, expiry, fixed physical
range and target execute revision are pinned and checked again around every
page and after seed construction. Regranting cannot replace the old proof.
Append-only growth outside the share's range does not enlarge its seed.

Viewer and compiler share `visible_conversation_text`: ordinary user text and
assistant final/legacy text only, excluding tool, reasoning, file, unknown event
and child-agent streams. Projection does not resolve any attachment or tool
payload. The complete selected stream is ordered oldest first. Limits are
explicit constants: 1 MiB UTF-8 text, 1,000 messages, 8 MiB selected source bytes
and 256 pages. Exceeding any limit rejects the whole compilation; no truncated
success is returned. Reading uses the existing JSONL inode/boundary validation.
No sidecar lock spans history reading or queue draining.

Call synchronous code through the existing `run_history_io` offload from the
original authenticated context. The resolver must check live credentials, not
merely return a cached identity. Cancellation does not authorize a detached
worker's output. Runtime must call `revalidate(seed.proof)` after every wait,
before business commit/result delivery, every later Turn and resource consumer.
Provider context can remember previously seeded text, so successful first use
does not remove the source authorization dependency.

Target configuration fingerprint, actual Workspace/model/credential/tool grants,
private Binding/Session, pending owner publication, durable seed transaction,
idempotent create token handling, request generation and late approvals are
**not implemented by this compiler**. Runtime must enforce them independently.
Unsupported Provider combinations remain denied. No model/Provider or UI
acceptance is established by deterministic compiler tests; B3 remains incomplete
until its integrated and real execution milestones pass.

## Persistent publication owner boundary

`ContinuationPublication(host, compiler, seed, config_fingerprint, target_snapshot=...)` is a one-use
lexical synchronous context manager owned by the current asyncio Task. During
its scope, the existing `register_owner_and_source` attaches a pending
`continuation` record beside `source` in the original owner sidecar, only for
one fresh Session belonging to the seed's target identity/project. Initial owner
revision/source epoch must both be one. Inherited ContextVars do not grant child
Tasks or worker threads this privilege, and scope exit always deactivates it.

The same Task can perform internal owner/revision checks while pending. All
ordinary checks in another Task or after restart reject pending targets. Call
`scope.write_seed()` after ordinary metadata exists; then `scope.commit()` before
reporting transaction success. Both synchronous methods reject an already held
sidecar lock. They do not allocate a Session or own a Runtime lease. Compile the
source seed through existing authenticated history IO beforehand; do not offload
the Task-owned publication scope into a worker.

`continuation.json` lives in the existing private target Session directory and
contains only version, strict proof, role/content messages and canonical digest.
It uses the existing lifecycle atomic JSON writer plus directory fsync. Existing
identical content is idempotent; different/corrupt content is never overwritten.
Commit reads and validates the complete bounded seed outside the sidecar lock,
then checks metadata (exact profile, config fingerprint and explicit model),
original source proof and publication nonce/revision/epoch again under the
existing sidecar lock before changing pending to committed. Model may be empty
for compiler fixtures; production Runtime must require an explicitly authorized
model and never inherit a source/default credential.

Committed owner checks receive no lexical privilege. They reconstruct strict
persisted proof and revalidate the current source share and target authorization,
including after a new host is constructed. Recursion follows only the exact
source share, rejects repeated Session identities and is bounded to eight
continuation ancestors. The ordinary owner guard neither reads seed/history,
waits, nor writes the sidecar. `read_seed(host, session_id, identity)` separately
requires committed current ownership before and after bounded file reads and
checks all fields, canonical hash, message/text/file limits and regular-file
identity. File limit is 8 MiB, covering escaped JSON plus the 1 MiB text limit.

The existing CAS compensation can tombstone pending owner reservations; it
cannot erase a newer generation or a committed continuation using an old prepare
receipt. A lost response after commit does not roll back the target. Runtime
must recover the same idempotent result under fresh authorization, or use the
normal delete/revoke lifecycle for an explicit later withdrawal.

These host primitives do not implement the Runtime transaction, create-token
retry results, Provider binding/credential grants, input injection, UI or active
execution cancellation. Missing/invalid persisted data denies access; it is not
silently migrated or accepted as a second permission database.

Registration checks the same sidecar snapshot for any earlier continuation with
that complete trusted identity and create_token, including pending, committed
and retired owner records. Such a token cannot allocate a second target even
when the new input differs or a different Runtime/process races. This uses the
existing file lock and owner records, without another index or lock. Runtime
must resolve an authorized committed result before attempting registration;
pending conflicts do not transfer the old scope or expose a partial Session.

Safe seed persistence currently requires POSIX O_NOFOLLOW, O_DIRECTORY and
O_NONBLOCK. Unsupported platforms fail before continuation owner registration;
flags are never silently dropped. Ordinary owner operations remain unchanged.

A retired owner containing continuation provenance permanently reserves that
Session ID: ordinary registration cannot overwrite it and discard its token
tombstone. New continuations use new random IDs. Existing non-continuation
retired owner recreation retains its previous behavior.


## Runtime creation and consumption integration

`AgentRuntime.continue_session(ContinuationInput)` uses the existing provisioner,
claim-token lock and owner publication. It compiles the complete source before
allocation, selects an explicit eligible Native Single profile/model and checks
recipient project read/execute, canonical workspace/read and credential/use.
Prewarm is disabled for this transaction. The original nonsecret model binding
and full entry fingerprints, workspace, work mode and canonical mode are stored
in the same publication record. Current catalog defaults cannot replace them.

Creation flushes metadata, atomically writes the seed and commits publication
inside the original provision receipt's finalize lock before returning success.
Failure before business commit invokes the original abort finalizer. A temporary
compensation outage retains ABORTING and the receipt for owned retry; confirmed
business commit forbids old-receipt deletion. Unknown disk-write outcomes retain the original receipt until its exact nonce,
proof, seed, metadata and original owner/source epoch prove the write committed.
That fact check can settle the original receipt on same-token retry or shutdown;
it never grants source access or deletes a committed target. A still-unreadable
or mismatched outcome remains pending and prevents completed shutdown.
The full trusted identity and complete normalized source/target inputs scope the
original create token. Retrying a committed token checks the original durable
record and returns that same target; changing inputs conflicts.

`ContinuationExecution` is private per-admitted-request state. It freezes the
original Runtime execution ID and Session generation and rejects cancellation,
terminal state, replacement generation and a different request identity. Its
checks also reread original source/owner proof and target catalogs/grants.
`ContinuationContext` carries the bounded plaintext seed into the original
Single adapter route. Under the existing adapter lock, new/rebuilt contexts get
source seed plus the target's own history before the current request. Existing
contexts are only revalidated, never appended to; the seed is not rewritten into
the target's conversation history. Failed/late context construction removes only
its newly created default context and propagates denial.

The primary model branch builds the exact approved metadata entry without
legacy name/default/cache/login fallback. Its immutable entry fingerprint flows
through the existing Native model request authority. Each actual credential use
checks the original binding and entry plus the live original Runtime/source
facts before and after awaited resolution. This is catalog/factory selection
integrity, not a new policy for arbitrary model sampling parameters.

## RPC and final local delivery

`session.share.continuation.options` accepts exactly source session/share,
expected revision and target project. It returns only currently eligible profile,
provider, canonical mode, model selection and display label combinations. No
credential reference, endpoint, key or seed is projected. `session.share.continue`
accepts the exact `ContinuationInput` fields and returns safe target Session
metadata plus `continued_from`; it does not send a chat Turn or subscribe to the
source Session.

Both source permissions must belong to the same original share/range/revision.
AgentServer checks a private result guard after its send lock. At the Gateway,
the existing queued frame carries a new local guard compiled from the same
persisted host sidecar, original request and current full principal. It rechecks
the committed target owner, original input/token, target configuration/resources
and source permission immediately before websocket delivery. The response itself
does not carry a grant and cannot reconstruct a callback. Remote authorities
without the same authoritative local state are outside this combination.

Deterministic transaction, context, consumer and delivery tests plus ordinary
Native loopback evidence establish separate slices. UI, final locked candidate
stable and full real Provider/channel stories remain explicit delivery gates.
The initial target selector supports configured Native normal Single with empty
provider configuration and API-key OpenAI Chat Completions metadata only. This
does not open Codex/OpenCode continuation, Team continuation, autonomous child
models, full activity restoration, Swarmflow or remote deployment.


## Owner cleanup after source revocation (integration slice)

The cleanup-only owner stamp does not call the continuation source grant, project
read/execute ACL, or general owner view authority. It pins the original full
trusted identity, owner revision, source epoch, target binding and lifecycle
facts. Only pure cancel parameters use this decision. Pause, resume, supplement,
history, tools, model calls, sharing and downloads retain their normal gates.

AgentServer captures the cleanup request before awaited dispatch. Runtime pins
the original Coordinator generation and executions and refuses a newer Turn.
It does not resolve an Agent from project/mode hints or create one for cleanup.
An optional resource-release phase on the original Coordinator close keeps the
Session QUIESCING until the existing cached Native owner stops and its recorded
owned tasks have exited. Concurrent closers join that close; caller cancellation
cannot abandon it; timeout/failure keeps the fence and cached owner for retry.
Only then may the original binding/cache bookkeeping be released and an
`exit_confirmed` cancellation success be returned. This is not a new scheduler.

This slice still needs Gateway/UI integration and exact full validation. Native
strict host task drainage compensates for the presently observed core scheduler
stop not joining each execution task; it does not claim a core fix. Other Provider
strict stop ports, automatic active revocation propagation, permanent-delete
completion receipts, and the full R2-B4 acceptance story remain open.
