# Sharing mutation audit foundation

This slice records confirmed sharing create/update/revoke and continuation
publication commits. It does **not** complete R2-B4 audit acceptance: request
admission, denied access, read/execute consumption, delivery, resource/Turn and
artifact references, query and UI remain integration work.

## Storage and authority

`project_extensions.json` retains its original schema, lock, load and atomic
save. A top-level `sharing_audit` schema 1 contains `next_sequence` and `events`.
This domain is a history, not an authorization source, durable lease or second
execution state machine. `_inventory_revision` excludes only this exact top-level
key; project/ACL/share/owner/source/resource/continuation/deletion facts remain
part of the inventory proof. No database, JSONL sink, queue or lock was added.

The locked snapshot gets both the authority change and audit event before one
original `_save`. Revise never resaves its outer stale snapshot. Publication
commits append after the original validations and in the same save that changes
pending to committed. Already committed retries do not append another event,
including legacy committed records that have no audit history.

Absent audit is legacy history not recorded by this version; the first mutation
starts sequence 1. It does not invent old events. Unknown/malformed schema,
sequence, record fields, identities or bounds are rejected and never reset.
Original latest-state share records remain available to existing consumers.

## Typed data and call compatibility

`server/runtime/session/sharing_audit.py` provides frozen `SharingAuditContext`,
`SharingAuditBounds`, `SharingAuditFacts` and `SharingAuditWriteResult`, plus a
pure `append_sharing_audit(data, context, facts)` function. It validates the
existing domain and stages an event without IO or another lock. Its immediate
receipt says `persisted=False, reason=audit_staged`; only the post-save observer
gets `persisted=True, reason=audit_persisted`.

An event has a local sequence, generated event/attempt identifiers, finite time,
optional original request ID/method, full trusted actor and target identities,
exact source/target Session and registered project references where known,
share revisions, old/new bounds, original parent/source/owner revisions and the
actual decision revisions. Revoke can act on an old pinned source grant, so its
original and current decision revisions are deliberately separate. Publication
adds its existing ID, nonsecret seed digest, target ACL revision and approved
view/execute range. It does not claim that model execution or delivery occurred.

Identity uses all authority/actor/subject fields and preserves normalized Unicode
within the 1024-character audit field bound. Optional host request context must
match the operation's real actor and method. No raw params, credentials or
credential references, paths, messages, seed body, model entries or exceptions
are copied. A JSON event has a 32 KiB bound. Event order uses sequence, not time.

`grant`/`revise` still return dicts; `revoke` still returns int. Each accepts
optional keyword `audit_context` and `audit_result` callback. A legacy host call
without context records its real mutation actor with null method/request ID;
null means host API, not an invented RPC. The server-side publication
`commit(scope, *, audit_context=None, audit_result=None)` provides the same
optional seam. The existing governance scope's `commit()` remains unchanged and
uses the host-API path. These new contexts/results are not wire DTOs.

The callback receives only the frozen status/sequence/event ID after the save
and after the store's own lock context exits. Failure, including a callback
raising cancellation, emits a fixed warning and does not replay or invalidate
the already saved mutation. Caller cancellation before/during the original save
is not intercepted by this notification helper. Callers with an enclosing
sidecar guard still own that guard and must not await inside it.

## Failures and boundaries

Positive grant/update/publication commit refuses malformed audit or failed save;
no success callback occurs. A rename-before-commit failure leaves the old
authority and audit version. Original platform/fsync behavior is unchanged:
the existing directory fsync helper can swallow errors, so this slice does not
claim newly guaranteed power-loss durability across platforms.

Revocation must not be blocked by a separately damaged audit domain. After the
original authority and CAS checks it preserves that domain verbatim, persists
the revoke, emits `sharing_audit_degraded: audit_storage_invalid` and calls the
optional observer with `persisted=False, degraded=True`. This is **not** audit
success. If the authority file itself cannot be saved, revoke still fails;
neither a success callback nor a false claim of persisted revocation is made.
Existing callers without an observer see only the fixed warning; RPC/UI
degraded-status projection is not implemented in this slice.

The original optional auto-permission JSONL and its observe-only contract are
unchanged. They cannot replace this atomic mutation history or prove missing
consumption/delivery events. There is no audit query/export endpoint or UI yet.
No automatic trimming occurs. Since the sidecar loads and saves whole JSON,
growth increases authorization/load and write cost; long-term capacity,
retention and bounded querying need a separately frozen policy before claiming
an indefinitely scalable audit service.

## Behavioral verification

The new real-store suites are `test_sharing_audit.py` and
`test_continuation_publication_audit.py`. They exercise single-save mutation
history, exact old/new bounds, concurrent CAS, failed atomic replacement,
corrupt audit with effective revoke/degraded result, full Unicode identity,
nonsecret projection, callback failures, unchanged public return types,
publication idempotency and pending-on-failure, and inventory proof exclusion
that still detects all other authority changes. They use isolated files and
synthetic identities; they do not stand in for real Provider/UI audit acceptance.
