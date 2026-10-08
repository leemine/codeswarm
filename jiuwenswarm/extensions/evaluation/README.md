# Evaluation experiments (M1)

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
M1 is not an isolation boundary. Container-independent acceptance belongs to M2.

## Verification (2026-10-08)

The management evidence package `EVALUATION_EXPERIMENT/m1-baseline` records the
locked noneditable core source, legacy CLI/Docker baseline, real Native UI runs,
Code waiting/answering/history refresh, cancellation, classification and export.
Run `tools/testctl.py run --profile pr-stable` with the repository's normal strict
network test environment; the profile includes evaluation Python and Web contracts.
The Web build must include this extension. Application frontend peers resolve from
`src/applicationPlugins/ui.ts` to avoid altering unrelated test bundler resolution.
