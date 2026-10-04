# Original-owner Single deletion

Organization Single deletion now captures the original authenticated request, full identity, persisted binding and Runtime generation before any wait. It uses the existing project/session locks, SessionArchiveService lifecycle and RuntimeSessionProvisioner transaction. The owner sidecar stores the original deletion receipt; no second transaction store or Provider queue is added.

Source-share revocation does not prevent the original owner from stopping/deleting existing work. Cleanup authority grants no read, execute or credential use. Request transport channels do not select cached Provider resources: the original persisted channel does. Existing subagent resources, exact Provider/tasks and Coordinator producers must stop before resource disposal. Failure or uncertain exit retains fences and original ownership. Each asynchronous phase rechecks the original identity, binding, receipt and generation; a newer execution cannot be selected.

Deletion retires the owner only after resource/filesystem and observer commits. Metadata-less retry keeps the original project, binding, lifecycle operation and source epoch. Already-retired receipts allow only completion reconciliation. A service generation change may require a fresh request after committing the original receipt before the strict transport acknowledgment can be delivered; this deliberately does not weaken the exact original permit check.

Normal Session response permissions expire after retirement. The only replacement sink sends the fixed session_id/deleted/exit_confirmed result after the actual host confirms the original permit against the durable completed receipt. AgentServer checks under its send lock; Gateway checks at enqueue and again at the socket writer. It grants no general response/history/event exemption. Unknown exit, changed identity, recreated Session or stale receipt cannot produce a successful deleted event.

Heartbeat preparation reuses only the same durable delete operation with the same ready, actually quiescent scheduler/admission/execution handle. A failure after directory deletion cannot permanently strand an otherwise valid original retry. Preparing, changed or unconfirmed handles remain rejected; late cleanup cannot remove a newer handle.

## Validation and limits

Installed formal core26255e7b is used; swarm tests use this source worktree. The real sidecar/lifecycle/Runtime/Provisioner/Archive and actual AgentServer wire codec/Gateway queued writer are exercised, with synthetic Provider exit and transport. The combined original-owner/real Heartbeat/destructive-retry/delivery group passed36 tests; prior receipt/phase group45 passed. Legacy archive/provisioner regression previously48 passed with1 existing skip; final integrated affected regression and stable must run again. New tests are appended to both discovery and execution in stable, without removing old cases.

No full authentication handshake, real Provider exit, UI deletion, Team deletion, complete active recovery or full B4 revoke/audit story is claimed by these in-process tests. Those required product exits remain open until separately evidenced. Existing legacy single-user lifecycle paths retain their original behavior.

## Visible organization Single delete action

The ordinary Single sidebar menu exposes the existing DeleteDialog when the organization sharing surface is present. This is visibility only; the backend original-owner permit remains authoritative. Team and cron menus retain their existing behavior. The new action passes `requireExitConfirmation: true` to the shared deletion client and accepts only the exact requested Session with both `deleted: true` and `exit_confirmed: true`. Missing, false, mismatched and unknown results keep a retryable error. NOT_FOUND also refreshes inventory; it is not successful deletion.

Confirmed deletion removes only that Session's local state. App enters its existing unexecuted new-conversation flow only if the deleted ID is still current, explicitly clearing the previous-Session inheritance. A late A receipt cannot navigate the currently selected B or close B's deletion dialog. Both desktop and floating sidebar instances use this callback. No Session switch/create/chat request is introduced by this callback.

The client default remains legacy compatible with the original exact `{session_id}` deletion response. Existing legacy, Team and cron consumers are **not** claimed to enforce the new exit-confirmation requirement. This UI slice does not broaden their backend authority.

Validation: `npm run test:single-session-delete` exercises the real Sidebar/dialog/client and App's shared navigation hook through React/jsdom (12 passed); `test:session-delete` retains 24 passing existing deletion cases; `test:sidebar-model` retains 5 passing menu cases. SVG icons are test stubs, not visual validation. `npm run build` and `git diff --check` are required for delivery. Real browser/socket/Provider deletion remains a separate pending normal-path probe; no B4 completion is claimed by these tests.
