"""Exact live Goal report target for the original Native tool invocation.

A tool resource grant cannot select a Goal, sink, manager or execution source.
The core submit tool has no await between final authority and sink mutation.
"""
from openjiuwen.core.controller.schema.execution_origin import current_execution_origin
from openjiuwen.harness.goal.manager import GoalManager
from openjiuwen.harness.goal.schema import GoalRecord, GoalStatus
from openjiuwen.harness.goal.store import SESSION_GOAL_RECORD_KEY, SessionGoalStore
from openjiuwen.harness.rails.task_completion_rail import TaskCompletionRail
from openjiuwen.harness.tools.goal import GoalReportSink, SubmitGoalReportTool

from .tool_context import NativeExecutionSlice, current_native_execution_slice


def capture_goal_report(proof):
    """Freeze original references before invoking any execution checker."""
    from jiuwenswarm.runtime.harness.native_session import NativeExecutionSession

    tool = proof.executor
    scope = current_native_execution_slice()
    if (type(tool) is not SubmitGoalReportTool or type(scope) is not NativeExecutionSlice
            or type(scope.owner) is not NativeExecutionSession):
        raise ValueError('original Native Goal report source required')
    native = scope.owner
    owner, harness, engine = native._tool_owner, native._native, native.engine
    if owner is None or len(owner) != 4:
        raise ValueError('original Native Goal owner required')
    outer, inner, session, binding = owner
    pending, source = harness.active_turn, current_execution_origin()
    manager, rail = outer.goal_manager, outer._task_completion_rail
    if (type(manager) is not GoalManager or type(rail) is not TaskCompletionRail
            or type(manager._store) is not SessionGoalStore or type(tool._sink) is not GoalReportSink):
        raise ValueError('original Goal manager and report sink required')
    store, sink, tools = manager._store, tool._sink, rail._goal_tools
    active, slot, controller = outer._active_interaction_round, manager._execution_origin, outer.loop_controller
    if active is None or pending is None or source is None:
        raise ValueError('active original Goal execution required')
    work, facade = active.work, active._facade_task
    scope_task, authority = scope._task, scope.tool_authorizer
    source_checker, source_host = source._checker, source.host_value
    events = outer._event_manager
    facts = (work.context.get('session_id'), work.context.get('goal_id'), work.context.get('revision'))
    attempt = sink.attempt_index

    def fixed():
        raw = session.get_state(SESSION_GOAL_RECORD_KEY)
        record = GoalRecord.from_dict(raw) if type(raw) is dict else None
        if (current_native_execution_slice() is not scope or not scope.active
                or scope.owner is not native or scope._task is not scope_task or scope.tool_authorizer is not authority
                or scope_task is None or scope_task.done() or scope_task.cancelling()
                or native._tool_owner is not owner or native._native is not harness
                or native.engine is not engine or engine.binding is not binding
                or native._closing or native._closed or harness.active_turn is not pending
                or pending.abort_requested or pending._origin is not source
                or pending._agent is not outer or pending._session is not session
                or outer.react_agent is not inner or proof.agent is not inner or proof.session is not session
                or current_execution_origin() is not source or source._checker is not source_checker
                or source.host_value is not source_host or outer.goal_manager is not manager
                or manager._store is not store or store._session is not session
                or outer._task_completion_rail is not rail or rail._goal_manager is not manager
                or rail._goal_report_sink is not sink or tool._sink is not sink
                or rail._goal_tools is not tools or sum(value is tool for value in tools) != 1
                or rail._is_goal_round is not True
                or (rail._current_session_id, rail._current_goal_id, rail._current_revision) != facts
                or rail._current_attempt_index != attempt
                or (sink._session_id, sink.goal_id, sink.revision) != facts or sink.attempt_index != attempt
                or manager._execution_origin is not slot or type(slot) is not tuple
                or len(slot) != 4 or slot[:3] != facts or slot[3] is not source
                or outer._active_interaction_round is not active or active.work is not work
                or active._session is not session or active._controller is not controller
                or outer.loop_controller is not controller or active._facade_task is not facade
                or facade is None or facade.done() or facade.cancelling()
                or outer._event_manager is not events or events.active_work is not work
                or work.kind != 'goal' or work.execution_origin is not source
                or (work.context.get('session_id'), work.context.get('goal_id'), work.context.get('revision')) != facts
                or record is None or (record.session_id, record.goal_id, record.revision) != facts
                or record.status is not GoalStatus.ACTIVE or record.attempt_count != attempt
                or session.get_session_id() != binding.host_session_id or facts[0] != binding.host_session_id):
            raise ValueError('original Native Goal report target changed')

    def check():
        fixed()
        source._check_current()
        fixed()

    fixed()
    return check
