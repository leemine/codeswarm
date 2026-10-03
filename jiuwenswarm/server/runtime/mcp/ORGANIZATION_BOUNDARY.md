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
