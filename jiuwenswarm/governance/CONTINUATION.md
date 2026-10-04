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
