# Visible shared-history freshness

The read-only shared-history dialog reuses session.share.list to obtain the exact unique active view grant for its session_id/share_id pair. Only the response revision and finite unexpired expires_at (or explicit null) are used, for display freshness. This DTO is never authority to read history: initial reads, paging and periodic refresh still call session.share.history.get with the existing private cursor and server-side authorization.

While the dialog is visible and the connection is ready, a single timer rechecks every15seconds, or sooner at its returned expiry. At that point old text/cursor is cleared before revalidation. Failed list/history requests keep content clear and expose only the existing fixed generic message; there is no owner-history/download fallback. A changed grant revision discards the old cursor and starts from the first newly authorized page. An expiry that passes while history is in flight prevents that page being displayed, even if the browser timer has not run yet.

Close, target change, unmount, hidden/pagehide, identity change and disconnect cancel pending requests and timers and invalidate local request generations. Late responses cannot repopulate the dialog. Visible/ready reconnection may recheck only the same unchanged identity context; an identity-change signal requires an explicit refresh or reopening. Refresh does not bypass current backend authentication. Transport cancellation only cancels the local pending delivery; it does not introduce a backend operation or cancel protocol.

## Limits

There is no revocation push. An active visible dialog has a nominal15second detection window, subject to normal browser scheduling. Local expiry scheduling also depends on the browser clock; authoritative backend checks still run for every data request. A parent/source restriction that is not exposed in the grant's expires_at is caught by those rechecks, not an invented local deadline. Hidden dialogs clear immediately on their existing visibility lifecycle. Information already seen/copied by a user cannot be recalled. This is UI stale-content withdrawal, not instantaneous or retroactive revocation.

Existing wire DTOs and server authorization are unchanged. Legacy history API callers remain compatible; the optional AbortSignal is local transport control. The organization-only shared dialog does not become a legacy Session history view.

## Validation

`npm run test:shared-history`:25passing React/jsdom and API behavior tests (14existing,11new), including exact/duplicate/unavailable view grant rejection, expiry during late paging, delayed timer versus expired history, grant revision change, nominal refresh withdrawal, cancellation after close/unmount/auth/hidden/disconnect, and visible-only requests. `npm run test:session-continuation`:31passing existing sharing/continuation cases. Timer tests use a controlled local timer fixture; they do not claim real push/Provider acceptance. Frontend build and diff-check accompany the source delivery. No real browser, Provider, remote network or new dependency was used for this UI package.

## New-draft model display is not resource authority

The prior ordinary delete browser story on a1f279bf showed `/chat/new` with `alice fixture model` after deleting Bob's private continuation. App.enterNewConversation uses the existing defaultModelName via resolveNewConversationEntrySettings and clears the previous Session; it creates only frontend draft state. sessionStore.getEffectiveModelName returns the explicit key or existing display selection, not a credential or grant. No switch/create/chat request was sent by that delete navigation.

Actual Native consumption is separate: NativeModelCredentialAuthority checks the exact HTTP target/model/API implementation, original identity and current execution ownership, then requires one current credential/use grant whose reference matches the binding; BoundCredentialAuthority resolves and revalidates it for the request. The B3 continuation additionally checks its approved model binding/fingerprint. A model label or catalog/default selection therefore does not prove that Bob can invoke Alice's model. The successful prior HTTP call was Bob's model/credential; no new-draft Alice invocation was attempted. This package does not alter or claim per-actor model catalog visibility/default policy, and introduces no new configuration system.
