"""Read-only published context for the exact authenticated Native execution.

This extends the existing resource authority, not the workspace or shell grant.
The resolver still proves the original ReadFileTool and owned Native Session.
"""
from pathlib import Path

from .personal_context import personal_context_home
from .resources import ResourceAccessDenied, ResourceDecision, ResourceRequest
from .tool_resources import ToolResourceUse

_PAGES = 'host-personal-context-pages'
_READER = 'host-personal-context-reader'
_REFERENCE = 'native:personal-context-read'


class PersonalContextResources:
    def __init__(self, base, identity, *, is_current):
        self.base = base
        self.identity = identity
        self.is_current = is_current

    def resource_grants(self, project_id, identity):
        grants = self.base.resource_grants(project_id, identity)
        if any(row['resource_id'] in {_PAGES, _READER} for row in grants['resources']):
            raise ResourceAccessDenied('reserved context resource collision')
        return grants

    def _root(self, identity):
        from .organization_auth import configured_authenticator
        # Reuse the Rail's bounded, symlink-safe interpretation of its live switch.
        from openjiuwen.harness.rails.personal_context import _agent_use_enabled
        auth = configured_authenticator()
        if (identity != self.identity or self.is_current() is not True
                or auth is None or not auth.known_actor(identity)):
            raise ResourceAccessDenied('personal context execution unavailable')
        home = personal_context_home(identity)
        if not _agent_use_enabled(home / 'personal_context.yaml'):
            raise ResourceAccessDenied('personal context Agent use disabled')
        root = home / 'workspace' / 'context'
        if any(path.is_symlink() for path in (root, *root.parents)) or not root.is_dir():
            raise ResourceAccessDenied('published personal context unavailable')
        return root

    @staticmethod
    def _page(root, path):
        candidate = Path(path)
        if (not candidate.is_absolute() or not candidate.is_relative_to(root)
                or any(p.is_symlink() for p in (candidate, *candidate.parents))
                or not candidate.resolve().is_relative_to(root)
                or candidate.suffix != '.md' or not candidate.is_file()):
            raise ResourceAccessDenied('published personal context page required')

    def read_uses(self, execution, path):
        root = self._root(execution.identity)
        self._page(root, path)
        return (
            ToolResourceUse(ResourceRequest(_READER, 'invoke'), _REFERENCE),
            ToolResourceUse(ResourceRequest(_PAGES, 'read', path), str(root)),
        )

    def authorize_resource(self, project_id, identity, request):
        if request.resource_id not in {_PAGES, _READER}:
            return self.base.authorize_resource(project_id, identity, request)
        # Base lookup preserves current project/private ownership and revision.
        grants = self.resource_grants(project_id, identity)
        root = self._root(identity)
        if request.resource_id == _PAGES:
            if request.action != 'read' or request.path is None:
                raise ResourceAccessDenied('personal context is read-only')
            self._page(root, request.path)
            reference, scope = str(root), str(root)
        else:
            if request.action != 'invoke' or request.path is not None:
                raise ResourceAccessDenied('invalid personal context reader')
            reference, scope = _REFERENCE, None
        return ResourceDecision(True, project_id, identity.actor_id, identity.subject_id,
                                request, 1, grants['resource_revision'],
                                'owned_personal_context', reference, scope)
