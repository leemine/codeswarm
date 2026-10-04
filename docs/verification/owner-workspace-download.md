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

The foundation originally kept every organization HTTP gate closed. The integration
below now opens only its exact owner download consumer; all other gates remain. Sharing fixed text grants no attachments. Sealed assets
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


## Runtime and local delivery integration candidate

Main Codex integrates the original Runtime resource scope into Native's existing
HostRequest and per-invocation slice. The real ToolExecution/LocalFunction proof
reaches the existing send_file envelope before its first await. The issuer keeps
original Runtime execution IDs and request/channel IDs, complete identity,
Native Session/Turn/request entry, original tool invoke decision, owner revision
and each original Workspace resource decision. Ending an old admission, replacing
its request or revoking/regranting a resource cannot lend that old issuer new
authority; a new valid producing operation can capture a fresh issuer.

One explicit consumer `file.download_workspace_chunk` is added to the existing
ReqMethod and WorkspaceFileAdapter. Wire parameters are exactly token, integer
offset and integer limit (1–65536); the original Session comes from the envelope.
SessionRequestPermit captures the full owner Workspace permit before dispatch.
ProjectBoundary accepts only that exact local permit, whose existing ResourceGuard
requires Project execute and Workspace/read. AgentServer injects the original
SharingHost and identity resolver on both construction and rebuild. Each offloaded
read is checked after return and by the original final delivery guard. No generic
file permission, local fallback or URL-derived principal is accepted.

Only `/file-api/download` GET/HEAD is opened for organization mode, including
single Range and bounded inline safe media. Explicit session_id and existing HMAC
token are required; extra/duplicate selectors are rejected. The original Gateway
uses independent current credentials and obtains every bounded chunk from the
AgentServer. A fixed permit is rechecked after each await, and the existing actual
ASGI send guard checks headers and each body. The static app_web proxy goes only
to its configured Gateway, rechecks the same local source before headers and each
bounded read/write, and never falls back to local path or token routing. Partial
streams end with the advertised Content-Length unmet, so clients cannot report
complete success. Already delivered bytes cannot be withdrawn. This requires the
current same-host metadata/secret/filesystem deployment and makes no remote claim.

Source overlay against formally installed core3a3b: 533 affected governance,
Native and organization boundary tests passed (`/tmp/r2b-artifact-affected-final.log`);
108 old file proxy, verified download, Smart Approval and WebSocket tests passed
(`/tmp/r2b-artifact-legacy.log`). Independent actual Runtime→Native→ToolExecution
review found tool-revoke, mutable request rebinding and grant-revival gaps; retained
red cases and 65 passing component/download tests plus 143 compatibility tests are
in `/tmp/r2b-artifact-authority-review/README.md`. The 14 delivery tests use real
permits/readers/Adapter/HTTP middleware with synthetic transport, not real browser
or Provider acceptance. One initial ASGI test expected a normal return after stream
abort; it now models a deployed transport without propagating server exceptions
and still asserts zero buffered bytes and no later read. No production guard was
relaxed. Two old Runtime mocks lacked actual execution_id; those fixture fields
were added, and the exact newly scoped download error code was updated while all
old forbidden route assertions remain.

UI, exact integrated source installation/stable and real normal owner download
validation remain pending. Smart Approval sealed assets still lack original
Workspace provenance and are not opened by this candidate. No scope here closes
R1-13C/R2-B4, Team, Codex native mandatory authorization or complete activity restore.


Independent delivery review retained two further red cases and fixes. Static
BaseHTTPRequestHandler success headers are explicitly discarded when permission
fails before flush; after flush begins, errors only close the connection. The
Gateway pins exact local WebSocket client/URI/peer/socket and rechecks after its
send-lock wait, before signing and sending. A private per-call guard captures the
original request ID, actively expires at scope exit (including copied contexts),
and is never serialized. The static proxy also verifies its configured Gateway
and connected peer are loopback. Remote/unknown clients are refused before any
chunk request. Existing fallback remains denied. The real E2A/signature/codec/
Adapter composition uses only an in-memory socket bridge and preserves failures
in `/tmp/r2b-workspace-download-review/README.md`. Initial 29 delivery/composition
tests and 52 old client/E2A/auth tests passed; final scoped-lifetime tests and full
integrated stable are still recorded separately. Browser UI and actual local
transport downloads must be validated at the final candidate before completion.

Final independent E2A review: 18 passed in 6.23s, including original eight
composition cases plus scope normal/error/cancellation exit, copied contexts,
waiting child replay, timeout child within live scope, fixed original request ID
and actual final wire ID replacement. Evidence:
`/tmp/r2b-workspace-download-review/e2a-scope-final.log`. No real socket/Provider
was used for these counterexamples.

## Producer URL integration correction (2026-10-04)

The normal UI run at swarm `c3a972559e89c07ba64986007b8e826c538f2a26`
produced `chat.file`, but its download button stayed disabled: the actual
`build_file_download_info` producer still appended legacy `user_id`, while the
organization UI/HTTP contract accepts only `token`, original `session_id` and
optional `inline`. The UI boundary was correct and remains unchanged.

The explicit `artifact_issuer` branch now emits the signed token and its original
Session selector. Token generation still validates the real issuer before URL
construction. The issuer-free legacy branch retains the original URL helper,
including optional routing `user_id` and legacy token lifetime behavior. No new
query fields, issuer API or authorization fallback is added.

New actual metadata-builder tests use a real WorkspaceArtifactIssuer, real owner
sidecar/resource grants, signing manager and download permit. Both empty and
nonempty legacy routing users reproduce the original mismatch, then pass with
exact `token + session_id`; the resulting permit reads the fixture bytes. Two
legacy builder tests preserve their existing URL and token schemas.

Evidence: `/tmp/r2b-owner-artifact-url/` (`red-no-cov.log`, `affected-green.log`,
`send-consumer-unprivileged.log`, `source.json`). With formally installed core
`0c8e14d76cfb18e27592dde991b45b07818960b1` and candidate swarm source:

- Workspace download, delivery, actual artifact-authority review and existing web
  file-download modules: **125 passed** (one pre-existing Authlib warning).
- Existing send-file deduplication and execution-consumer modules: **21 passed**.
- `git diff --check`: passed. No dependency/lock changes.

Commands use `python -m pytest --no-cov -q` and the named modules, with private
`JIUWENSWARM_WORKSPACE/DATA_DIR/CONFIG_DIR/HOME` and `CONFIG_URL=off`. Initial
collection without the private template config failed and was corrected without
changing source configuration. Default full-repository coverage reporting made
the initial two red tests take 112 seconds; final affected tests disable coverage
report generation, not test assertions. The sandboxed send-consumer run failed
one durable-history fixture and stalled on another; the same unmodified 21 tests
passed outside the sandbox with task-private config. All attempts are retained.

This correction does not claim a new real UI pass. The integration owner must
rerun the same ordinary authenticated download story and final stable gate on
the integrated commit. No adversarial or external Provider probe was run here.

## Signed AgentServer admission correction (2026-10-04)

The ordinary UI run at swarm `380befb1d19f39f76a89cc020646411ed466832e`
with formal core `2d3db9c314af2bb426537c34290b986341d16698` reached the enabled
Artifacts download button after a real Native file publication, but no browser
download completed. The signed `file.download_workspace_chunk` request was
rejected before adapter dispatch: its strict three-field params contain only
`token`, `offset`, and `limit`, while the original Session selector lives in the
E2A envelope. Generic admission previously forwarded that selector only when
params also contained `session_id`.

Only this exact method now forwards the original envelope selector to the
existing Session boundary. The params schema, signature verification, owner and
artifact authority, final delivery guard, and all other method handling are
unchanged. Earlier E2A composition tests reconstructed admission inside their
socket fixture and thus did not cover this actual AgentServer entry point.

Six new tests enter `_handle_message` with real signed E2A, codec, admission,
owner/resource sidecars, issuer and WorkspaceFileAdapter. The legitimate request
returns fixture bytes; wrong/missing Session, an extra param, unsigned and
modified signed messages are rejected without reaching the adapter. Before the
fix only the legitimate case failed; after it, all six pass. The affected six
modules total **163 passed** with formally installed core above and candidate
swarm source, using `pytest --no-cov -q` (no core source overlay).

Evidence: `/tmp/r2b-owner-download-admission/{red.log,affected.log}`. The initial
sandboxed green run stalled on the actual threaded file read and hit its bounded
timeout; the unchanged test suite passed outside that sandbox in 19.25 seconds.
The real UI failure and successful owned-process cleanup remain at
`/tmp/r2b-owner-download-ui-run-380befb1/result.json`. This patch still requires
integration stable and the ordinary UI story on the new candidate; no new real
Provider, delayed-operation or revocation probe was run for this fix.

## Organization approved sealed snapshots (2026-10-04, candidate)

The organization `require_execution_authorization=True` path now preserves the
existing approval contract: approval names an exact source path, and delivery
captures its then-current bytes. It does not silently replace this with an
approval-time content digest. The original Native ToolExecution certificate,
current owning Runtime/Session, `native:send_file_to_user` tool/invoke decision,
and exact Workspace/read decision are captured before staging. No wire field,
legacy user ID, or arbitrary `/tmp` path can manufacture this source authority.

`WorkspaceArtifactIssuer.stage_sealed(owner, original_path, *, file_name,
expires_at)` opens the authorized source through the existing no-symlink
Workspace traversal and gives that same FD to the existing asset owner. Source
identity and authority are checked while copying and before publication. The
original sidecar stores the frozen source identity, owner revision, exact
resource decisions, non-sensitive ToolExecution fingerprint, source/root/file
stamps, sealed root/file stamps, name, expiry, size and digest. Only `state` is
excluded from the registration fingerprint. No credentials are persisted.
`VerifiedWorkspaceRegistration` returns an immutable snapshot, not the mutable
sidecar dictionary.

The existing signed `verified_asset_v1` token contains a
`workspace_artifact_v1` schema-2 registration digest. Existing token, RPC and
HTTP routes remain the sole delivery path. `WorkspaceDownloadPermit.capture`
checks this registration against the original owner and current full identity,
Session/source/project authority and exact Workspace and tool resource
revisions. Reads use the sealed asset FD, not the original source path. Tool
completion does not revoke already delivered assets; current permission,
source revocation, asset TTL and sealed-file identity still apply. Successful
staging cannot authorize another actor or a new grant revision. The protected
URL uses exactly `token` and `session_id`; legacy user routing is not emitted.

Organization commit, revoke and expiry cleanup also pin and compare the original
root device/inode before mutating via its FD. A replaced root or symlinked
ancestor is rejected, including restart recovery from copied sidecars carrying
another root identity. Unknown cleanup is retained and logged with fixed text;
it is never reported as deletion of a replacement directory. Legacy asset
records retain their existing branch. Safe metadata probing of sealed ZIP files
uses the same verified FD rather than reopening a raw sealed path.

A cancelled staging await retains ownership of its worker, drains it, and
removes only the returned unexposed asset. This drain has no independent time
limit. While that worker has not exited, the original task remains alive and a
bounded Session stop must report unconfirmed exit; this wait is not evidence of
completed cleanup. Push failure removes unexposed assets; an uncertain push or
commit failure retains a staged asset under the original TTL rather than
pretending publication was undone.

The new deterministic tests exercise the actual Native AbilityManager and
SmartApproval grant issuer, original owner/resource sidecars, HMAC, sealed
storage, download permit and actual ASGI GET/HEAD/range/final-buffer guards.
The model, push delivery and in-process E2A bridge are fixtures. They are not a
real browser/human approval or external Provider acceptance test. Evidence and
precise source combinations are recorded in `/tmp/r2b-sealed-artifacts/`.
Integration stable and the new formally locked core pairing remain required
before accepting this component change. The automatic-approval product entry is
currently disabled as described below; no ordinary approved-file UI pass is
claimed or reachable through its current configuration.

Candidate affected validation: **243 passed** in 36.21 seconds (one existing
Authlib deprecation warning), plus Ruff and `git diff --check`. The source is
this isolated branch based on swarm `10604eeb`, overlaid explicitly with
`PYTHONPATH`; core is the noneditable installed
`33d5b922f6a09df07a856c85f47b4851fa5f61e4` in
`/tmp/r2b-core-33d5b922-locked-venv`. This is local affected-source validation,
not verification of the integration owner's later core lock or full stable.
The new module is covered by the existing governance-projects directory
collection; its 180-second limit and existing exclusions are unchanged.


### Product entry availability clarification

Upstream `b6cd34c2b1ea068b57cb6f9a91a0d15a0c9457f9` (2026-09-18,
`!6891 fix(permissions): disable auto permission mode`) deliberately reduced
`auto_config._VALID_RUNTIME_MODES` to `manual`, removed the UI automatic option
and SmartApproval product documentation, and removed `automatic` from Gateway
accepted profiles. The commit message does not record a more specific business
or security reason; none is inferred here. Its `internal_auto_mode` pytest
fixture explicitly enables retained internals only for tests.

In the current source, `is_auto_permission_enabled` therefore returns false
for ordinary configuration. `interface_deep._auto_permission_enabled_for_config`
uses that predicate, and the real send-file assembly derives
`require_execution_authorization` solely from `_enable_auto_permission`.
Team tool assembly and Provider artifact projection retain the default false.
The exported Python component `SendFileToolkit` still supports the explicit
constructor/update-runtime-context keyword `require_execution_authorization=True`,
with a real host execution grant and Native source certificate required. This
is a retained component interface, not a high-level `jiuwenswarm_sdk.Client`
option or a CLI switch. No such SDK/CLI option is present in their source.

The new sealed tests exercise that explicit component composition without
changing `_VALID_RUNTIME_MODES`. Re-enabling automatic approval or exposing a
new manual sealed product option requires a separate authorized product change
and its UI acceptance. The already-open ordinary owner Workspace download UI
remains separate and does not prove this currently disabled sealed composition.
