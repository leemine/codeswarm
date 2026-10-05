"""Capture a temporary Goal command against the existing Runtime owner."""
from __future__ import annotations

import copy
from contextvars import copy_context

from jiuwenswarm.governance.preparation import GovernanceError
from jiuwenswarm.runtime.harness.native_goal_control import capture_native_goal_control


def capture_control(runtime, request, parent):
    """Keep the command principal separate from the original execution source."""
    coordinator = runtime._session_coordinator
    captured = coordinator.native_goal_control_admission(request.session_id, request.request_id)
    if captured is None:
        raise GovernanceError('original managed Native Goal unavailable')
    admission, check_producer = captured
    if admission.owner is not parent:
        raise GovernanceError('original Native Goal parent changed')
    native = admission.native
    session_id, request_id, channel_id = request.session_id, request.request_id, request.channel_id
    method, is_stream = request.req_method, request.is_stream
    params, metadata = request.params, request.metadata
    values, metadata_values = copy.deepcopy(params), copy.deepcopy(metadata)
    identity = runtime._governance_identity(request)
    project_id = runtime._governance_project(request)
    guard, host, manager = (runtime._submission_guard, runtime._organization_session_host,
                            runtime._agent_manager)
    decision = guard.check_access(project_id, identity, 'execute')
    revision = host.owner_revision(session_id, identity) if host is not None else None
    owner_channel = admission.record.channel_id
    lookup = getattr(manager, 'get_agent_for_session_nowait', None)
    facade = lookup(owner_channel, session_id) if callable(lookup) else None
    adapter = getattr(facade, '_adapter', None)

    def selected_child():
        if getattr(adapter, '_is_session_scoped_adapter', False):
            return adapter
        cached = getattr(adapter, '_get_cached_session_adapter', None)
        return cached(session_id) if callable(cached) else None

    child = selected_child()
    context = copy_context()

    def check_static():
        if (runtime._closed or runtime._session_coordinator is not coordinator
                or runtime._agent_manager is not manager
                or runtime._submission_guard is not guard
                or runtime._organization_session_host is not host
                or request.session_id != session_id or request.request_id != request_id
                or request.channel_id != channel_id or request.req_method is not method
                or request.is_stream != is_stream or request.params is not params
                or request.metadata is not metadata or params != values or metadata != metadata_values
                or identity is None or native.engine.binding.subject_id != identity.subject_id
                or native.engine.binding.host_session_id != session_id
                or not callable(lookup) or lookup(owner_channel, session_id) is not facade
                or getattr(facade, '_adapter', None) is not adapter or selected_child() is not child
                or child is None or getattr(child, '_native_execution', None) is not native
                or getattr(child, '_instance', None) is not native._native.agent):
            raise GovernanceError('original Native Goal command changed')

    def check_host():
        check_static()
        if (runtime._governance_identity(request) != identity
                or runtime._governance_project(request) != project_id
                or (host is not None and host.owner_revision(session_id, identity) != revision)):
            raise GovernanceError('original Native Goal owner changed')
        runtime._require_session_owner(session_id, request)
        if guard.check_access(project_id, identity, 'execute') != decision:
            raise GovernanceError('original Native Goal authorization changed')
        check_static()

    def check():
        check_producer()
        context.run(check_host)

    def check_ack():
        check_producer(for_ack=True)
        context.run(check_host)

    # Core target capture precedes this temporary callback. The capability
    # performs the final pure host/core checks after all callback code.
    control = capture_native_goal_control(native, source=admission.source,
        request_id=request_id, check_current=check, check_ack=check_ack)
    request._native_goal_control = control
    return facade, control
