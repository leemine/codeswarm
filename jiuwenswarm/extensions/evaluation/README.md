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
