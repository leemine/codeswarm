"""Resume the original private continuation without compiling another seed.

The Coordinator's existing interaction claim is the authority. This transient
handle pins its original cached child and native Turn; it cannot allocate one.
"""
from __future__ import annotations

from contextvars import copy_context
from dataclasses import dataclass

from jiuwenswarm.governance.continuation_context import validate_continuation_context
from jiuwenswarm.governance.resources import ResourceAccessDenied

_SEAL = object()


@dataclass(frozen=True, slots=True, init=False, repr=False)
class _ContinuationControl:
    child: object
    facade: object
    adapter: object
    request: object
    check: object
    seal: object

    def __init__(self, *args, **kwargs):
        raise TypeError("original Runtime control claim required")

    def __deepcopy__(self, memo):
        return self

    def __reduce_ex__(self, protocol):
        raise TypeError("live continuation control cannot be serialized")


def capture_continuation_control(runtime, request, facade):
    adapter = getattr(facade, '_adapter', None)
    routes = getattr(adapter, '_native_session_routes', {})
    sid = request.session_id
    route = routes.get(sid)
    context = route[3] if route is not None and len(route) > 3 else None
    if context is None:
        return None
    control_id = runtime._control_request_id(request)
    coordinator = runtime._session_coordinator
    parent = coordinator.claimed_control_parent(sid, control_id)
    child = adapter._get_cached_session_adapter(sid)
    native = getattr(child, '_native_execution', None)
    if native is None:
        raise ResourceAccessDenied('original continuation adapter unavailable')
    turn = native._native.active_turn
    identity = runtime._governance_identity(request)
    host_context = copy_context()

    def check():
        if (runtime._closed or request.session_id != sid
                or runtime._control_request_id(request) != control_id
                or host_context.run(runtime._governance_identity, request) != identity
                or identity != context.identity
                or coordinator.claimed_control_parent(sid, control_id) is not parent
                or runtime._agent_manager.get_agent_for_session_nowait(
                    request.channel_id or 'default', sid) is not facade
                or facade._adapter is not adapter
                or adapter._native_session_routes.get(sid) is not route
                or adapter._get_cached_session_adapter(sid) is not child
                or child._native_execution is not native
                or native.binding is not route[2].binding
                or native._closing or native._closed
                or turn is None or native._native.active_turn is not turn
                or turn.abort_requested
                or native.request_id_for_turn(turn.turn_id) != parent.request_id):
            raise ResourceAccessDenied('original continuation control changed')
        model = (request.params or {}).get('model_name')
        if model not in (None, '', context.seed.proof.request.model_name):
            raise ResourceAccessDenied('continuation control cannot select a model')
        validate_continuation_context(context, sid, parent.request_id)

    check()
    retained = object.__new__(_ContinuationControl)
    for key, value in dict(child=child, facade=facade, adapter=adapter,
                           request=request, check=check, seal=_SEAL).items():
        object.__setattr__(retained, key, value)
    return retained


def continuation_control(request, *, facade=None, adapter=None, child=None):
    retained = getattr(request, '_continuation_control', None)
    if retained is None:
        return None
    if (type(retained) is not _ContinuationControl or retained.seal is not _SEAL
            or retained.request is not request
            or facade is not None and retained.facade is not facade
            or adapter is not None and retained.adapter is not adapter
            or child is not None and retained.child is not child):
        raise ResourceAccessDenied('original continuation control required')
    retained.check()
    return retained
