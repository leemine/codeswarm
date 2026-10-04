# Organization-mode MCP boundary

The legacy MCP path uses installation-wide configuration, a credential store
keyed by MCP name, and process-level connection/environment state. It is not a
subject-authorized credential consumer.

When the host configures organization authentication, this path is unavailable:

- Shared `CredentialStore` reads, placeholder resolution and CLI credential
  environment construction reject with `PermissionError`.
- Server config assembly, explicit Session MCP selection, named registration,
  marketplace/CLI connect and OAuth continuation reject before consumption.
- Direct HTTP preflight, AgentServer's temporary precheck and live probes return
  an explicit authorization-required failure. Registration checks again after
  asynchronous preflight so a caught preflight exception cannot permit a later
  connection. Direct CLI runners and OAuth process startup also reject.
- Background prewarm, global adapter MCP startup/reload and global token
  environment synchronization are skipped. This keeps the primary Web service
  and read-only sharing available without reading or consuming MCP credentials.
- Connector names and token-schema metadata are not credential values; their
  metadata helpers remain available. Single-user behavior is unchanged when
  organization authentication is not configured.

There is no caller-provided bypass flag. A valid organization login, a tool name,
`ready` inventory status, an injected placeholder callback or a saved credential
reference does not establish `credential/use` authorization.

This is a fail-closed prerequisite, **not completed organization MCP execution**.
A future trusted host consumer must bind full identity, private execution and
current credential grants to resolution, connect/reconnect and invocation;
separate connection state; and release owned resources on exit. It must not
re-enable this legacy fallback or inject shared tokens into `os.environ`.

This change does not close already running processes when an operator changes
process environment configuration in place, nor does it claim OS-user isolation.
Mode changes require a service lifecycle transition; current protected tool
execution still requires its own live authorization. Real authorized MCP and
Provider acceptance remains separate from the denial regressions.

## Private stateless HTTP consumer slice

`governed_http.invoke_mcp_tool` is an explicit host consumer, not a registration
bypass. It receives a mandatory original `BoundCredentialAuthority`, one exact
host HTTP binding, and host tool/current-operation predicates. It supports one
stateless Streamable HTTP JSON operation per private SDK session/client. Every
actual POST (including SDK initialization, notification and schema discovery)
resolves its credential separately; request snapshots and immutable resource
decisions are rechecked before handoff and after bounded response reads. A
secret-free local receipt reports only request count and successful local close.

The supplied predicates must retain the original actor/subject, private Binding,
generation, catalog revision and actual executor proof, including across SDK
child tasks. They must not select the latest Turn or treat credential permission
as tool permission. `BoundCredentialAuthority.check_for_request` provides the
same live decision without resolving a secret; it is not a separate ACL.

The consumer rejects redirect, stateful session IDs, SSE response streams,
unexpected methods/arguments, and stale authority. It does not use the legacy
shared pool, environment/proxy defaults, OAuth or automatic tool-call replay.
Each operation owns its pool and closes it on success, failure and cancellation;
a failed close produces no success receipt. Requests already accepted remotely
cannot be undone by local revocation. This is not an OS/network sandbox.

No production registration route or Native MCP resource mapper is enabled by
this module. In particular, MCPTool's internal parse/schema callbacks run after
the generic Tool authority point: its exact private client/executor mapping and
actual post-parse arguments still need a separately verified host integration.
The existing organization-mode legacy denies remain in effect. Stateful/SSE,
OAuth/stdio, persistent pooling, actual Native tools and real Provider/channel
acceptance remain separate unfinished work. Tests use the actual installed MCP
SDK with HTTPX MockTransport and synthetic credentials; they are not live MCP
service acceptance.
