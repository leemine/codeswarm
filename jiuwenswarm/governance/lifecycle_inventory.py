"""Read-only lifecycle DTOs filtered by existing project/session authorities."""
from .session_sharing import SessionSharingDenied


def project_lifecycle_projection(params, permit):
    from jiuwenswarm.server.runtime.session import lifecycle as lc, project_store
    if permit.method != 'project.lifecycle' or not permit.revalidate():
        raise SessionSharingDenied('current lifecycle read permit required')
    access = permit.host._storage
    identity = permit.identity

    def visible(pid):
        # Project catalog ACLs use actor_id, as ProjectAdapter does. Subject
        # credential checks belong to execution/resource consumption.
        return access.authorize(pid, identity.actor_id, 'read').allowed

    if params.get('events'):
        entries = []
        for entry in lc.event_snapshots():
            payload = entry.get('payload', {})
            sid = payload.get('session_id')
            if sid:
                allowed = permit.host.owner_current(sid, identity)
            else:
                allowed = visible(payload.get('project_id', ''))
            if allowed:
                # Internal cleanup results contain other session IDs and paths.
                summary = {key: value for key, value in entry.get('result', {}).items()
                           if key in {'deleted', 'archived', 'exit_confirmed', 'project_id'}}
                if sid:
                    summary['session_id'] = sid
                entries.append({**entry, 'result': summary})
        result = {'events': entries}
    elif params.get('inventory'):
        projects = []
        for project in project_store.list_projects(include_hidden=True, cache_bust=True):
            if visible(project.project_id):
                projects.append({'project_id': project.project_id,
                                 'operation': lc.projection('project', project.project_id)['lifecycle_operation']})
        result = {'projects': projects}
    else:
        pid = lc.validate_id(params.get('project_id'))
        if not visible(pid):
            raise SessionSharingDenied('project unavailable')
        result = lc.projection('project', pid)
        result['exists'] = project_store.get_project_by_id(pid, cache_bust=True) is not None
        result['operation'] = result['lifecycle_operation']
    if not permit.revalidate():
        raise SessionSharingDenied('lifecycle audience changed')
    return result


LIFECYCLE_EVENTS = frozenset({
    'project.lifecycle.updated', 'project.deleted', 'session.lifecycle.updated',
    'session.archived', 'session.unarchived', 'session.deleted',
})


def lifecycle_event_delivery(frame, identity_resolver, host):
    """Bind a bounded notification to its current recipient until queue drain."""
    from .application_boundary import admit_application_request
    event = frame.get('event')
    payload = frame.get('payload')
    if event not in LIFECYCLE_EVENTS or not isinstance(payload, dict):
        raise SessionSharingDenied('invalid lifecycle event')
    permit = admit_application_request('project.lifecycle', {'events': True},
                                      identity_resolver=identity_resolver, host=host)
    identity = permit.identity
    sid = payload.get('session_id')
    pid = payload.get('project_id')
    if event.startswith('session.'):
        if not isinstance(sid, str) or not sid or payload.get('resource_id', sid) != sid:
            raise SessionSharingDenied('invalid session lifecycle event')
        def scope():
            return host.owner_current(sid, identity)
    else:
        if not isinstance(pid, str) or not pid or payload.get('resource_id', pid) != pid or sid:
            raise SessionSharingDenied('invalid project lifecycle event')
        def scope():
            return host._storage.authorize(pid, identity.actor_id, 'read').allowed
    fields = {'resource_id', 'operation_id', 'revision', 'project_id', 'session_id',
              'execution_blocked', 'stop_pending', 'deleted', 'archived', 'exit_confirmed'}
    public = {key: value for key, value in payload.items() if key in fields
              and type(value) in {str, int, bool}}
    operation = payload.get('lifecycle_operation')
    if isinstance(operation, dict):
        public['lifecycle_operation'] = {key: value for key, value in operation.items()
            if key in {'operation_id', 'resource_type', 'resource_id', 'kind', 'status',
                       'phase', 'retryable', 'updated_at', 'stop_pending'}
            and type(value) in {str, int, float, bool}}
    else:
        public['lifecycle_operation'] = None
    def guard():
        try:
            return permit.revalidate() and scope()
        except Exception:
            return False
    if not guard():
        raise SessionSharingDenied('lifecycle recipient unavailable')
    return {'type': 'event', 'event': event, 'payload': public}, guard
