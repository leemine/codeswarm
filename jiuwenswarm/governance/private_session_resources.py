"""Session-bound resources for authenticated conversations without a Project.

Ownership stays in the existing Session directory. The existing private host
authentication config supplies explicit account resource grants; model catalog
visibility and membership in an unrelated Project never grant resource use.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .resources import ResourceAccessDenied, ResourceDecision, ResourceDefinition, _ACTIONS


def workspace_stamp(root):
    path = Path(root)
    if not path.is_absolute() or str(path.resolve()) != str(path) or path.is_symlink():
        raise ResourceAccessDenied('private workspace path changed')
    info = path.stat()
    if not path.is_dir():
        raise ResourceAccessDenied('private workspace unavailable')
    return [info.st_dev, info.st_ino]


class PrivateSessionResources:
    """An adapter over one exact owner record, never a global default grant."""
    def __init__(self, host, session_id):
        self.host = host
        self.session_id = session_id

    def _rows(self, project_id, identity):
        from .organization_auth import configured_authenticator
        with self.host._storage._locked():
            record, owner, source, _ = self.host._current(self.host._storage._load(), self.session_id)
            if (identity != owner or source.get('kind') != 'private'
                    or project_id != source['project_id']):
                raise ResourceAccessDenied('private Session owner required')
            root = source['workspace']
            auth = configured_authenticator()
            if auth is None or not auth.known_actor(identity):
                raise ResourceAccessDenied('private resource authority unavailable')
            config = auth._config()
            policies = config.get('private_session_resources', {})
            if not isinstance(policies, dict):
                raise ResourceAccessDenied('invalid private resource policy')
            policy = policies.get(identity.actor_id, {'revision': 1, 'resources': []})
            if (not isinstance(policy, dict) or type(policy.get('revision')) is not int
                    or policy['revision'] < 1 or not isinstance(policy.get('resources'), list)):
                raise ResourceAccessDenied('invalid private resource policy')
            rows = [{'resource_id': 'session-workspace', 'kind': 'workspace',
                     'reference': root, 'action': action, 'scope': root, 'expires_at': None}
                    for action in ('read', 'write')]
            ids = {'session-workspace'}
            for item in policy['resources']:
                definition = ResourceDefinition(item['resource_id'], item['kind'], item['reference'])
                # Arbitrary filesystem roots cannot be configured as private
                # workspaces. Only the provisioner's owned directory is implicit.
                actions = item['actions']
                if (definition.kind == 'workspace' or definition.resource_id in ids
                        or not isinstance(actions, list) or not actions
                        or any(a not in _ACTIONS[definition.kind] for a in actions)):
                    raise ResourceAccessDenied('invalid private resource grant')
                ids.add(definition.resource_id)
                rows.extend({'resource_id': definition.resource_id, 'kind': definition.kind,
                             'reference': definition.reference, 'action': action,
                             'scope': None, 'expires_at': None} for action in actions)
            # Changes invalidate pinned consumers even if an operator forgot to
            # increment the explicit policy revision. No credential values enter it.
            revision = int(hashlib.sha256(json.dumps(policy, sort_keys=True).encode()).hexdigest(), 16) + 1
            return record['revision'], revision, rows

    def resource_grants(self, project_id, identity):
        _, revision, rows = self._rows(project_id, identity)
        return {'resource_revision': revision, 'resources': rows}

    def authorize_resource(self, project_id, identity, request):
        acl, revision, rows = self._rows(project_id, identity)
        for row in rows:
            if (row['resource_id'], row['action']) != (request.resource_id, request.action):
                continue
            if row['kind'] == 'workspace':
                if request.path is None or not Path(request.path).resolve().is_relative_to(Path(row['scope'])):
                    break
            elif request.path is not None:
                break
            return ResourceDecision(True, project_id, identity.actor_id, identity.subject_id,
                                    request, acl, revision, 'private_session', row['reference'], row['scope'])
        raise ResourceAccessDenied('private Session resource denied')
