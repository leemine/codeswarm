"""Owner execution permission for existing Native Single Goal commands."""
from __future__ import annotations

from .contracts import AuthorizationDecision, TrustedIdentity
from .goal_read import _capture_goal_route
from .session_sharing import SessionSharingDenied


def validate_goal_mutation(params):
    """Accept the existing Goal UI fields, without execution-route overrides."""
    if (type(params) is not dict or type(params.get('action')) is not str
            or params['action'] not in {'set', 'resume', 'pause', 'clear'}):
        raise SessionSharingDenied('Explicit Goal mutation required')
    fields = {'session_id', 'action', 'mode', 'work_mode', 'project_id'}
    action = params['action']
    if action in {'set', 'resume'}:
        fields.add('model_name')
    if action == 'set':
        fields.update({'objective', 'overwrite_confirmed', 'token_budget', 'max_attempts'})
        if type(params.get('objective')) is not str or not params['objective'].strip():
            raise SessionSharingDenied('Goal objective required')
    if set(params) - fields:
        raise SessionSharingDenied('Unsupported Goal mutation parameters')
    for key, value in params.items():
        if key == 'overwrite_confirmed':
            valid = type(value) is bool
        elif key in {'token_budget', 'max_attempts'}:
            valid = type(value) is int and value > 0
        else:
            valid = type(value) is str and bool(value.strip())
        if not valid:
            raise SessionSharingDenied('Invalid Goal mutation parameter')


def capture_goal_mutation_route(session_id, params, check_owner):
    try:
        return _capture_goal_route(session_id, params, check_owner,
                                   validate_goal_mutation, model_hint=True)
    except (TypeError, ValueError):
        raise SessionSharingDenied('Original Native Goal route unavailable') from None


def admit_goal_mutation(params, *, identity_resolver, host, envelope_session=None):
    """Capture routing before the first identity callback; allocate no execution."""
    from .session_boundary import SessionRequestPermit, _session
    from jiuwenswarm.server.runtime.session.session_metadata import get_session_metadata

    validate_goal_mutation(params)
    sid = _session(params.get('session_id') or envelope_session)
    if envelope_session and sid != envelope_session:
        raise SessionSharingDenied('Conflicting Goal Session references')
    storage = host._storage
    # The shared route captures the same metadata before invoking this callback.
    # Fix the project independently here before any caller-supplied auth code.
    metadata = get_session_metadata(sid, cache_bust=True, enable_writeback=False, infer_defaults=False)
    project = metadata.get('project_id') if type(metadata) is dict else None
    if type(project) is not str or not project:
        raise SessionSharingDenied('Explicit Goal project required')
    identity = epoch = decision = None

    def check_owner():
        nonlocal identity, epoch, decision
        if host._storage is not storage:
            raise SessionSharingDenied('Original Goal authority storage changed')
        current = identity_resolver()
        if host._storage is not storage:
            raise SessionSharingDenied('Original Goal authority storage changed')
        if not isinstance(current, TrustedIdentity):
            raise SessionSharingDenied('Authenticated Goal owner required')
        if identity is None:
            identity = current
            epoch = host.owner_revision(sid, identity)
            decision = storage.authorize(project, identity.actor_id, 'execute')
        if (current != identity or host._storage is not storage
                or not host.owner_current(sid, identity)
                or host.owner_revision(sid, identity) != epoch
                or not isinstance(decision, AuthorizationDecision)
                or decision.allowed is not True or decision.project_id != project
                or decision.actor_id != identity.actor_id or decision.action != 'execute'
                or decision.reason in {'legacy_default', 'legacy_unmanaged', 'orphan_owner'}
                or storage.authorize(project, identity.actor_id, 'execute') != decision
                or host._storage is not storage):
            raise SessionSharingDenied('Original Goal execution authorization changed')
        current_metadata = get_session_metadata(sid, cache_bust=True,
                                                enable_writeback=False, infer_defaults=False)
        if type(current_metadata) is not dict or current_metadata.get('project_id') != project:
            raise SessionSharingDenied('Original Goal project changed')

    route = capture_goal_mutation_route(sid, params, check_owner)
    permit = SessionRequestPermit(identity, identity_resolver, host, ((sid, epoch),),
                                  method='command.goal', goal_mutation_route=route)
    if not permit.revalidate():
        raise SessionSharingDenied('Goal execution authorization denied')
    return permit
