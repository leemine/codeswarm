# Original continuation authority loss and confirmed exit

A sharing revoke/update used to change the durable grant while leaving an
already-running private continuation waiting. Future resource consumption was
denied, but that did not confirm its existing execution/resources had exited.

Runtime now attaches one stop-only capability and monitor to the original
Coordinator Session record when it admits a continuation. It pins the original
owner, generation and durable Binding. The admitted preparation fixes the actual
cached agent slot before invocation; cleanup never discovers a replacement by a
routing hint. Sharing revoke/update waits for the original Coordinator close.
Cross-process authority changes and expiry are rechecked on a one-second nominal
interval while that original record lives. This is not a hard real-time bound.
No Provider event consumer, new work queue, persistent grant store or new Session
state machine is added.

Authority loss closes the existing generation and fences admission before
cancelling producers. The existing strict subagent/Provider stop ports must
confirm exit before manager/binding cleanup. Failure keeps QUIESCING and the
original close available for retry. Regranting permission does not abandon a
previously requested exit or reopen execution. Runtime shutdown owns and joins
its record's monitor; a newer generation cannot inherit an older watch.

A retained private cleanup capability may stop a previously admitted owner even
when the actor is disabled; live incoming cleanup calls still require the known
actor check. It grants no read, execution, credential, deletion, publication or
rebinding authority. The original owner/stamp/metadata remain mandatory. A
changed cached slot is rejected, never selected as a replacement to stop.

## Evidence and limits

With installed core f6c56838 and explicit swarm source overlay, 175 affected tests
passed (`/tmp/r2b-active-revocation-final.log`). They include real sidecar/sharing
RPC/Coordinator tasks, expiry without RPC, failed exit/retry, regrant during
QUIESCING, newer-generation protection, disabled actor cleanup and three actual
Runtime/AgentManager composition regressions. Model/Provider stop is synthetic;
these results are not actual Provider/UI or final installed pairing acceptance.

The first review exposed two bugs now covered: regrant incorrectly stopped
retrying an unconfirmed exit, and successful AgentManager stop makes its
has-session-runtime lookup empty (not a cache replacement). Cleanup now retains
the original cache slot rather than rediscovering ownership after stop.

This slice covers the persistent source/project/resource authority of an ordinary
Single Native continuation. It does not turn one connection token's expiry into
authority to stop another connection's execution. Per-connection active-operation
withdrawal, Team ownership and mandatory real revoke/expiry acceptance remain
separate open exits. Provider effects accepted before revocation are not rolled
back; loss of authority denies further consumption and initiates confirmed exit.
Share mutations may already be durable when the RPC reports EXIT_UNCONFIRMED;
refresh observes that mutation while the original Runtime cleanup continues.
