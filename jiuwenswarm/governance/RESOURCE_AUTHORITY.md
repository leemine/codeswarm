# Execution resource authority

`governance.resources` is an instance extension capability implementing
`ResourceAuthorizer`. `ProjectAccessStore` is the disk-backed implementation. It
keeps `resource_access` under the existing project record in
`project_extensions.json`, using its existing file lock, atomic replacement and
fsync. It does not use the user-editable `extensions` object. Old records without
resource grants remain compatible with project ACL APIs but authorize no
protected execution resources.

A host constructs `ResourceDefinition(resource_id, kind, reference)` and calls
`register_resource(..., owner_subject_id=..., actions=..., expected_revision=...)`
after independently verifying ownership and availability. This is a **host-only
provisioning API**, never a route that accepts an asserted resource owner from an
ordinary caller. Reissuing the same definition/owner after revocation changes its
root grant version; existing delegated grants do not revive. Resource identity,
kind, reference and original owner cannot be replaced by re-registration.

| Kind | Operations | Reference and enforcement |
|---|---|---|
| workspace | read, write | Canonical absolute root; each operation supplies an absolute path, resolved and checked against the current subject scopes. |
| tool | invoke | Exact opaque tool reference; no wildcard implication. |
| credential | use | Opaque credential reference only. Secret values belong to the host resolver and are never accepted or persisted by this API. |
| process | execute | Explicit host process capability. It permits native process execution; it is not a directory sandbox or a Workspace path grant. |

Project ownership, project admin and project execute do not imply any of these
resources. A process grant does not imply a credential reference grant, although
an unrestricted local process still has its real OS user's ambient filesystem
and environment access. Hosts must select supported enforcement combinations and
sanitize/provision the process environment; this API cannot claim OS isolation.

## Delegation, revocation and visibility

`grant_resource(project_id, identity, resource_id, subject_id=..., actions=...,
expected_revision=..., scope=None, delegable=False, expires_at=None)` checks the
current authenticated actor's explicit delegable resource grant. Delegation
requires actor and execution subject to match; ordinary resource use checks both
subjects independently. Actions and Workspace scope cannot expand, expiration
cannot exceed the granting subject's expiration, and omitted scope/expiration
inherit their limits. Project execute is also required to create a delegation.

A grant records its parent subject and parent grant revision. Every decision
checks the full bounded parent chain (at most 16 grants), including expiration,
version, operation and scope containment. Cycles, missing parents, stale parents
and malformed grants deny access. Revocation, narrowing or regranting a parent
invalidates its descendants until explicitly regranted. The resource revision is
independent of `acl_revision`; compare both at admission and never cache an allow
across operations.

`revoke_resource` requires the recipient, the original grantor, or a current
project admin. Administrative ability to revoke does not permit granting.
Concurrent writes use mandatory expected resource revisions and the existing
sidecar lock. Project execute revocation separately blocks resource execution by
that actor, even while a resource grant remains recorded.

Ordinary project extension reads remove the resource authority entirely.
`resource_grants(project_id, identity)` returns only currently usable resource
reference metadata for the caller's actor and execution subject, never other
subjects' grants or credential references. Unknown, deleted, unmigrated or
corrupt projects/resources fail closed.

## Consumption at the execution boundary

`ResourceGuard(authorizer).check(project_id, identity, ResourceRequest(...))`
calls the authority every time and validates the returned project, actor,
execution subject, resource, action, path, revision types, reference and expiry.
Authority exceptions, malformed/nonboolean decisions and missing authorities
raise `ResourceAccessDenied`. Custom policies use the same contract; they must
also enforce project execute and return fresh project/resource revisions.

Call this after any scheduling/approval wait, immediately before the operation.
The check grants no resource lifetime and allocates no lease. Existing Runtime,
Binding, Session and Provider owners still own acquisition, stopping, exit
confirmation and release. A credential resolver must resolve only the explicitly
authorized reference for the execution subject, fail if it is unavailable, and
never infer inheritance from another subject's Binding or private Session.

`ProjectAccessStore.guard_resource` holds the existing authorization file lock
through one short synchronous operation. It must not span an async process or a
long download. Path resolution rejects traversal and existing symlink escapes,
but filesystem mutation can still race with a later open; host file adapters need
safe descriptor-relative/no-follow primitives for that boundary. Native shell
text is never parsed to claim path confinement. Provider admission, operating
system isolation and in-flight cancellation require their actual execution
adapters and remain separate from this authorization backend.

## Credential consumer boundary (implementation in progress)

`BoundCredentialAuthority` is an internal host adapter over the same
`ResourceGuard`. A host freezes exact `CredentialUse` references, purposes and
sink destinations for one private execution. The resolver runs only after the
current subject's explicit `credential/use` decision; a revision, identity or
execution change while resolving discards the result. Consumer exceptions omit
resolver diagnostics, which may contain secret values. The adapter does not
persist credentials or create another grant store.

Each actual transport request, including retry and redirect, must call
`resolve_for_request` with its real, exact destination before sending. Returned
values must not be cached in a model configuration or reused for later calls.
A successful adapter unit test is not evidence that existing Model, MCP or
Provider consumers have adopted it. Those production integrations and their
real transport evidence remain required; unsupported consumers must not be
represented as credential-isolated.

The initial Native model consumer selects only explicit `models.defaults` or
`models.default` entries from raw host configuration. Each governed entry must
declare `credential_encoding` as `plain` or `host_crypto`; encrypted values
require this Runtime's crypto extension and fail closed on decoding errors.
Environment interpolation, OAuth placeholder fallback and ambiguous entries are
rejected. `credential_reference` may name an explicit host-managed opaque
account reference; otherwise a deterministic reference derives from public
model/endpoint metadata. The host registers that exact reference and grants
`credential/use` independently of project execution. Two accounts using the
same model and endpoint require distinct explicit references.

Runtime captures the model authority in the original Native HostRequest;
consumption also checks the exact currently owned Native execution and Binding.
A stale request, changed adapter instance, revoked credential or changed
authority cannot deliver its resolved credential. The model transport must pin
this authority at the beginning of each logical call and recheck it for each
HTTP send/retry. Actual transport/factory integration is still in progress.
