"""Private cleanup authority for one original Session generation.

This grants no history, model, tool, credential, or continuation access. Runtime
owns the close and resource release; transport hints never select an Agent.
"""
from __future__ import annotations

import json
from contextvars import copy_context
from dataclasses import dataclass, field

from jiuwenswarm.governance.session_boundary import is_cleanup_request
from jiuwenswarm.governance.session_sharing import SessionSharingDenied

_MARKER = object()


@dataclass(frozen=True, repr=False)
class SessionCleanupAuthority:
    session_id: str
    channel_id: str
    request_id: str
    method: str
    generation: int | None
    executions: frozenset[str]
    _params: str = field(repr=False)
    _identity: object = field(repr=False)
    _stamp: tuple = field(repr=False)
    _runtime: object = field(repr=False)
    _request: object = field(repr=False)
    _context: object = field(repr=False)
    _marker: object = field(repr=False)

    def __deepcopy__(self, memo):
        return self

    def check(self, *, execution=True):
        runtime, request = self._runtime, self._request
        if (self._marker is not _MARKER or runtime._closed
                or request.session_id != self.session_id or request.channel_id != self.channel_id
                or request.request_id != self.request_id or request.req_method.value != self.method
                or json.dumps(request.params or {}, sort_keys=True, separators=(',', ':')) != self._params
                or self._context.run(runtime._governance_identity, request) != self._identity
                or runtime._organization_session_host.cleanup_owner_stamp(
                    self.session_id, self._identity) != self._stamp):
            raise SessionSharingDenied('original cleanup authority changed')
        if execution:
            current = runtime._session_coordinator.snapshot_session(self.session_id)
            if ((current.generation if current else None) != self.generation
                    or current is not None and any(
                        not item.state.terminal and item.execution_id not in self.executions
                        for item in current.executions)):
                raise SessionSharingDenied('cleanup cannot select a newer execution')


def capture_cleanup(runtime, request):
    if runtime._organization_session_host is None:
        return None
    method = request.req_method.value if request.req_method else ''
    if not is_cleanup_request(method, request.params or {}):
        return None
    if (request.params or {}).get('session_id', request.session_id) != request.session_id:
        raise SessionSharingDenied('conflicting cleanup Session references')
    existing = getattr(request, '_cleanup_authority', None)
    if existing is not None:
        if (not isinstance(existing, SessionCleanupAuthority) or existing._marker is not _MARKER
                or existing._runtime is not runtime or existing._request is not request):
            raise SessionSharingDenied('cleanup authority belongs to another request')
        existing.check()
        return existing
    identity = runtime._governance_identity(request)
    stamp = runtime._organization_session_host.cleanup_owner_stamp(request.session_id, identity)
    session = runtime._session_coordinator.snapshot_session(request.session_id)
    active = tuple(item for item in session.executions if not item.state.terminal) if session else ()
    target = (request.params or {}).get('target_request_id', '')
    if target and any(item.request_id != target for item in active):
        raise SessionSharingDenied('cleanup target is no longer the sole original execution')
    authority = SessionCleanupAuthority(
        request.session_id, request.channel_id, request.request_id, method,
        session.generation if session else None, frozenset(item.execution_id for item in active),
        json.dumps(request.params or {}, sort_keys=True, separators=(',', ':')),
        identity, stamp, runtime, request, copy_context(), _MARKER,
    )
    authority.check()
    request._cleanup_authority = authority
    return authority


async def cancel_owned_session(runtime, request, authority):
    from jiuwenswarm.common.schema.agent import AgentResponse
    from jiuwenswarm.runtime.session.model import SessionCloseTimeoutError

    if authority.method not in {'chat.cancel', 'chat.interrupt'}:
        raise SessionSharingDenied('cancel requires a pure cancellation request')
    authority.check()
    sid, channel = authority.session_id, authority.channel_id
    if authority.generation is None:
        # Never search a default/project Agent or allocate one for cleanup.
        existing = runtime._agent_manager.get_agent_for_session_nowait(channel, sid)
        if existing is not None or runtime.is_session_running(sid):
            raise SessionSharingDenied('existing execution has no cleanup generation')
    else:
        async def release():
            authority.check()
            await runtime._agent_manager.release_subagent_runtime_for_session(
                channel_id=channel, session_id=sid, reason='owner_cancel')
            authority.check()
            await runtime._agent_manager.stop_existing_session_runtime(channel_id=channel, session_id=sid)
            authority.check()
            # Retire the manager's original binding/cache bookkeeping only
            # after the exact child's Provider/tasks have confirmed exit.
            await runtime._agent_manager.cleanup_session_runtime(channel_id=channel, session_id=sid)
            authority.check()
            await runtime._forget_agent_execution_owner(channel_id=channel, session_id=sid)
            runtime._plan_controller.reset_session(sid)

        closed = await runtime._session_coordinator.close_session(
            sid, generation=authority.generation, wait_timeout=10, release_resources=release)
        if closed.timed_out:
            raise SessionCloseTimeoutError(sid, closed.timed_out)
    authority.check()
    await runtime._clear_pending_interaction(sid)
    authority.check()
    return AgentResponse(request_id=request.request_id, channel_id=request.channel_id, ok=True,
        payload={'event_type': 'chat.interrupt_result', 'intent': 'cancel', 'success': True,
                 'session_id': sid, 'exit_confirmed': True})
