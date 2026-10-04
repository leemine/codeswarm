# Native Goal organization ingress and delivery

This bounded slice admits the existing `command.goal` mutation schema only for an owned, explicitly configured Native Single Session. It does not start an executor, allocate a Binding, read a checkpoint, mutate a Goal, or create a second permission/lifecycle store. Runtime remains responsible for original active controls, fresh idle admission and provider completion.

`get` keeps its original owner/project read-only route. `set`, `resume`, `pause` and `clear` require the original full authenticated identity, current Session owner/revision and an explicit project `execute` decision from the same host ProjectAccessStore. A Session owner with `read` and `execute` need not have project `admin`. Shared history access is not ownership and supplies no mutation authority. These Goal actions are not cleanup exemptions after execute permission is removed.

The exact existing fields are:

- Common: `session_id`, `action`, `mode`, `work_mode`, `project_id` (routing hints must match original metadata).
- `set`: additionally nonempty `objective`, boolean `overwrite_confirmed`, positive integer `token_budget`/`max_attempts`, and optional `model_name`.
- `resume`: additionally optional `model_name`.
- `pause`/`clear`: no objective, model or execution parameters.

Unknown fields, Provider/profile overrides, share tokens and bool-as-integer budgets are rejected. Existing Web requests remain shaped as before: set/resume use stream and pause/clear use unary. This slice does not decide managed unary set/resume execution semantics.

Routing metadata, original parameters and the authority storage reference are fixed before the first identity callback. Every permit revalidation checks both sides of that callback and rereads original Native profile revision/fingerprint, Single mode, project, owner revision, model selection and current execute decision. Existing AgentServer wire and Gateway queue sinks reuse the original request permit; queued private responses are denied after changes. No callback or grant is serialized.

Model hint compatibility is deliberately bounded: the wire `model_name` is compared to the persisted Session `model` selection key, not a label or a new default. An empty original model plus a UI-supplied default key remains explicitly unsupported here; proving that key against an existing Runtime Binding is follow-up work. This boundary does not select or alter a model to fill that gap. Model credentials and actual per-request model/tool resource enforcement remain Runtime/provider responsibilities.

Affected deterministic validation: 213 passed (47 new), with one existing Authlib warning; source overlay, not locked-pair acceptance. Validation uses real temporary authentication, owner/project storage, routing metadata and the existing AgentServer/Gateway sinks. SDK/Provider execution is not started. New tests are in `tests/unit_tests/governance/test_native_goal_mutation.py`, collected by the existing governance stable directory command. Source-only review/test results and any environment failures are retained at `/tmp/r2b-native-goal-governance`; final locked-pair stable and public Runtime/UI integration remain required. This module alone does not close Native Goal or R2-B acceptance.

The AgentServer-local optional result binding accepts only the exact Runtime `NativeGoalMutationDelivery` type with the original Session and full identity. Its private `final_check` runs again at the existing wire sink and cannot alter the route unnoticed. Direct Runtime consumption also checks the receipt but creates no wire permit. This callback is never serialized: the separate Gateway process revalidates its own original route/owner/execute permit, not the AgentServer's in-memory core receipt. Runtime's receipt construction and outer-consumer binding are supplied by the separate integration package; the contract-fixture tests here are explicitly not proof of actual core receipt semantics.
