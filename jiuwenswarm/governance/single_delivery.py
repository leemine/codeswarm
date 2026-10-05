"""Release scope for organization Single; existing authority remains independent."""
from .session_sharing import SessionSharingDenied


def require_single_delivery(method, params, *, session_id=None, channel_id=None):
    """Reject unreleased execution before any allocation; never grant access."""
    from jiuwenswarm.common.mode_matrix import is_team_mode
    from jiuwenswarm.runtime.request import resolve_agent_request_mode
    from jiuwenswarm.common.schema.agent import AgentRequest
    from jiuwenswarm.common.schema.message import ReqMethod
    from jiuwenswarm.server.runtime.agent_adapter.goal_control import tui_goal_operation

    if type(params) is not dict:
        raise SessionSharingDenied('Single request parameters required')
    if method == 'command.goal' and params.get('action', 'get') != 'get':
        raise SessionSharingDenied('Organization Goal execution is not released')
    if params.get('attach_goal') is not None and params.get('attach_goal') is not False:
        raise SessionSharingDenied('Organization Goal attachment is not released')
    if method not in {'chat.send', 'chat.resume', 'chat.answer', 'chat.user_answer', 'session.create',
                      'session.input.intent', 'command.goal'}:
        return
    if method == 'chat.send':
        operation = tui_goal_operation(AgentRequest('scope', channel_id=channel_id or '',
            req_method=ReqMethod.CHAT_SEND, params=params))
        if operation is not None and operation.get('action') != 'get':
            raise SessionSharingDenied('Organization Goal execution is not released')
    if params.get('team') or params.get('team_hint') or params.get('is_swarm'):
        raise SessionSharingDenied('Organization Team execution is not released')
    candidates = [params]
    if session_id:
        from jiuwenswarm.server.runtime.session.session_history import is_valid_session_id
        if not isinstance(session_id, str) or not is_valid_session_id(session_id):
            raise SessionSharingDenied('Valid Single Session required')
        from jiuwenswarm.server.runtime.session.session_metadata import get_session_metadata
        stored = get_session_metadata(session_id, cache_bust=True,
            enable_writeback=False, infer_defaults=False)
        if isinstance(stored, dict):
            candidates.append(stored)
    for candidate in candidates:
        mode = candidate.get('mode')
        if isinstance(mode, str) and mode.strip():
            canonical = resolve_agent_request_mode(mode, work_mode=candidate.get('work_mode'))[2]
            if is_team_mode(canonical):
                raise SessionSharingDenied('Organization Team execution is not released')
