# Heartbeat preparation across a permanent-delete retry

A failure after Session directory removal can leave the original permanent-delete
operation pending. The existing Provisioner intentionally does not abort that
operation's Heartbeat fence after destructive work starts. Previously its next
`begin_session_delete` call rejected the remaining in-memory marker indefinitely.

Heartbeat now keeps an opaque preparation owner in the existing
`_deleting_sessions` mapping. It reads the operation ID from the original host
lifecycle file, never from request parameters. Repeating `begin_session_delete`
can reuse this preparation only when the same pending delete operation is still
current, preparation finished, its Scheduler/Admission/Execution objects are
identical, scheduling remains suspended, admission remains blocked, and no active
Heartbeat or pinned agent remains. A boolean alone is insufficient.

Preparation in progress, a different operation, a completed operation, and a
legacy invocation without a durable operation do not authorize this reuse.
Original legacy single-call behavior remains. No new persistent store, operation
journal, scheduler, or public method parameter was added. The original Runtime
and lifecycle locks remain responsible for serializing the delete transaction.

Commit and abort make the original preparation unavailable for reuse before
awaiting cleanup. Cleanup removes a marker only when it is still the same object;
a late result cannot erase a replacement marker. Existing failure propagation and
job policy behavior remain. Retrying the destructive transaction does not abort
and reopen Heartbeats as a workaround.

## Verification

`tests/unit_tests/agentserver/test_heartbeat_delete_retry.py` uses the actual
Heartbeat runtime and original Runtime/Provisioner fixture with temporary
lifecycle files. Its original regression fails before this fix: a synthetic
trajectory commit failure after directory removal leaves the next delete stuck.
The fixed transaction completes on retry without repeating preparation or
releasing the fence between attempts.

Additional cases cover concurrent preparation, different/absent operation IDs,
actual dependency/quiescence checks, preparation failure/cancellation, cleanup
reentry, stale cleanup handle identity, and commit error propagation. Together
with existing Heartbeat runtime/Session and Runtime Provisioner tests, 156 tests
passed against the installed core `26255` locked environment. These are local
swarm source tests with a non-editable installed core, not fresh candidate CI or
real Provider acceptance. No real Provider, model service, or user credentials
were used. Full B4 deletion and Gateway acknowledgement remain integration gates.
