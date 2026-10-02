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
from contextlib import contextmanager
from typing import Any, Iterator, Mapping

from jiuwenswarm.common.work_mode import is_default_project_id
from jiuwenswarm.governance.contracts import AuthorizationDecision, ProjectAction
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
        if record is None:
            return result(False, reason='project_unavailable')
        ext = data['projects'].get(project_id)
        if ext is None:
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
