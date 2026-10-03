"""Persistent project extensions and ACL authority, independent of subscriptions.

Legacy registry fields retain their original owner. This versioned sidecar holds
only new fields. Every decision reloads disk; corrupt storage fails closed.
Host migration supplies an explicit owner map, never guesses from route user_id.
"""
from __future__ import annotations

import copy
import json
import os
import threading
import time
from pathlib import Path
from contextlib import contextmanager
from typing import Any, Iterator, Mapping

from jiuwenswarm.common.work_mode import is_default_project_id
from jiuwenswarm.governance.contracts import AuthorizationDecision, ProjectAction, TrustedIdentity
from jiuwenswarm.governance.resources import (
    ResourceAccessDenied, ResourceDefinition, ResourceDecision, ResourceRequest,
    _ACTIONS as RESOURCE_ACTIONS, normalized, valid_expiry,
)
from jiuwenswarm.server.runtime.session import project_store

_ACTIONS = frozenset({'read', 'write', 'execute', 'admin'})
_SCHEMA = 1
_HELD_LOCKS = threading.local()


class ProjectAccessDenied(PermissionError):
    """Generic denial deliberately excludes project contents and memberships."""


class ProjectRevisionConflict(ValueError):
    pass


class ProjectAccessStore:
    """Disk-backed authorizer using the current AgentServer's injected data root."""

    @property
    def path(self):
        return project_store._projects_file().with_name('project_extensions.json')

    @contextmanager
    def _locked(self):
        # Synchronous adapter guards may call nested read helpers. Reentrancy
        # is thread-local; another process/thread still acquires the OS lock.
        held = getattr(_HELD_LOCKS, 'paths', set())
        key = str(self.path)
        if key in held:
            yield
            return
        with project_store.file_lock(self.path):
            _HELD_LOCKS.paths = held | {key}
            try:
                yield
            finally:
                _HELD_LOCKS.paths = held

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {'schema_version': _SCHEMA, 'projects': {}}
        try:
            data = json.loads(self.path.read_text(encoding='utf-8'))
            if data.get('schema_version') != _SCHEMA or not isinstance(data.get('projects'), dict):
                raise ValueError('invalid extension schema')
            return data
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            raise ProjectAccessDenied('project authorization storage unavailable') from exc

    def _save(self, data: dict[str, Any]) -> None:
        tmp = self.path.with_suffix('.json.tmp')
        with tmp.open('w', encoding='utf-8') as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, self.path)
        project_store._fsync_dir(self.path.parent)

    @staticmethod
    def _record(project_id: str) -> dict[str, Any] | None:
        # Registry writes are atomic replacements. Do not invert the sidecar /
        # registry lock order when this is called from an authorization guard.
        path = project_store._projects_file()
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
            return next((p for p in data['projects'] if p.get('project_id') == project_id), None)
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            raise ProjectAccessDenied('project registry unavailable') from exc

    def _decision(self, data, project_id, actor_id, action) -> AuthorizationDecision:
        def result(allowed, revision=0, reason=''):
            return AuthorizationDecision(allowed, project_id, actor_id, action, revision, reason)
        if action not in _ACTIONS:
            return result(False, reason='unknown_action')
        if not project_id or is_default_project_id(project_id):
            return result(True, reason='legacy_default')
        record = self._record(project_id)
        ext = data['projects'].get(project_id)
        if ext is None:
            if record is None:
                return result(False, reason='project_unavailable')
            return result(not record.get('access_managed', False), reason='legacy_unmanaged' if not record.get('access_managed') else 'extension_missing')
        if not isinstance(ext, dict) or ext.get('schema_version') != _SCHEMA:
            return result(False, reason='extension_invalid')
        revision = ext.get('acl_revision', 0)
        if type(revision) is not int or revision < 1:
            return result(False, reason='extension_invalid')
        owner = ext.get('owner_id')
        if not isinstance(owner, str) or not owner:
            return result(False, revision, 'owner_unknown')
        if not actor_id:
            return result(False, revision, 'identity_required')
        if record is None:
            # Deleting a registry entry does not publish its remaining history.
            # Only the retained owner may read/clean it; stale member grants
            # cannot survive deletion and no actor may execute a missing project.
            allowed = actor_id == owner and action in {'read', 'write', 'admin'}
            return result(allowed, revision, 'orphan_owner' if allowed else 'project_unavailable')
        if actor_id == owner:
            return result(True, revision, 'owner')
        acl = ext.get('acl', {})
        grants = acl.get(actor_id, []) if isinstance(acl, dict) else []
        # Each action is explicit. In particular read/admin never imply execute.
        allowed = isinstance(grants, list) and action in grants
        return result(allowed, revision, 'grant' if allowed else 'permission_denied')

    def authorize(self, project_id: str, actor_id: str, action: ProjectAction) -> AuthorizationDecision:
        try:
            with self._locked():
                return self._decision(self._load(), project_id, actor_id, action)
        except (ProjectAccessDenied, OSError):
            return AuthorizationDecision(False, project_id, actor_id, action, 0, 'storage_unavailable')

    def protected_ids(self) -> tuple[str, ...]:
        """Enumerate persisted protection, including deleted registry IDs.

        Unscoped legacy inventories must consider orphaned Session histories,
        not only projects still present in the registry. Storage corruption
        raises instead of returning an empty (apparently unprotected) set.
        """
        with self._locked():
            data = self._load()
            ids = set(data['projects'])
            path = project_store._projects_file()
            if path.exists():
                try:
                    registry = json.loads(path.read_text(encoding='utf-8'))
                    records = registry['projects']
                    if not isinstance(records, list) or any(not isinstance(p, dict) for p in records):
                        raise ValueError('invalid project registry')
                    ids.update(p.get('project_id') for p in records if p.get('access_managed'))
                except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
                    raise ProjectAccessDenied('project registry unavailable') from exc
            if any(not isinstance(pid, str) or not pid.strip() for pid in ids):
                raise ProjectAccessDenied('invalid protected project identity')
            return tuple(sorted(pid for pid in ids if not is_default_project_id(pid)))

    def is_protected(self, project_id: str) -> bool:
        """Persisted ACL or a creation marker; corruption is also protected."""
        if not project_id or is_default_project_id(project_id):
            return False
        try:
            with self._locked():
                record = self._record(project_id)
                return project_id in self._load()['projects'] or bool(record and record.get('access_managed'))
        except (ProjectAccessDenied, OSError):
            return True

    @contextmanager
    def guard(self, project_id: str, actor_id: str, action: ProjectAction) -> Iterator[AuthorizationDecision]:
        """Recheck and keep ACL revisions stable through one synchronous API IO."""
        with self._locked():
            decision = self._decision(self._load(), project_id, actor_id, action)
            if not decision.allowed:
                raise ProjectAccessDenied(f'project {action} permission required')
            yield decision

    @staticmethod
    def _mark_managed(project_ids: set[str]) -> None:
        missing = {pid for pid in project_ids
                   if not (ProjectAccessStore._record(pid) or {}).get('access_managed')}
        if not missing:
            return
        def mark(records):
            for record in records:
                if record.get('project_id') in missing:
                    record['access_managed'] = True
        project_store._mutate(mark)

    def migrate_legacy(self, owners: Mapping[str, str | None] | None = None) -> int:
        """Host-only idempotent migration. Unknown owners remain inaccessible.

        Existing records, grants and revisions are never changed by migration.
        This is deliberately not an end-user claim-ownership operation.
        """
        owners = owners or {}
        with self._locked():
            data = self._load()
            count = 0
            for project in project_store.list_projects(cache_bust=True):
                if project.project_id in data['projects']:
                    continue
                owner = owners.get(project.project_id)
                if owner is not None and (not isinstance(owner, str) or not owner.strip() or owner != owner.strip()):
                    raise ValueError('owner_id must be normalized')
                data['projects'][project.project_id] = {
                    'schema_version': _SCHEMA, 'owner_id': owner,
                    'acl_revision': 1, 'acl': {}, 'goal': '', 'extensions': {},
                }
                count += 1
            self._mark_managed(set(data["projects"]))
            if count:
                self._save(data)
            return count

    def initialize(self, project_id: str, owner_id: str) -> None:
        """Host-only creation hook; access_managed marker closes crash windows."""
        if not isinstance(owner_id, str) or not owner_id.strip() or owner_id != owner_id.strip():
            raise ValueError('owner_id must be normalized')
        with self._locked():
            data = self._load()
            if project_id in data['projects']:
                raise ProjectRevisionConflict('project extension already exists')
            if self._record(project_id) is None:
                raise ValueError('project not found')
            self._mark_managed({project_id})
            data['projects'][project_id] = {
                'schema_version': _SCHEMA, 'owner_id': owner_id, 'acl_revision': 1,
                'acl': {}, 'goal': '', 'extensions': {},
            }
            self._mark_managed({project_id})
            self._save(data)

    def get(self, project_id: str, actor_id: str) -> dict[str, Any] | None:
        with self.guard(project_id, actor_id, 'read'):
            record = self._load()['projects'].get(project_id)
            if record is None:
                return None
            # Member lists are admin-only even when contents are readable.
            result = copy.deepcopy(record)
            # Resource references have independent visibility; even project read
            # and admin do not authorize disclosing another subject's credentials.
            result.pop('resource_access', None)
            if not self._decision(self._load(), project_id, actor_id, 'admin').allowed:
                result.pop('acl', None)
            return result

    def update(self, project_id: str, actor_id: str, *, goal: str, extensions: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(goal, str) or not isinstance(extensions, dict):
            raise ValueError('goal must be a string and extensions an object')
        # Reject non-JSON data before changing the stored record.
        clean = json.loads(json.dumps(extensions, allow_nan=False))
        with self.guard(project_id, actor_id, 'write'):
            data = self._load()
            record = data['projects'].get(project_id)
            if record is None:
                raise ValueError('project extensions require host migration')
            record['goal'] = goal
            record['extensions'] = clean
            self._save(data)
            return {'goal': goal, 'extensions': copy.deepcopy(clean)}

    def replace_acl(self, project_id: str, actor_id: str, *, acl: dict[str, list[str]], expected_revision: int) -> int:
        if type(expected_revision) is not int or not isinstance(acl, dict):
            raise ValueError('acl and expected_revision are required')
        for member, actions in acl.items():
            if not isinstance(member, str) or not member.strip() or member != member.strip() or not isinstance(actions, list) or any(not isinstance(action, str) or action not in _ACTIONS for action in actions):
                raise ValueError('invalid ACL member or action')
        with self.guard(project_id, actor_id, 'admin'):
            data = self._load()
            record = data['projects'].get(project_id)
            if record is None:
                raise ValueError('project ACL requires host migration')
            if record['acl_revision'] != expected_revision:
                raise ProjectRevisionConflict('project ACL revision changed')
            record['acl'] = copy.deepcopy(acl)
            record['acl_revision'] += 1
            self._save(data)
            return record['acl_revision']

    @staticmethod
    def _resource_state(record: dict) -> dict:
        state = record.get('resource_access')
        if state is None:
            return {'schema_version': 1, 'revision': 0, 'catalog': {}, 'grants': {}}
        if (not isinstance(state, dict) or state.get('schema_version') != 1
                or type(state.get('revision')) is not int or state['revision'] < 1
                or not isinstance(state.get('catalog'), dict) or not isinstance(state.get('grants'), dict)):
            raise ResourceAccessDenied('resource authorization storage unavailable')
        return state

    def _resource_record(self, data: dict, project_id: str) -> dict:
        record = data['projects'].get(project_id)
        if (self._record(project_id) is None or not isinstance(record, dict)
                or record.get('schema_version') != _SCHEMA or not record.get('owner_id')):
            raise ResourceAccessDenied('managed project required for resource authorization')
        return record

    @staticmethod
    def _resource_definition(state: dict, resource_id: str) -> ResourceDefinition:
        value = state['catalog'].get(resource_id)
        if not isinstance(value, dict):
            raise ResourceAccessDenied('resource unavailable')
        return ResourceDefinition(resource_id, value['kind'], value['reference'])

    @staticmethod
    def _scope(definition: ResourceDefinition, scope: str | None) -> str | None:
        if definition.kind != 'workspace':
            if scope is not None:
                raise ValueError('only workspace resources accept path scopes')
            return None
        scope = definition.reference if scope is None else scope
        if not isinstance(scope, str) or not Path(scope).is_absolute():
            raise ValueError('workspace grant scope must be absolute')
        resolved = Path(scope).resolve()
        if not resolved.is_relative_to(Path(definition.reference)):
            raise ResourceAccessDenied('workspace grant exceeds resource scope')
        return str(resolved)

    @staticmethod
    def _within(child: str | None, parent: str | None) -> bool:
        return child is None and parent is None or (
            child is not None and parent is not None and Path(child).is_relative_to(Path(parent))
        )

    def _active_resource_grant(self, state: dict, definition: ResourceDefinition, subject_id: str, now: float) -> dict:
        grants = state['grants'].get(definition.resource_id, {})
        if not isinstance(grants, dict):
            raise ResourceAccessDenied('invalid resource grants')
        seen = set()
        child = None
        selected = None
        # Delegations are a bounded parent chain, never an unbounded authority graph.
        for _ in range(16):
            if subject_id in seen:
                raise ResourceAccessDenied('cyclic resource delegation')
            seen.add(subject_id)
            grant = grants.get(subject_id)
            if not isinstance(grant, dict):
                raise ResourceAccessDenied('resource grant missing')
            actions = grant.get('actions')
            if (not isinstance(actions, list) or not actions
                    or any(not isinstance(action, str) or action not in RESOURCE_ACTIONS[definition.kind] for action in actions)
                    or type(grant.get('delegable')) is not bool
                    or type(grant.get('revision')) is not int or not 1 <= grant['revision'] <= state['revision']
                    or not valid_expiry(grant.get('expires_at'))):
                raise ResourceAccessDenied('invalid resource grant')
            scope = self._scope(definition, grant.get('scope'))
            if scope != grant.get('scope'):
                raise ResourceAccessDenied('resource scope changed')
            expiry = grant.get('expires_at')
            if expiry is not None and expiry <= now:
                raise ResourceAccessDenied('resource grant expired')
            if child is not None:
                if (type(child.get('parent_revision')) is not int or child.get('parent_revision') != grant['revision'] or not grant['delegable']
                        or not set(child['actions']) <= set(actions)
                        or not self._within(child['scope'], scope)
                        or expiry is not None and (child['expires_at'] is None or child['expires_at'] > expiry)):
                    raise ResourceAccessDenied('resource delegation no longer valid')
            if selected is None:
                selected = grant
            parent = grant.get('parent')
            if parent is None:
                if (subject_id != state['catalog'][definition.resource_id].get('owner_subject_id')
                        or grant.get('parent_revision') is not None):
                    raise ResourceAccessDenied('resource root grant invalid')
                return selected
            normalized(parent, 'parent')
            child, subject_id = grant, parent
        raise ResourceAccessDenied('resource delegation depth exceeded')

    @staticmethod
    def _resource_actions(definition: ResourceDefinition, actions) -> list[str]:
        if not isinstance(actions, (list, tuple)) or not actions or any(
            not isinstance(action, str) or action not in RESOURCE_ACTIONS[definition.kind] for action in actions
        ):
            raise ValueError('invalid resource actions')
        return sorted(set(actions))

    @staticmethod
    def _resource_revision(state: dict, expected_revision: int) -> None:
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError('expected resource revision is required')
        if state['revision'] != expected_revision:
            raise ProjectRevisionConflict('resource authorization revision changed')

    def register_resource(self, project_id: str, definition: ResourceDefinition, *, owner_subject_id: str,
                          actions: tuple[str, ...], expected_revision: int,
                          delegable: bool = False, expires_at: float | None = None) -> int:
        """Host-only resource provisioning; never bind this to caller-owned wire data.

        Project ownership cannot manufacture resource ownership. The host confirms
        the resource and its explicit owner before calling this entry point. It
        may reissue that same root grant after revocation; children stay invalid.
        """
        if not isinstance(definition, ResourceDefinition):
            raise ValueError('resource definition required')
        normalized(owner_subject_id, 'owner_subject_id')
        actions = self._resource_actions(definition, actions)
        if type(delegable) is not bool or not valid_expiry(expires_at) or expires_at is not None and expires_at <= time.time():
            raise ValueError('invalid resource lifetime or delegation')
        with self._locked():
            data = self._load()
            record = self._resource_record(data, project_id)
            state = self._resource_state(record)
            self._resource_revision(state, expected_revision)
            definition_data = {'kind': definition.kind, 'reference': definition.reference, 'owner_subject_id': owner_subject_id}
            prior = state['catalog'].get(definition.resource_id)
            if prior is not None and prior != definition_data:
                raise ProjectRevisionConflict('resource definition or owner cannot be replaced')
            state['revision'] += 1
            state['catalog'][definition.resource_id] = definition_data
            state['grants'].setdefault(definition.resource_id, {})[owner_subject_id] = {
                'actions': actions, 'scope': self._scope(definition, None),
                'delegable': delegable, 'expires_at': expires_at,
                'parent': None, 'parent_revision': None, 'revision': state['revision'],
            }
            record['resource_access'] = state
            self._save(data)
            return state['revision']

    def grant_resource(self, project_id: str, identity: TrustedIdentity, resource_id: str, *,
                       subject_id: str, actions: tuple[str, ...], expected_revision: int,
                       scope: str | None = None, delegable: bool = False,
                       expires_at: float | None = None) -> int:
        if not isinstance(identity, TrustedIdentity) or identity.actor_id != identity.subject_id:
            raise ResourceAccessDenied('resource delegation requires the authenticated subject')
        normalized(subject_id, 'subject_id')
        if subject_id == identity.actor_id:
            raise ResourceAccessDenied('self delegation is not allowed')
        if type(delegable) is not bool or not valid_expiry(expires_at) or expires_at is not None and expires_at <= time.time():
            raise ValueError('invalid resource lifetime or delegation')
        with self.guard(project_id, identity.actor_id, 'execute'):
            data = self._load()
            record = self._resource_record(data, project_id)
            state = self._resource_state(record)
            self._resource_revision(state, expected_revision)
            definition = self._resource_definition(state, resource_id)
            source = self._active_resource_grant(state, definition, identity.actor_id, time.time())
            actions = self._resource_actions(definition, actions)
            scope = self._scope(definition, source['scope'] if scope is None else scope)
            # Omitted expiry inherits the granting subject's upper lifetime bound.
            expires_at = source['expires_at'] if expires_at is None else expires_at
            if (not source['delegable'] or not set(actions) <= set(source['actions'])
                    or not self._within(scope, source['scope'])
                    or source['expires_at'] is not None and (expires_at is None or expires_at > source['expires_at'])):
                raise ResourceAccessDenied('resource grant exceeds delegable authority')
            # Prevent replacing an ancestor (including the host root) with a child.
            cursor = identity.actor_id
            grants = state['grants'][resource_id]
            for _ in range(16):
                if cursor == subject_id:
                    raise ResourceAccessDenied('cyclic resource delegation')
                cursor = grants[cursor].get('parent')
                if cursor is None:
                    break
            state['revision'] += 1
            grants[subject_id] = {
                'actions': actions, 'scope': scope, 'delegable': delegable,
                'expires_at': expires_at, 'parent': identity.actor_id,
                'parent_revision': source['revision'], 'revision': state['revision'],
            }
            self._active_resource_grant(state, definition, subject_id, time.time())
            self._save(data)
            return state['revision']

    def revoke_resource(self, project_id: str, identity: TrustedIdentity, resource_id: str, *,
                        subject_id: str, expected_revision: int) -> int:
        if not isinstance(identity, TrustedIdentity):
            raise ResourceAccessDenied('trusted resource identity required')
        normalized(subject_id, 'subject_id')
        with self._locked():
            data = self._load()
            record = self._resource_record(data, project_id)
            state = self._resource_state(record)
            self._resource_revision(state, expected_revision)
            self._resource_definition(state, resource_id)
            grants = state['grants'].get(resource_id, {})
            grant = grants.get(subject_id)
            if not isinstance(grant, dict):
                raise ResourceAccessDenied('resource grant unavailable')
            # Project admins can remove grants, but this does not let them create one.
            if (identity.actor_id not in {subject_id, grant.get('parent')}
                    and not self._decision(data, project_id, identity.actor_id, 'admin').allowed):
                raise ResourceAccessDenied('resource revocation denied')
            del grants[subject_id]
            state['revision'] += 1
            self._save(data)
            return state['revision']

    def authorize_resource(self, project_id: str, identity: TrustedIdentity, request: ResourceRequest) -> ResourceDecision:
        actor_id = identity.actor_id if isinstance(identity, TrustedIdentity) else ''
        subject_id = identity.subject_id if isinstance(identity, TrustedIdentity) else ''
        acl_revision = resource_revision = 0
        try:
            if not actor_id or not isinstance(request, ResourceRequest):
                raise ResourceAccessDenied('trusted identity and resource request required')
            with self._locked():
                data = self._load()
                decision = self._decision(data, project_id, actor_id, 'execute')
                acl_revision = decision.revision
                if not decision.allowed:
                    raise ResourceAccessDenied('project execution denied')
                record = self._resource_record(data, project_id)
                state = self._resource_state(record)
                resource_revision = state['revision']
                definition = self._resource_definition(state, request.resource_id)
                if request.action not in RESOURCE_ACTIONS[definition.kind]:
                    raise ResourceAccessDenied('resource action unavailable')
                scopes, expiries = [], []
                for subject in {actor_id, subject_id}:
                    grant = self._active_resource_grant(state, definition, subject, time.time())
                    if request.action not in grant['actions']:
                        raise ResourceAccessDenied('resource operation denied')
                    if definition.kind == 'workspace':
                        if request.path is None or not self._within(str(Path(request.path).resolve()), grant['scope']):
                            raise ResourceAccessDenied('resource path outside scope')
                        scopes.append(grant['scope'])
                    elif request.path is not None:
                        raise ResourceAccessDenied('path scope is not a process sandbox')
                    if grant['expires_at'] is not None:
                        expiries.append(grant['expires_at'])
                return ResourceDecision(True, project_id, actor_id, subject_id, request, acl_revision,
                                        resource_revision, 'grant', definition.reference,
                                        max(scopes, key=len) if scopes else None,
                                        min(expiries) if expiries else None)
        except (ProjectAccessDenied, ResourceAccessDenied, OSError, ValueError, TypeError, KeyError, AttributeError):
            return ResourceDecision(False, project_id, actor_id, subject_id, request,
                                    acl_revision, resource_revision, 'resource_permission_denied')

    @contextmanager
    def guard_resource(self, project_id: str, identity: TrustedIdentity, request: ResourceRequest) -> Iterator[ResourceDecision]:
        """Hold the existing sidecar lock across a short synchronous resource IO.

        Hosts must still use safe file primitives for path/symlink TOCTOU. This
        lock is not a shell sandbox and must not be held across an async process.
        """
        with self._locked():
            decision = self.authorize_resource(project_id, identity, request)
            if not decision.allowed:
                raise ResourceAccessDenied('resource operation denied')
            yield decision

    def resource_grants(self, project_id: str, identity: TrustedIdentity) -> dict:
        """Return only the caller's currently usable reference metadata."""
        if not isinstance(identity, TrustedIdentity):
            raise ResourceAccessDenied('trusted resource identity required')
        with self.guard(project_id, identity.actor_id, 'execute'):
            record = self._resource_record(self._load(), project_id)
            state = self._resource_state(record)
            visible = []
            for resource_id in state['catalog']:
                try:
                    definition = self._resource_definition(state, resource_id)
                    grant = self._active_resource_grant(state, definition, identity.subject_id, time.time())
                    for action in grant['actions']:
                        request = ResourceRequest(resource_id, action, grant['scope'])
                        decision = self.authorize_resource(project_id, identity, request)
                        if decision.allowed:
                            visible.append({'resource_id': resource_id, 'kind': definition.kind,
                                            'reference': decision.reference, 'action': action,
                                            'scope': decision.scope, 'expires_at': decision.expires_at})
                except (ResourceAccessDenied, ValueError, TypeError, KeyError):
                    continue
            return {'resource_revision': state['revision'], 'resources': visible}
