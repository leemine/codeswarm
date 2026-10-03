# Trusted dual-user local Gateway probe

`_dual_user_gateway_probe.py` starts the actual AgentServer and Gateway modules,
logs in two independently provisioned users through HTTP, and opens two real
WebSocket clients. It does not call the business adapters directly. A transparent
relay preserves signed requests/handshake assertions and can hold a successful
AgentServer response until after revocation.

Run in the repository's installed test environment, with dependencies checked
against `uv.lock`. Supply a new absolute directory owned by the test:

```sh
PYTHONPATH=. timeout 220s .venv/bin/python tests/system_tests/_dual_user_gateway_probe.py --root /tmp/codeswarm-dual-user-unique-run
```

The probe needs local TCP listeners and permission to create its own subprocesses.
It creates a private, expiring organization auth configuration, isolated data and
configuration directories, dynamic loopback ports, and fixture histories. Raw
access tokens remain in memory; the private auth file stores digests and a signing
key and must not be published as evidence. Only `result.json`, service logs and the
probe log should be retained outside the private fixture directory. The script
uses an internal 180-second deadline, stops only its two owned processes, and has
a process-group kill fallback if graceful shutdown times out.

Verified boundaries:

- Separate HTTP login cookies and simultaneous WebSocket connections.
- Same project membership does not expose the other user's private inventory.
- A forged routing user does not grant access to the owner's history.
- Owner creates a persistent view grant; recipient reads its fixed history range.
- Appending new history does not expand the granted snapshot.
- A view grant does not authorize chat execution, switching or owner history APIs.
- Share revocation denies access through the original and reconnected WebSockets.
- A successful shared-history response held in the transport is discarded after
  revocation; a subsequent request on the live connection returns `FORBIDDEN`.
- HTTP logout revokes a credential while inventory is queued: the response is not
  delivered, the old socket closes with policy rejection, and reconnect fails.

The current Gateway silently drops a response whose delivery permit became stale.
The probe explicitly records this outcome and confirms the transport forwarded the
held response; it does not mistake a generic request timeout for proof of denial.
The missing explicit terminal response remains a user-experience limitation.

`/api/trajectory` is checked for organization-mode denial. File API availability
is configuration dependent: a missing route is recorded as **not mounted**, not
as successful download authorization validation.

History and ownership are fixture-provisioned before startup. This covers the
trusted authentication, transport and persistent sharing boundaries. It does not
claim real Provider execution, browser UI, Team behavior, full activity recovery,
remote deployment, credential resource isolation, or session-creation publication
acceptance. The result records the exact swarm commit and installed core Git source.

## Actual browser variant

`_dual_user_browser_probe.py` reuses the private service fixture and additionally
starts the actual web application, serving an existing production frontend build.
It requires Playwright and a local `google-chrome` executable. Record the build's
source commit/tree separately; the probe records the served index checksum.

```sh
PYTHONPATH=. timeout 290s .venv/bin/python tests/system_tests/_dual_user_browser_probe.py --root /tmp/codeswarm-dual-browser-unique-run --dist /absolute/path/to/frontend/dist
```

Two isolated browser contexts log in through the actual login form. Alice opens
her own Session, creates a persistent share in the owner dialog, and revokes it.
Bob uses the sidebar inbox and read-only viewer; refresh preserves the fixed
snapshot, and refresh after revocation removes cached content. A separate tab
using Bob's context attempts Alice's owner URL with forged routing metadata and
must show an unavailable page without private content. The owner Session switch
must also succeed, verifying identity survives the Gateway's background queue.
The fixture explicitly grants project execute ACL to both users, but grants no
tool/process resources and never submits a model request.

The browser variant records screenshots, visible page text, page errors, and
credential-free RPC summaries. It does not record cookies, login bodies, raw
frames, or a browser trace. Its internal deadline is 240 seconds; all three owned
service processes and the browser are closed on exit. Preserve the result, logs,
PNG screenshots and `browser-*.json` evidence, never the private auth file.
Unopened organization bootstrap RPCs may remain explicitly denied; this slice is
not a claim of complete application, Provider, Team or session-creation acceptance.
