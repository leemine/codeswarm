# Explicit Native stateless text MCP installation

`install_native_mcp_tools` accepts an already trusted project, exact
ExecutionBinding/NativeSession/React agent/core Session, one McpHttpBinding and an
immutable manifest. It does not read legacy configuration, discover tools,
resolve credentials or use the shared client pool. Each installation creates
unique agent-owned aliases and private clients. Closing invalidates every record
before removing only the exact owned card and resource slots. Partial installs
roll back their own slots; a foreign replacement is not removed.

The only supported manifest contract is `stateless-text-v1`: an object with the
single required string `text` and `additionalProperties: false`. It is an explicit
host declaration of a remote text operation with no other resource effects, not
a remote sandbox or a discovery claim. Its requirements are a specific existing
`tool/invoke` resource and the connection's existing `credential/use` resource.
No resources/grants are automatically created. Unknown schemas, executors,
subagents and additional arguments fail closed. Schema metadata is deeply frozen
and the installed mutable card is compared against a complete snapshot.

`native_mcp_resources` verifies the actual early/final Native executor proof.
`actual_mcp_resources` instead verifies the captured core ToolExecution because
that early/final proof expires before MCPTool's own parsing. Both bind the exact
project, execution Binding, NativeSession, actual React agent/core Session,
manager, card, concrete MCPTool, private client and installation lifetime.

The private client captures ToolExecution on its original task, snapshots the
post-parse arguments in a distinct BeforeToolContext and binds the original
Native slice's `mcp_authorizer` once. It passes the fixed source certificate and
actual operation to the host factory. SDK children may validate the captured
origin but cannot capture another certificate. Every HTTP request uses the
existing private `governed_http` consumer and must retain current tool plus
credential authority. Initialization, notification, tools/call and the SDK's
output-schema tools/list request are all part of this one fixed operation.
Results retain core's existing MCP content/error conversion; local transport
receipts and credential values are not appended to tool results.

## Host composition

Runtime supplies the fixed `NativeMcpCredentialAuthority` for the original
request, full identity, generation, Session and resource authorizer. NativeSession
captures it per request and injects the original input token (including MCP-only
bundles). The factory validates the exact original execution slice, host request,
installed executor and unchanged catalog at every actual SDK request. It uses the
same BoundToolResourceAuthority and BoundCredentialAuthority as other consumers.

The adapter installs metadata before Native start from this project's explicit
`mcp.servers` entries. Registration does not resolve credentials or contact the
server. Each execution owns its registration closure; successful direct stop and
startup rollback reclaim only its own slots. Failure to confirm Provider/round
exit retains them for retry. This requires the core owned-round drain correction,
not merely cancellation requested or an ended output stream.

## Existing configuration and resource grants

Only explicit `streamable-http` rows with these fields are supported:

```yaml
mcp:
  servers:
    - name: project-echo
      enabled: true
      transport: streamable-http
      url: http://127.0.0.1:8765/mcp
      headers:
        Authorization: Bearer HOST_STORED_SECRET
      organization:
        project_id: proj_example
        revision: v1
        credential_resource_id: echo-account
        credential_reference: mcp-account:project-echo
        credential_encoding: plain
        tools:
          - remote_name: echo
            description: Echo the supplied text
            input_schema:
              type: object
              properties:
                text: {type: string}
              required: [text]
              additionalProperties: false
            resource_id: echo-tool
            contract: stateless-text-v1
```

The header is read only after live resource checks, from the existing private host
configuration. `host_crypto` encoding uses the host's existing decoder. Neither
raw headers nor secret values enter the catalog hash, result or tool history.
Environment placeholders, login tokens, legacy state/credential stores and
implicit credential fallback are rejected. The example is a schema illustration,
not a configured service or permission grant.

A host administrator must already have registered and granted the exact existing
resources: `echo-tool`, kind tool, reference `mcp:project-echo:echo`, action invoke;
and `echo-account`, kind credential, reference `mcp-account:project-echo`, action
use. The endpoint is fixed by the credential binding. Changing catalog metadata
requires a new execution Binding; secret rotation at the same approved reference
remains subject to checks at each actual request. No frontend toggle or requested
MCP name bypasses these checks. Existing organization-mode legacy MCP registration
stays closed; no resource or grant is created automatically.

## Evidence and remaining acceptance

The source-combination regression uses real Runtime/Coordinator/sidecar,
NativeSession dispatch, mandatory Native tool checks, AbilityManager/MCPTool,
MCP SDK and HTTPX MockTransport: 54 affected cases passed with the core 5fe71211
round-drain candidate. Its nine host cases cover factory propagation, changed
catalog/Binding rejection, MCP-only dispatch, startup rollback, direct stop,
timeout retention and confirmed retry. DeepAgent/model/remote server parts are
explicit fixtures; the pending-round case binds the real core stop methods.

Core internal parse callbacks were isolated in formal b1921e73 (PR36); the core
round drain is a separate formal merge prerequisite. New tests are included in
both stable discovery and execution lists. Final locked installation, full batch
stable and ordinary real Native UI/MCP acceptance still need their exact paired
SHA evidence. Source tests alone do not close R1-13C or R2-B.

Stateful/SSE, OAuth/stdio, legacy pooling, arbitrary remote resource effects,
Team and delegated subagent MCP use remain outside this narrow contract. Existing
legacy behavior is unchanged. Only declared stateless text operations are mapped.
