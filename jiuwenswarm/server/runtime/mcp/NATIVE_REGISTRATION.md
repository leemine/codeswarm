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

## Host integration and remaining acceptance

The host must supply a real McpOperationAuthority with original Runtime request
lifetime, identity, catalog, Binding and resource checks, including across async
credential resolution. `is_current_host_request` is injected by NativeSession's
wrapper, not chosen by this private client. Bundle propagation, Runtime factory,
actual registration/cleanup ownership and composition into the Native mapper
are separate integration work. This module does not re-enable organization-mode
legacy MCP registration or authorize unknown products/Team/subagent delegation.

The initial implementation was tested using real AbilityManager, MCPTool parse
callbacks, ProjectAccessStore/ResourceGuard, BoundCredentialAuthority, installed
MCP SDK and HTTPX MockTransport. The transport and NativeSession owner fixture are
synthetic; this is not real service/Provider/UI acceptance or a complete locked
swarm candidate validation.

A diagnosed upstream prerequisite remains at core f6c56838: MCPTool's internal
TOOL_PARSE_STARTED/FINISHED callbacks run inside the original method and can see
its ToolExecution on the same task. External/direct, child-task and expired
certificate calls already deny, but that inner callback visibility must be
masked in core before this package is considered production-complete. The fix
must preserve parsing/transformation and restore visibility only for the actual
private client call. A one-use client flag would not substitute for that proof.
Stateful/SSE, OAuth/stdio, legacy pooling and arbitrary remote resource effects
remain unsupported by this narrow contract; existing legacy behavior is unchanged.
