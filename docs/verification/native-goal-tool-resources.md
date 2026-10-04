# Native Goal report tool resource boundary

Refs: leemine/codeswarm#33

`submit_goal_report` now requires an explicit `tool` resource reference
`native:submit_goal_report` with `invoke`. A name or grant alone is insufficient:
the actual core `SubmitGoalReportTool`, original wrapped native method, installed
card, original TaskCompletionRail tool/sink, Native owner and backing Session,
GoalManager, active work/facade, PendingTurn, ExecutionOrigin, Goal id/revision
and attempt must remain exact. The existing Native executor proof captures these
references before authority callbacks and rechecks after callbacks. The resolver
only maps requirements; the existing ResourceGuard remains the authorizer.

The original core report method performs no await between final authority and
its sink write. No tool schema/catalog changes, alternate implementation,
credential store, state machine, queue, Runtime or Session lock are added.
Legacy calls without the mandatory host boundary remain unchanged. Reading
malformed Goal state for this proof does not invoke SessionGoalStore.load's
repair/clear behavior.

Actual component tests use real organization principals, owner/project/resource
sidecars, default Runtime resource resolution, original Coordinator and Native
Goal execution, AbilityManager and core Tool final checks. Only model HTTP and
Session storage/stream IO use bounded synthetic fixtures. The fixture explicitly
uses the production shared outer/inner AbilityManager; it does not fake tool
ownership or disable the lifecycle factory. Tests cover successful completion,
missing grant, same-name replacement, foreign sink, manager change, stale
attempt, wrong source and mutation during the resource policy callback.

Local source-overlay affected verification: 92 passed (Goal report, old Native
tool resources/authority, artifact authority and MCP host chain), one existing
Authlib deprecation warning. Exact commands/source/logs are retained under
`/tmp/r2b-native-goal-tool-resources`; this is not noneditable lock/CI acceptance
or real Provider validation. The existing governance suite discovers and runs
the new test file without manifest exceptions.

## Remaining read-tool boundary

`get_current_goal` remains fail closed under this resource mapper, even with a
name-matching tool resource. Its original core invoke awaits GoalManager.get's
control lock and then reads the latest record; a pre-invocation-only grant does
not bind the target or authorize the actual locked read. A separate core consumer
check must verify the original operation/manager/Session/Goal and current resource
authority inside that lock, and denial must not become a successful no-goal
response. This report-tool slice does not close that requirement or the complete
managed Native Goal acceptance milestone.
