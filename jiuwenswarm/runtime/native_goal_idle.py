"""Current controller authority for the original, confirmed idle Goal host."""
from contextvars import copy_context
from copy import deepcopy
from dataclasses import fields

from openjiuwen.harness.engine import ExecutionBinding

from openjiuwen.harness.goal.schema import GoalRecord

from jiuwenswarm.governance.preparation import GovernanceError
from jiuwenswarm.runtime.harness.native_goal_idle_control import capture_idle_goal_control
from jiuwenswarm.runtime.harness.native_session import NativeExecutionSession
from jiuwenswarm.runtime.native_goal_readmission import previous_exit
from jiuwenswarm.runtime.session import SessionWorkKind
from jiuwenswarm.runtime.session.model import SessionExecutionState
from jiuwenswarm.runtime.native_goal_mutation import NativeGoalMutationDelivery


def capture_control(runtime, request):
    """Capture before callbacks; old execution proves exit, never authorization."""
    sid, rid, channel = request.session_id, request.request_id, request.channel_id
    method, stream, params, metadata = request.req_method, request.is_stream, request.params, request.metadata
    values, meta_values = deepcopy(params), deepcopy(metadata)
    coordinator, agents, host, guard = (runtime._session_coordinator, runtime._agent_manager,
        runtime._organization_session_host, runtime._submission_guard)
    lookup = getattr(agents, '_peek_agent_for_session_nowait', None)
    facade = lookup(channel, sid) if callable(lookup) else None
    root = getattr(facade, '_adapter', None)
    child = (root if getattr(root, '_is_session_scoped_adapter', False)
             else getattr(root, '_session_adapters', {}).get(sid))
    native = getattr(child, '_native_execution', None)
    if type(native) is not NativeExecutionSession or host is None:
        raise GovernanceError('Original idle Native Goal host is unavailable')
    engine, harness, tool_owner = native.engine, native._native, native._tool_owner
    binding, agent, session = engine.binding, harness.agent, harness._agent_session
    if type(binding) is not ExecutionBinding or agent is None or session is None:
        raise GovernanceError('Original idle Goal binding is unavailable')
    binding_values = tuple(getattr(binding, f.name) for f in fields(ExecutionBinding))
    manager = agent.goal_manager
    store = manager._store if manager is not None else None
    if store is None or store._session is not session:
        raise GovernanceError('Original idle Goal backing Session differs')
    execution = manager._execution
    pinned = [(obj, name, getattr(obj, name)) for obj, names in (
        (harness, ('_command_lock', '_first_managed_turn')),
        (agent, ('_interaction_send_lock', '_interaction_control_lock', 'loop_controller',
                 '_event_manager', '_interaction_output', '_interaction_session')),
        (manager, ('_execution', '_control_lock')),
        (execution, ('_owner', '_event_manager')),
    ) for name in names]
    if manager._control_lock is not agent._interaction_control_lock:
        raise GovernanceError('Original idle Goal control lock differs')
    expected = manager.peek() if manager is not None else None
    if type(expected) is not GoalRecord or expected.session_id != sid:
        raise GovernanceError('Existing idle Goal required')
    previous, check_previous = previous_exit(native, manager, expected, coordinator)
    owner = coordinator.native_execution_owner(sid, rid)
    producer, record, principal = owner.task, coordinator._sessions.get(sid), owner._execution_authority
    generation = owner.generation
    if (owner.work_kind is not SessionWorkKind.GOAL_CONTROL or owner.parent_execution_id is not None
            or producer is None or producer.done() or owner._native_admission is not None):
        raise GovernanceError('Fresh idle Goal controller required')
    context = copy_context()
    from jiuwenswarm.governance.organization_auth import current_principal
    if principal is None or context.run(current_principal) is not principal:
        raise GovernanceError('Original idle Goal principal required')
    identity = project = decision = epoch = None
    completed = False

    def static():
        selected = (root if getattr(root, '_is_session_scoped_adapter', False)
                    else getattr(root, '_session_adapters', {}).get(sid))
        successful_exit = (completed and producer.done() and not producer.cancelled()
                           and producer.exception() is None and owner.state is SessionExecutionState.SUCCEEDED)
        if (any(getattr(obj, name) is not value for obj, name, value in pinned)
                or runtime._closed or runtime._session_coordinator is not coordinator
                or runtime._agent_manager is not agents or runtime._organization_session_host is not host
                or runtime._submission_guard is not guard or lookup(channel, sid) is not facade
                or facade._adapter is not root or selected is not child
                or child._native_execution is not native or child._instance is not agent
                or child._parent_session_id != sid or native.engine is not engine
                or engine.binding is not binding or native._native is not harness
                or native._tool_owner is not tool_owner or harness.agent is not agent
                or harness._agent_session is not session or agent.goal_manager is not manager
                or manager._store is not store or store._session is not session
                or tuple(getattr(binding, f.name) for f in fields(ExecutionBinding)) != binding_values
                or binding.host_session_id != sid or binding.provider_id != 'native'
                or request.session_id != sid or request.request_id != rid or request.channel_id != channel
                or request.req_method is not method or request.is_stream != stream
                or request.params is not params or params != values
                or request.metadata is not metadata or metadata != meta_values
                or coordinator._registry.get(owner.execution_id) is not owner
                or coordinator._sessions.get(sid) is not record or record.generation != generation
                or (owner.task is not producer and not (successful_exit and owner.task is None))
                or (producer.done() and not successful_exit) or producer.cancelling()
                or (owner.state.terminal and not successful_exit) or owner.cancellation_requested or owner._execution_authority is not principal
                or owner.generation != generation or owner.parent_execution_id is not None
                or owner.work_kind is not SessionWorkKind.GOAL_CONTROL or owner._native_admission is not None):
            raise GovernanceError('Original idle Goal controller changed')
        check_previous()

    def authority():
        if (runtime._governance_identity(request) != identity or principal.identity() != identity
                or runtime._governance_project(request) != project
                or host.owner_revision(sid, identity) != epoch or not host.owner_current(sid, identity)
                or guard.check_access(project, identity, 'execute') != decision):
            raise GovernanceError('Idle Goal authorization changed')

    def check():
        static()
        context.run(authority)
        static()

    static()
    identity = runtime._governance_identity(request)
    static()
    if identity is None or native.engine.binding.subject_id != identity.subject_id:
        raise GovernanceError('Idle Goal Binding subject differs')
    project = runtime._governance_project(request)
    static()
    decision = guard.check_access(project, identity, 'execute')
    static()
    epoch = host.owner_revision(sid, identity)
    static()
    control = capture_idle_goal_control(native, request_id=rid, expected_record=expected,
        previous=previous, check_current=check)
    request._native_goal_control = control
    def finish():
        nonlocal completed
        control.check_result()
        completed = True
        # This permits only the already completed operation's acknowledgment.
        # Core retains its original task/action/result and rejects a new action.
        return NativeGoalMutationDelivery(sid, identity, control.check_result)
    return facade, control, finish
