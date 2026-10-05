"""Fresh Runtime authority for an explicit Native idle Goal intent.

The original Goal source retains its exact prior exit graph even after the
registry forgets terminal history. That graph proves exit, never new authority.
"""
from __future__ import annotations

import asyncio
from contextvars import copy_context
from copy import deepcopy
from dataclasses import fields

from openjiuwen.harness.engine import ExecutionBinding
from openjiuwen.harness.goal.schema import GoalRecord
from openjiuwen.harness.schema.interaction import SendInputRequest

from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.governance.preparation import GovernanceError
from jiuwenswarm.runtime.harness.native_session import NativeExecutionSession, NativeOwnedTurn
from jiuwenswarm.runtime.native_execution_origin import NativeExecutionAdmission


def previous_exit(native, manager, expected, coordinator):
    """Return exact original receipt and a facts-only recheck, without registry lookup."""
    slot = manager._execution_origin
    if slot is None:
        return None, lambda: None  # Core independently requires its first fresh Pending.
    if type(slot) is not tuple or len(slot) != 4 or slot[:3] != (
            expected.session_id, expected.goal_id, expected.revision):
        raise GovernanceError('Goal record differs from its original live source')
    origin = slot[3]
    owner = getattr(origin, 'host_value', None)
    admission = getattr(owner, '_native_admission', None)
    if type(admission) is not NativeExecutionAdmission:
        raise GovernanceError('Original Goal has no Runtime exit provenance')
    owned, producer, record, source = (admission.owned_turn, admission.producer,
                                      admission.record, admission.source)
    if type(owned) is not NativeOwnedTurn:
        raise GovernanceError('Original Goal lacks its exact Native exit receipt')
    pending, entry = owned._pending, owned._entry

    def check():
        if (getattr(owner, '_native_admission', None) is not admission
                or admission.owner is not owner or admission.native is not native
                or admission.owned_turn is not owned or admission.producer is not producer
                or admission.record is not record or admission.source is not source
                or admission.coordinator is not coordinator or coordinator._sessions.get(expected.session_id) is not record
                or not admission.confirmed.is_set() or not producer.done()
                or not owner.state.terminal or owner.session_id != expected.session_id
                or record.session_id != expected.session_id
                or owned._native is not native or owned.source is not source
                or source.host_value is not owner or owned._pending is not pending
                or pending._origin is not origin or origin.host_value is not owner
                or owned._entry is not entry or entry.owned is not owned
                or entry.lifecycle is None or entry.lifecycle.source is not source):
            raise GovernanceError('Original Goal exit provenance changed or remains active')
        # The Native host and core validate the retained actual exit barrier,
        # Task successes and original pending inventories at admission.
    check()
    return owned, check


async def submit_readmission(runtime, request, child, inputs, *, action):
    """Used by the original adapter after startup, under the new Runtime resource scope."""
    if action not in {'resume', 'attach'} or runtime is None:
        raise GovernanceError('Explicit Runtime Goal readmission required')
    sid, rid, channel, method, stream = (request.session_id, request.request_id,
        request.channel_id, request.req_method, request.is_stream)
    params, metadata = request.params, request.metadata
    fixed_inputs = deepcopy(inputs)
    values, metadata_values = deepcopy(params), deepcopy(metadata)
    if action == 'resume':
        from jiuwenswarm.server.runtime.agent_adapter.goal_control import tui_goal_operation
        intent = (params if method is ReqMethod.COMMAND_GOAL else tui_goal_operation(request))
        if not isinstance(intent, dict) or intent.get('action') != 'resume':
            raise GovernanceError('Original request is not an explicit Goal resume')
    elif method is not ReqMethod.CHAT_SEND or params.get('attach_goal') is not True:
        raise GovernanceError('Original request is not an explicit Goal attachment')
    coordinator, host, guard, agents = (runtime._session_coordinator,
        runtime._organization_session_host, runtime._submission_guard, runtime._agent_manager)
    if host is None:
        raise GovernanceError('Trusted Goal readmission host required')
    lookup = getattr(agents, '_peek_agent_for_session_nowait', None)
    facade = lookup(channel, sid) if callable(lookup) else None
    root = getattr(facade, '_adapter', None)
    native = getattr(child, '_native_execution', None)
    if type(native) is not NativeExecutionSession:
        raise GovernanceError('Original Native Goal executor required')
    engine, harness = native.engine, native._native
    binding, agent, session, tool_owner = engine.binding, harness.agent, harness._agent_session, native._tool_owner
    if type(binding) is not ExecutionBinding or agent is None or session is None:
        raise GovernanceError('Original Native Goal Session is unavailable')
    binding_values = tuple(getattr(binding, f.name) for f in fields(ExecutionBinding))
    manager = agent.goal_manager
    store = manager._store if manager is not None else None
    if store is None or store._session is not session:
        raise GovernanceError('Original Goal backing Session differs')
    expected = manager.peek()
    if type(expected) is not GoalRecord or expected.session_id != sid:
        raise GovernanceError('Existing Goal required for readmission')
    previous, check_previous = previous_exit(native, manager, expected, coordinator)
    owner = coordinator.native_execution_owner(sid, rid)
    producer = owner.task
    principal, generation, record, kind, parent = (owner._execution_authority, owner.generation,
        coordinator._sessions.get(sid), owner.work_kind, owner.parent_execution_id)
    if (owner.parent_execution_id is not None or producer is not asyncio.current_task()
            or owner._native_admission is not None):
        raise GovernanceError('Goal readmission requires a fresh root producer')
    context = copy_context()
    from jiuwenswarm.governance.organization_auth import current_principal
    if context.run(current_principal) is not principal:
        raise GovernanceError('Original Goal producer principal differs from the request')
    identity = project = decision = epoch = None

    def static():
        selected = root if getattr(root, '_is_session_scoped_adapter', False) else getattr(root, '_session_adapters', {}).get(sid)
        if (runtime._closed or runtime._session_coordinator is not coordinator
                or runtime._organization_session_host is not host or runtime._submission_guard is not guard
                or runtime._agent_manager is not agents or not callable(lookup)
                or lookup(channel, sid) is not facade or getattr(facade, '_adapter', None) is not root
                or selected is not child or child._native_execution is not native
                or child._instance is not agent or child._parent_session_id != sid
                or native.engine is not engine or engine.binding is not binding
                or tuple(getattr(binding, f.name) for f in fields(ExecutionBinding)) != binding_values
                or binding.host_session_id != sid or binding.provider_id != 'native'
                or native._native is not harness or harness.agent is not agent
                or harness._agent_session is not session or native._tool_owner is not tool_owner
                or agent.goal_manager is not manager or manager._store is not store or store._session is not session
                or request.session_id != sid or request.request_id != rid or request.channel_id != channel
                or request.req_method is not method or request.is_stream != stream
                or request.params is not params or params != values
                or request.metadata is not metadata or metadata != metadata_values
                or coordinator._registry.get(owner.execution_id) is not owner
                or coordinator._sessions.get(sid) is not record or record.generation != generation
                or owner._execution_authority is not principal or owner.generation != generation
                or owner.work_kind is not kind or owner.parent_execution_id is not parent
                or owner.task is not producer or producer.done() or owner.state.terminal
                or owner.cancellation_requested or owner.request_id != rid or owner.session_id != sid):
            raise GovernanceError('Original Goal readmission request or producer changed')
        check_previous()

    def authority():
        if (runtime._governance_identity(request) != identity
                or runtime._governance_project(request) != project
                or host.owner_revision(sid, identity) != epoch
                or not host.owner_current(sid, identity)
                or guard.check_access(project, identity, 'execute') != decision):
            raise GovernanceError('Goal readmission authorization changed')

    def check():
        static()
        context.run(authority)
        static()

    # All executor and previous-exit references precede the first auth callback.
    static()
    identity = runtime._governance_identity(request)
    if identity is None or binding.subject_id != identity.subject_id or (principal is not None and principal.identity() != identity):
        raise GovernanceError('Goal readmission subject differs from Binding')
    project = runtime._governance_project(request)
    decision = guard.check_access(project, identity, 'execute')
    epoch = host.owner_revision(sid, identity)
    check()
    return await native.submit_goal_readmission(
        request=SendInputRequest(rid, fixed_inputs), action=action,
        expected_record=expected, previous=previous, check_current=check)
