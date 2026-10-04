# Single deletion receipt ports

This is the host authority slice for B4, not a complete deletion endpoint or Provider acceptance. Progress remains in the existing `lifecycle.operation`; the existing project sidecar stores one `owner.deletion` receipt. There is no new store, queue, or lifecycle state machine.

## Private host API

- `capture_deletion(session_id, identity, permit) -> DeletionCapture`: read-only, exact real `SessionRequestPermit`, Single `session.delete`, full live identity, canonical parameters and original cleanup stamp. Must run before this request changes lifecycle generation. Its `descriptor` returns a copy of the validated binding for choosing the original project lock. Runtime SDK callers form the same trusted SessionBoundary permit.
- `begin_deletion(capture, operation) -> DeletionReceipt`: caller holds the existing project then Session execution-owner locks and has begun/claimed the existing delete operation. Matches that actual operation and original owner/binding; a single sidecar CAS/save invalidates source once and writes the receipt. Repeating this same capture is idempotent; another capture cannot replace it.
- `resume_deletion(session_id, identity, *, identity_resolver) -> DeletionReceipt`: same original full identity only; reads the persisted receipt even when metadata is gone or owner is retired. Returns an admission-only handle with the actual same-operation generation frozen in memory. Its `descriptor` is a private copy for selecting the original project lock, never a UI payload.
- `check_deletion(receipt, *, for_admission=False) -> None`: default rejects admission-only and retired handles; the active adopted receipt is required for destructive cleanup. `for_admission=True` checks a read-only recovery handle or exact retired ACK fact. Both recheck live original identity, sidecar authority and exact observed operation generation. No current/latest-generation fallback.
- `adopt_deletion(receipt, operation) -> DeletionReceipt`: after acquiring original execution-owner locks, explicitly claim the same lifecycle operation and CAS its stored generation to the frozen observed/current claim generation. A claim can advance the observed generation by one; resumed observations can account for an earlier process crashing between lifecycle claim and sidecar adoption. Does not change source epoch, owner revision or nonce. Old handles become stale. An already-retired receipt may adopt the same operation only to finish lifecycle facts; it still cannot pass destructive check.
- `commit_deletion(receipt) -> None`: only after both active/archive target directories are absent, retire precisely the original owner and retain the receipt. Repeated retirement is idempotent. An unadopted nonretired receipt cannot commit.
- `confirms_deletion(receipt) -> bool`: exact retirement/operation/filesystem fact only; does not require live request credentials and never grants read/execute or delivery permission. Use to reconcile a sidecar save that committed then raised. Read outage returns false, not success.
- `confirm_deletion_for_permit(permit) -> bool`: content-free final ACK proof. Requires exact Single delete/live full identity/canonical parameters, retired original receipt, lifecycle resource `deleted=True` and embedded operation completed, and no recreated target directories. Initial permit must match the entire original stamp and initial generation; retry permit's private `deletion_receipt` must match its original nonce/record/observed generation. Generation takeover invalidates old ACK permits. Callers still pin original connection, request ID and same permit at enqueue and final sink. No method-only exemption.

## Storage and lock boundary

The strict receipt records full identity, original owner revision and complete cleanup stamp, fixed invalidated source epoch, source/project generations, immutable minimal binding, canonical original request parameters, unique deletion ID, existing operation ID, initial and adopted generation. All fields are private host facts; none may be granted by a wire payload. There is no credential, Provider state, history or message copy.

The caller owns original project → Session execution-owner serialization across awaits. These synchronous ports take only the existing sidecar lock and read atomically published lifecycle/metadata files; they do not acquire lifecycle execution locks, await, drain history or recursively save. Callers recheck after every await and under the original locks. Missing metadata is accepted only for the exact receipt/operation; a remaining partial directory additionally requires the existing destructive phase. A retired deletion Session ID cannot be reused through either owner registration port.

Normal owner/view/execute/share/history and `cleanup_owner_stamp` continue to reject retired/missing-metadata targets. Read-only admission checks must never be passed as a destructive Provisioner guard. If retirement succeeded before `lifecycle.complete`, adopt the original operation, confirm the exact retirement fact, then finish lifecycle/ACK without re-running Provider/resource deletion.

## Validation and limits

Tests use real temporary sidecar, metadata and lifecycle files with synthetic identities and injected save/read failures. They cover one-save invalidation, retry/new host, save-before-failure and save-after-commit failures, read outage, strict actor/subject/authority, owner/epoch/binding/operation drift, partial deletion, concurrent sidecar CAS, worker/task checks, explicit claim adoption and its crash window, retired-before-lifecycle-complete recovery, no Session ID reuse and exact original/retry permit ACK. Source tests are not fresh noneditable locked-install acceptance. No real Provider or remote service is invoked.

Runtime/SessionArchive/Provisioner/AgentServer/Gateway integration and actual Native task exit remain separate required verification. The ports do not make background recovery a trusted user, reopen source permissions, implement archive restore, or broaden this slice to Team, cron or bulk deletion.

A restart retry that claims/adopts a newer lifecycle generation intentionally cannot send success using its old pre-claim permit: the exact final ACK check rejects that permit. A new incoming retry against the same service observes the now-stable generation and can confirm the completed result. This is safe but does not claim first-retry delivery succeeds across a takeover. Tests cover old-permit rejection and a new same-generation receipt ACK; no automatic latest-generation substitution is allowed.

## Original receipt audit transactions (2026-10-04)

`SharingHostService.begin_deletion`, `adopt_deletion`, and `commit_deletion` now
accept optional host-created `audit_context` and post-save `audit_result`
keywords. They retain their existing result types. The original receipt supplies
full identity, source/owner revisions, Session/project, operation and generation;
wire fields cannot select those audit facts. A missing context records a real
host API call with deterministic receipt/phase/generation attempt correlation.

Begin appends only `exit_requested`; adoption appends `cleanup_retry`. Both stage
the event in the original locked snapshot and save it with their original owner
mutation. Bad audit blocks these new destructive admissions before persistence.
No new lifecycle owner, queue, index or database is introduced. Repeating one
attempt returns its original audit event; explicit distinct attempts remain
separate. Callbacks run only after a successful save, outside the sidecar lock.

Commit retains the original identity/receipt/operation/project/file-removal
checks. Only when retirement's original conditions hold does the same save
append `exit_confirmed` and persist `retired=True`. The caller still owns actual
resource quiescence; begin, a producer terminal or audit phase cannot establish
that exit. These ports do not replace the original Runtime/Provider close chain.

If audit staging fails after resources/files were already disposed, commit
preserves the original audit bytes and atomically persists retirement plus an
optional private `audit_pending` extension inside the original deletion receipt.
This frozen extension contains the original deletion nonce, context and facts.
It is not authority and cannot widen the receipt. Unknown, malformed, foreign,
or inconsistent pending content is rejected, never reset. The legacy schema-1
record without this optional field remains accepted. Authority comparison may
exclude this one extension only after strict validation; all original authority
fields still compare exactly.

After that successful save, `DeletionAuditPending(receipt)` reports the committed
retirement and missing audit explicitly. The Runtime integrator must preserve
this exception/status rather than let the old generic `confirms_deletion` branch
turn it into ordinary success. A save failure remains an ordinary uncertain IO
failure; no durable outcome is guessed from a returned Python value.

`host.deletion_audit_pending(receipt)` provides a strict, live-identity and exact
retired-receipt reconciliation flag, including the write-then-exception case.
A failed read remains unknown. `False` means no stored pending observation; it
does not assert complete historical audit.

`host.supplement_deletion_audit(receipt, *, audit_result=None)` reads only the
stored original observation, appends it and removes pending in one original
save. It takes no replacement context/facts from wire or a newer request. A
recovered admission-only receipt may use this audit-only port without receiving
content or destructive cleanup authority. Repeated commits with pending use the
same repair path, ignoring any newer audit context. Legacy retired records with
no pending are no-ops and never receive reconstructed historical events.

A later lifecycle claim never rewrites a pending confirmation's generation.
Adoption reports pending until the original observation is repaired. Repair can
use the recovered exact receipt while retaining its earlier confirmed generation;
new cleanup attempts are recorded separately only after original repair. Both
repair save-before-failure and save-after-failure preserve idempotence.

Integration note: in the baseline `runtime/session_delete_authority.py:131-139`,
`commit_owner` catches all exceptions and suppresses them when
`confirms_deletion` is true. The Runtime owner must handle `DeletionAuditPending`
first; for an unknown ordinary save failure, a true deletion confirmation still
requires the strict pending query. Pending or unknown audit status must preserve
the original receipt and must not become a normal complete-audit response. That
Runtime/transport integration is intentionally outside this persistence slice.

Persistence-slice validation: **174 tests passed** in 33.60 seconds, including
real temporary sidecar/metadata/lifecycle files, original Runtime deletion and
AgentServer/Gateway receipt tests. New cases cover atomic event+mutation saves,
corrupt-audit admission blocking, persisted pending after actual retirement,
restart/claim generation preservation, save-before/save-after ambiguity,
original-context repair and unknown pending rejection. Provider exit remains a
synthetic fixture; this slice does not claim real Provider or UI acceptance.
Source: isolated branch based on swarm `7f29ca3a`, explicit swarm source overlay
with noneditable installed core `c7fa3781fa576492827f07f33dcb576c186be296`.
Evidence: `/tmp/r2b-delete-receipt-audit/`; Ruff, diff checks and existing
`pr-stable` plan validation passed. Full integrated stable and Runtime pending
response wiring remain required. No lifecycle timeout or whitelist changed.

## Runtime and existing deletion UI pending-audit delivery (2026-10-04)

The Runtime captures one trusted `SharingAuditContext` for the original delete
request and passes it to begin/adopt/commit. A direct host API with no request ID
keeps that field absent; it does not invent a wire request. `commit_owner` now
handles `DeletionAuditPending` explicitly. Other save failures require both
confirmed original retirement and a strict pending read. An unreadable status
cannot become `false` or successful delivery. A completed deletion retry calls
only the original receipt's supplement operation; original request/attempt and
confirmed generation remain unchanged and no Provider stop is repeated.

Organization RPC success retains `session_id`, `deleted: true` and
`exit_confirmed: true`, plus `audit_pending: boolean`. True means deletion really
completed but that deletion's audit observation still needs repair. False means
no pending observation on that receipt, not complete historical audit coverage.
`SharingHostService.deletion_audit_pending_for_permit` derives this field from
the exact original incoming permit, live full identity, receipt nonce, original
owner/source/lifecycle and completed deletion. Unknown facts reject delivery.
AgentServer and the Gateway do not trust a supplied backend/wire status flag.
The Gateway recomputes the status after queueing, before serialization; a valid
repair between enqueue and delivery can therefore change true to false without
dropping the original receipt. This port authorizes no content or subscriptions.

The current organization Single menu reuses its existing DeleteDialog. A
validated pending receipt removes only its original Session and enters a draft
only if that Session is still current. The same dialog stays mounted across
that navigation, shows an explicit already-deleted warning, and changes the
existing confirm button to retry audit saving for the same ID. A late A receipt
cannot change B's dialog or navigate B. Existing settings, side-pane and legacy
Session-list consumers also distinguish the validated pending receipt from a
failed deletion. Legacy exact-ID success without these fields remains compatible
and is not labeled as complete audit history.

Closing the dialog, refreshing or signing out loses the UI retry entry. The
original persisted receipt remains recoverable through authenticated same-ID
`session.delete`; this slice adds no pending-operation inventory or full activity
restoration. UI retries cannot repair a corrupt audit history themselves; the
underlying storage must become valid first. Original owner/identity constraints
still apply after restart. SDK `SessionDeleteResult` appends nullable
`audit_pending` (None is legacy unknown); the integrating Runtime owner must map
the organization service result into this field in `runtime/service.py`.

Validation uses swarm source overlay based on `6574677c` and noneditable core
`c7fa3781fa576492827f07f33dcb576c186be296`, with isolated configuration. Deletion,
receipt, cleanup and actual AgentServer/Gateway tests: 119 passed, followed by
7 focused audit tests including the extra incomplete-lifecycle retry (120 unique
delete/cleanup cases total). Existing writer/authorization tests: 74 passed.
React: 16 Single and 27 existing-consumer cases passed. Frontend build passed;
desktop and 375px actual DeleteDialog renderings were inspected using installed
Chrome and existing theme tokens. These are deterministic synthetic-exit and
component evidence, not real Provider/product UI acceptance or integrated stable.
Evidence and complete commands: `/tmp/r2b-delete-audit-runtime/README.md`.
