"""Owner-only Goal query through existing metadata, cache and persistence."""
from __future__ import annotations

from contextvars import copy_context
from copy import deepcopy

from jiuwenswarm.governance.preparation import GovernanceError


async def read_goal(runtime, request):
    from jiuwenswarm.governance.session_boundary import admit_session_request, bind_goal_read_delivery
    from jiuwenswarm.governance.goal_read import capture_goal_read_route
    from jiuwenswarm.runtime.events import RuntimeEvent
    from jiuwenswarm.runtime.goal_read import read_native_goal

    host, manager, guard = (runtime._organization_session_host, runtime._agent_manager,
                            runtime._submission_guard)
    if host is None:
        raise GovernanceError('Trusted Goal read host required')
    context = copy_context()
    sid, rid, channel, method, stream = (request.session_id, request.request_id,
        request.channel_id, request.req_method, request.is_stream)
    params, metadata = request.params, request.metadata
    values, meta_values = deepcopy(params), deepcopy(metadata)
    identity = project = decision = permit = None

    def static():
        if (runtime._closed or runtime._organization_session_host is not host
                or runtime._agent_manager is not manager or runtime._submission_guard is not guard
                or request.session_id != sid or request.request_id != rid
                or request.channel_id != channel or request.req_method is not method
                or request.is_stream != stream or request.params is not params
                or request.metadata is not metadata or params != values or metadata != meta_values):
            raise GovernanceError('Original Goal read request changed')

    def current_identity():
        static()
        current = context.run(runtime._governance_identity, request)
        if current is None or current != identity:
            raise GovernanceError('Original Goal read principal changed')
        static()
        return current

    # This first capture establishes routing facts only. The reader pins its
    # actual Session/storage before calling the real owner authorization below.
    route = capture_goal_read_route(sid, params, static)
    lookup = getattr(manager, '_peek_agent_for_session_nowait', None)
    if not callable(lookup):
        raise GovernanceError('Existing Goal cache lookup unavailable')

    def check_owner():
        nonlocal identity, project, decision, permit
        static()
        if permit is None:
            identity = context.run(runtime._governance_identity, request)
            if identity is None:
                raise GovernanceError('Trusted Goal read principal required')
            project = context.run(runtime._governance_project, request, action='read')
            decision = guard.check_access(project, identity, 'read')
            permit = admit_session_request('command.goal', params, identity_resolver=current_identity,
                host=host, envelope_session=sid)
        if binding is not None and binding.subject_id != identity.subject_id:
            raise GovernanceError('Goal Binding belongs to another subject')
        if (not permit.revalidate() or context.run(runtime._governance_project, request, action='read') != project
                or guard.check_access(project, current_identity(), 'read') != decision):
            raise GovernanceError('Original Goal read authorization changed')
        static()

    def lookup_facade():
        static()
        return lookup(route.channel_id, sid)

    facade = lookup_facade()
    root = getattr(facade, '_adapter', None)
    child = (root if getattr(root, '_is_session_scoped_adapter', False) else
             getattr(root, '_session_adapters', {}).get(sid))
    native = getattr(child, '_native_execution', None)
    binding = getattr(getattr(native, 'engine', None), 'binding', None)
    result = await read_native_goal(descriptor=route.descriptor,
        lookup_descriptor=route.lookup_descriptor, lookup_facade=lookup_facade,
        check_owner=check_owner, binding=binding)
    payload = result.to_payload()
    if stream:
        payload['event_type'] = 'goal.snapshot'
    bind_goal_read_delivery(sid, identity, result)
    result.final_check()
    return RuntimeEvent(request_id=rid, channel_id=channel, session_id=sid,
                        payload=payload, is_complete=True)
