# Owner Workspace download foundation

2026-10-04 · executor: Codex authentication subagent. Scope: immutable host permit,
bounded safe file reader, original artifact token provenance and exact Native
send_file method consumption. Base: swarm 9dacf66a. Integration, public Runtime
ports, HTTP/RPC/UI and final delivery sinks remain the main agent's work.

## Authority and interfaces

`governance.workspace_download.WorkspaceArtifactIssuer.capture(host,
identity_resolver, session_id, *, channel_id, workspace, source_check,
actual_paths)` requires a trusted factory holding the original execution. It
never obtains identity from wire parameters or ambient/default-owner fallback.
The callback `source_check()` must return literal True and prove the actual
ToolExecution, original Native slice/Session/Runtime admission and final arguments.
`issue(path, session_id)` rechecks that origin and the same allowed path.

`workspace_artifact_origin.validate_send_file_origin(**factory_arguments)` checks
an actual core ToolExecution and the original LocalFunction bound to exactly
`SendFileToolkit.send_file`, final parsed path/target arguments, original slice,
subject and current toolkit routing. It returns a fixed checker. This is only
method evidence; Runtime/NativeSession must supply their original owner/admission
checks as well. No other toolkit method, subagent or cross-channel target is
admitted. Empty target_channels is explicitly pinned to the original channel.

The original HMAC token gains `workspace_artifact_v1`, with strict schema 1,
original full identity, Session owner record revision/source epoch/lifecycle
generations, canonical existing Binding fields, root device/inode and file
device/inode/size/mtime_ns/ctime_ns. These are signing facts, never grants. No new
store/ACL is created. The token is at most 4096 UTF-8 bytes and has at most the
existing 600 second TTL. Legacy single-user ordinary token behavior is unchanged.
Organization direct signing without the explicit issuer fails closed.

`WorkspaceDownloadPermit.capture(host, identity_resolver, session_id, token,
token_validator=original_manager.validate_token)` captures current normal owner
and exact Project/resource revisions independently of those signing facts.
Caller callbacks and validator are private host ports, never request objects.
`size`, `name`, `session_id` expose only the authorized result's bounded metadata.
`check()` succeeds with None and otherwise raises generic PermissionError subclass
WorkspaceDownloadDenied. Call it after every await and at final header/body sink.
`read(offset, limit)` returns at most 65536 bytes after both authority and file
identity checks; it does not imply those bytes may be delivered later unchecked.
A renewed resource grant does not revive an old permit. A fresh request can obtain
a new permit only if current authority and original artifact source remain valid.

## File safety and locks

Only current owner ordinary Single private Sessions under the original persisted
Workspace are eligible. Owner access uses normal SharingHost `_current`, including
B3 continuation provenance, never cleanup-only authority. The existing Binding
field validator is reused only as schema validation. Project execute plus explicit
workspace/read for actor and subject is required, unchanged from ResourceGuard.

The reader walks from `/` using directory descriptors and O_DIRECTORY/O_NOFOLLOW,
then opens a nonblocking, no-follow regular file. Root/file identity is fixed.
Reads are bounded synchronous pread under the original reentrant ProjectAccessStore
sidecar lock, with checks before/after; no history locks, await, new queues or long
stream lock. Descriptors are closed on every path. Platforms without required
POSIX primitives are rejected explicitly. Replacement inode, directory/symlink
escape, FIFO, size or timestamp changes deny. Consumers must not substitute a
new path/file after capture.

## Limits and integration requirements

All existing organization HTTP 403 gates remain. No new enum, URL credential or
public protocol was added. Sharing fixed text grants no attachments. Sealed assets
in /tmp, skills, arbitrary path-only fallback, trajectory and Git exports stay
closed. Organization require_execution_authorization=True currently selects the
existing sealed-asset path, whose Workspace provenance is not yet available; this
combination rejects before staging rather than bypassing Smart Approval. Do not
call that combination successfully supported.

The main integration must carry optional `artifact_issuer_factory` on the original
ExecutionResourceAuthorities/NativeExecutionSlice and register it from the exact
Runtime/NativeSession request. This slice does not add those public host fields.
Without the factory, organization send_file fails closed. Long-lived supervisor
ambient identity is not a substitute. Ordinary tool delivery catches failures
with its existing error result, but no path-only artifact card is published.

HTTP consumers must authenticate the original principal, preserve the same permit
across offload/queue/backpressure, and invoke check immediately at ASGI send and
any static proxy wfile.write. Shared credentials, copied token, a successful read,
or an earlier upstream check never authorize a later buffered chunk. HTTP/UI and
real Provider/download validation remain required before closing B4.

## Validation

Tests use real temporary organization credential configuration, current principals,
ProjectAccessStore and Session owner sidecar plus real file descriptors; two owners
share one Project but cannot exchange tokens. Actual core LocalFunction invocation
creates the ToolExecution certificate; its host slice/factory in that test is a
synthetic port, not a complete Runtime or Provider acceptance claim. Coverage
includes read barriers, current credential/owner/resource/source changes, regrant,
strict signed schema, file replacement, platform requirements and original method/
args/target/slice mismatches. Affected regression: 147 passed (including 48 new tests), no skips; Ruff and
`git diff --check` pass. Commands and exact source provenance are in
`/tmp/r2b-owner-download-implementation/README.md`.

Local source tests use `/tmp/r2b-core-3a3b575f-locked-venv/bin/python` and this
worktree's source. They are not a fresh swarm non-editable lock installation or CI.
No new real adversarial Provider probe, HTTP service or user credential was used.
