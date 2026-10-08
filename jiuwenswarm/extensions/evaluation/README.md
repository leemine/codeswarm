# Evaluation experiments (M1 + independent acceptance)

Application ID: `evaluation-experiments`; navigation: `app:evaluation-experiments`.
The AgentServer owns one instance of the evaluation metadata store and use cases.
The Gateway only forwards the fixed evaluation RPC methods through the existing
connection. The existing Runtime owns execution, interactions, history and exit.

M1 uses `shared-host-v1`: Native and acceptance operate in the same assigned
workspace. Results are shared-environment acceptance, not independent validation.
Only trusted personal host execution is supported by this initial environment;
organization/project resource admission must retain its existing checks.

Task and dataset versions are immutable. Editing a draft and publishing produces
a new revision; experiment snapshots preserve the old content and material digest.
Each experiment contains independent Trial IDs; retries create Attempt IDs in the
same Trial. Pending/unknown outcomes are never automatically retried after restart.
Metadata schema 1 lives in one SQLite database; newer schemas fail closed.

JSONL contains one schema-1 TaskDraft per nonempty line, with `task_id`, `name`,
`instruction`, optional `files` (`path`, `content`), `deliverables`, and `acceptance`.
Paths must be normalized relative paths; no links, external credential fields,
owner IDs or unknown fields. Imports preview line errors and ID conflicts, then
revalidate atomically on save; importing never executes code. Python acceptance
requires a script and bounded timeout. Natural-language-only tasks await manual
review and never count as automatic passes. Credentials stay in the host model
configuration; experiments freeze model/profile references, never key values.


## Run from the existing Web UI

1. Open **Evaluation experiments / 评测实验** in the application sidebar.
2. Load the bundled micro dataset, select a subset, or enter a task and publish it.
   Published versions are read-only; **Edit as new version** preserves earlier runs.
   JSONL import previews every line before saving drafts.
3. Choose a configured model and a Native execution profile, repeats and timeout.
   Acknowledge shared-host execution, freeze the experiment, then explicitly start.
4. Follow each attempt's **Open original session** link for Code tools, questions
   and approvals. Answers stay in the original controls; refreshing restores only
   currently owned questions. The original history includes resumed tool results.
5. Review the original Runtime terminal, confirmed exit, acceptance exit code,
   output and declared-file diff. Export JSON rechecks the current owner.

Cancellation requests the original Runtime's cancellation and waits for its exit
and the observer to close. A closed stream alone is not success. Completed records
survive service restart; an unresolved prior execution remains unknown and is not
resubmitted automatically. An unavailable configuration is an error, not fallback.
Costs and token coverage currently show **unknown**, never an invented zero.

No new package dependency or core source override is required. Enable this bundled
extension through the existing plugin loader and use the normal AgentServer /
Gateway / Web launch path. JSONL and acceptance scripts are trusted local input;
M1 is not an isolation boundary. Independent acceptance is an explicit policy below.

## Verification (2026-10-08)

The management evidence package `EVALUATION_EXPERIMENT/m1-baseline` records the
locked noneditable core source, legacy CLI/Docker baseline, real Native UI runs,
Code waiting/answering/history refresh, cancellation, classification and export.
Run `tools/testctl.py run --profile pr-stable` with the repository's normal strict
network test environment; the profile includes evaluation Python and Web contracts.
The Web build must include this extension. Application frontend peers resolve from
`src/applicationPlugins/ui.ts` to avoid altering unrelated test bundler resolution.


## Independent acceptance (EVAL-05)

Select **Independent container acceptance / 独立容器验收** before freezing an
experiment. Native still uses the original shared-host Code workflow and controls.
After Runtime confirms exit, the verifier reconstructs immutable initial materials
and applies only declared deliverables. It never mounts the execution workspace.
The authoritative Python script and offline dependencies are separate read-only
mounts; verification uses a non-root, network-disabled container with a read-only
root filesystem and private writable temporary directories.

The administrator must preinstall the Docker image named by
`JIUWENSWARM_EVALUATION_IMAGE` (default `python:3.12-slim`); its resolved image ID
is frozen. No image pull is performed. Optional `JIUWENSWARM_EVALUATION_WHEELHOUSE`
contains preapproved wheels whose hashes are frozen. `acceptance.dependency_lock`
is a normalized path present in initial files or declared deliverables. Each lock
line must be `package==version --hash=sha256:<digest>` and match a frozen wheel.
Install is offline, binary-only and hash-required. A changed declared lock can
select another frozen wheel; undeclared host installs do not enter verification.
Files are limited to 1 MiB each, with 4 MiB/100 delivered files per capture; links,
hard links, special files and mutation during reads are rejected. Initial files
can set `executable: true`; controlled delivery preserves file modes and deletions.

Authoritative scripts must execute at least one `assert`. Empty tests, early zero
exit and missing reports are environment errors, not passes. UI evidence includes
image ID, test hash, assertion count, output, delivery hashes/diffs and confirmed
container cleanup. Cancellation checks a persisted random ownership label before
removing a container; uncertain cleanup remains unknown. Old shared-environment
experiments retain their policy and immutable digests.

This is reproducibility isolation for trusted personal tasks, not a hostile-host
or arbitrary malicious-Python security boundary. Organization execution, EVAL-06,
Team evaluation and remote Workers remain outside this implementation. Management
`EVALUATION_EXPERIMENT/eval05` evidence includes real Docker/pip negative fixtures
and separately identified real Native/Code UI verification.

Execution completion includes the original request's resumed control descendants.
A root stream may finish while a later question or permission still owns work;
the plugin reads the Runtime receipt lineage and waits for every active receipt.
Cancellation targets the remaining live receipts through the original Runtime
cancel channel. A missing lineage remains unknown; no history replay or closed
stream is treated as successful completion.

Frozen experiments also pin implementation content and the installed core source.
Starting an old pending experiment after a code/dependency change is rejected with
`IMPLEMENTATION_CHANGED` / `DEPENDENCY_CHANGED`; copy its configuration and freeze a
new experiment. Old results remain readable, and cancellation remains available.
Relocating an identical installation or reorganizing commits does not change code
identity. Each submitted attempt records its actual execution source separately.
