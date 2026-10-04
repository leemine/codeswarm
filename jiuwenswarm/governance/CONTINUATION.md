# Authorized continuation source contract

This is a private swarm source compiler, not the B3 Session transaction or an
execution endpoint. It does not allocate a Session, configure a Provider, bind
credentials, copy historical events, run tools or change the current UI Session.

`ContinuationInput.from_wire(params)` accepts exactly the source Session/share,
expected share revision, create token, target project, execution profile, Single
mode and optional title. No identity, directory, range, cursor, native Session,
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

`ContinuationPublication(host, compiler, seed, config_fingerprint)` is a one-use
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
