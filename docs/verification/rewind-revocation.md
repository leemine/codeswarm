# Existing Single rewind authorization at consumption

Scope: `session.rewind`, `session.rewind_context`, `session.rewind_and_restore`,
`session.rewind_compact`. This closes host-side wait/write gaps in already-open
organization methods. It does not implement full activity recovery, Team
rewind, remote deployment, or a new Provider context protocol.

## Authority and retained behavior

AgentServer captures the original owner permit, authenticated context, exact
wire facts, durable binding/channel, existing cached Agent/child/DeepAgent/
ReactAgent/context engine/live Session, original context, and Coordinator
generation/execution IDs before the extension hook. After waits it checks the
same facts; it never allocates, warms, chooses a default Agent, or selects a
new generation for organization rewind. Team/missing or changed binding and
missing existing context engine fail closed. Legacy single-user resolver
fallback and normal asynchronous persistence remain unchanged.

The private owner-only guard revalidates the original continuation provenance,
owner revision, current identity and project write decision. History truncation,
compact records and metadata use their original synchronous atomic writers and
locks, including a check after staging and before atomic replacement. Permission
denials propagate through former best-effort catches. File restoration checks
each write/unlink. Context rebuild pins the original objects and checks before
and after each host-awaited core operation and before state/save/commit calls.
Rewind remains a sequence of existing operations, not a rollback transaction:
revocation stops subsequent actions; previously authorized effects are not undone.

## Lock-order audit

The guard only accepts an owner `SessionRequestPermit`; shared-history,
inventory, continuation RPC and cleanup permits are rejected. Its permit checks
call `SharingHost.owner_current/owner_revision`, `_current`, `_live_binding`, and
committed continuation `guard`/`ContinuationCompiler.revalidate`. These read
original sidecar records and atomically published metadata/lifecycle files.
They do not acquire history or lifecycle resource locks, drain writer queues,
or read seed/history content. Project write uses the host's original access
store. Coordinator snapshots and cached object selection are synchronous.

History keeps its original FILE → Session resource-lock ordering. The callback
inside those original write points only briefly acquires the original reentrant
sidecar lock. Metadata keeps its original Session → metadata FILE order. No new
combined lock, cross-await lock, queue item field, persistent grant, or request
queue is introduced. Source preparation releases the sidecar before history IO.
Do not replace this guard with a generic shared-history delivery checker.

## Validation and precise limits

Baseline swarm `bd7ac7cd`; tests used its isolated source with the formal
non-editable core `26255e7b7502c46cd5611ee2179be3c5df2ec1cc` environment.
This is local affected-source verification, **not** acceptance of the baseline's
newer `f6c56838b7b65a22c70cb012ea437b5b96680af2` lock pairing. The installed
ContextEngine file and that newer core Git blob have the same SHA-256
`b9ef34a0edf4101c6da79d32af30f11ec8149dee6707d40872088a2aa7c7444b`.

- Affected history/metadata/compact/resolver/legacy suite: 243 passed.
- Final new authority suite after the last narrow checks: 32 passed (includes
  normal guarded context rebuild, actual sidecar/history, four hook barriers,
  original binding/object/generation changes, staged-file revoke, context
  waits, and original denial propagation).
- Evidence and exact commands/source hashes: `/tmp/r2b-rewind-revocation/`.
- Initial sandbox run timed out because the fixture's asynchronous IO completion
  could not wake the event loop; the same bounded isolated suite ran outside
  that sandbox. One intermediate test omitted required `timestamp`; corrected
  fixture results above supersede that failure. No acceptance tests were removed.

Actual `ContextEngine.clear_context` synchronously removes its context before
awaiting `CONTEXT_CLEARED`; it has no before-event decorator. The actual-core test
revokes inside this after-event and proves no later rebuild/state/commit occurs.
It is incorrect to describe this event as a pre-delete await or claim that the
host can undo the earlier authorized clear. Core create/save after-events and
an already-entered external persistence await likewise are not retroactively
undoable. These tests prove host boundaries, not cancellation of arbitrary
in-flight Provider/checkpointer side effects. No new real Provider adversarial,
UI, stable, or final lock-pair acceptance was run in this independent slice;
those remain integration responsibilities. Full B4 is not closed by this slice.
