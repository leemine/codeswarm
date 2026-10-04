"""Host-only owner/source epochs for persistent Session sharing.

All source facts live in the existing project sidecar, alongside the owner.
The store invokes its resolver under that sidecar lock with an older data
snapshot: the resolver MUST remain read-only and never acquire history locks,
drain writes, await, or save. History preparation is an explicit separate host
operation. Runtime owns prepublish registration and invalidation before every
rebind/delete; metadata user_id and routing fields are never owner evidence.
"""
from __future__ import annotations

import copy
from contextlib import contextmanager
from dataclasses import asdict
from typing import Callable

from jiuwenswarm.governance.contracts import TrustedIdentity
from jiuwenswarm.governance.session_sharing import (
    SessionHistoryRange, SessionSharingAuthority, SessionSharingConflict, SessionSharingDenied,
)
from . import lifecycle
from .project_access import ProjectAccessStore
from .session_sharing import SessionSharingStore
from .shared_history import compile_shared_history_range

_MAX_COMPONENT = (1 << 64) - 1


def _component(value, *, positive=False):
    if type(value) is not int or not int(positive) <= value <= _MAX_COMPONENT:
        raise SessionSharingDenied('invalid sharing epoch component')
    return value


def _revision(epoch, acl, session_generation, project_generation):
    return (_component(epoch, positive=True) << 192 | _component(acl, positive=True) << 128
            | _component(session_generation) << 64 | _component(project_generation))


class SharingHostService:
    """Synchronous host ports for SessionSharingAdapter and guarded IO.

    ``directory`` resolves eligible invitation targets. ``known_actor`` validates
    a persistent known subject in the current authority, independently of token
    logout, expiry or revocation. Source ownership never depends on an active
    credential. Request authentication remains the host's separate boundary.
    Both callbacks must avoid network IO and history/lifecycle locks. Source actions are view/read, manage/read+admin, and execute only when the
    owner also has current project execute. Target execution and resource use
    remain independent checks; other actions are never inferred.
    """
    def __init__(self, directory: Callable[[TrustedIdentity, str], TrustedIdentity | None], *,
                 known_actor: Callable[[TrustedIdentity], bool], storage=None):
        self._directory = directory
        self._known_actor = known_actor
        self._storage = storage if storage is not None else ProjectAccessStore()
        self.store = SessionSharingStore(self.resolve_source, storage=self._storage)

    def target_resolver(self, requestor: TrustedIdentity, actor_id: str) -> TrustedIdentity | None:
        try:
            if not isinstance(requestor, TrustedIdentity) or self._known_actor(requestor) is not True:
                return None
            target = self._directory(requestor, actor_id)
            return target if (isinstance(target, TrustedIdentity) and target.actor_id == actor_id
                              and target.authority == requestor.authority) else None
        except Exception:
            return None

    def _record(self, data, session_id, *, active=True):
        self.store._session(session_id)
        record = self.store._section(data)['owners'].get(session_id)
        if not isinstance(record, dict) or record.get('retired') is not False:
            raise SessionSharingDenied('trusted Session owner unavailable')
        _component(record.get('revision'), positive=True)
        try:
            owner = TrustedIdentity(**record['identity'])
        except (KeyError, TypeError, ValueError) as exc:
            raise SessionSharingDenied('trusted Session owner unavailable') from exc
        source = record.get('source')
        if not isinstance(source, dict) or type(source.get('schema_version')) is not int or source['schema_version'] != 1:
            raise SessionSharingDenied('Session source requires host registration')
        _component(source.get('epoch'), positive=True)
        if type(source.get('active')) is not bool or (active and source['active'] is not True):
            raise SessionSharingDenied('Session source is invalidated')
        project_id = source.get('project_id')
        if not isinstance(project_id, str) or not project_id or project_id.strip() != project_id:
            raise SessionSharingDenied('explicit managed project required')
        _component(source.get('session_generation'))
        _component(source.get('project_generation'))
        return record, owner, source

    def _project(self, data, project_id, owner):
        # Default/unmanaged/orphan read compatibility is never sharing authority.
        ext = data['projects'].get(project_id)
        if (not isinstance(ext, dict) or ext.get('schema_version') != 1
                or not isinstance(ext.get('owner_id'), str) or not ext['owner_id']
                or self._storage._record(project_id) is None):
            raise SessionSharingDenied('managed project unavailable')
        acl_revision = _component(ext.get('acl_revision'), positive=True)
        read = self._storage._decision(data, project_id, owner.actor_id, 'read')
        if (read.allowed is not True or read.project_id != project_id or read.actor_id != owner.actor_id
                or read.action != 'read' or type(read.revision) is not int or read.revision != acl_revision
                or read.reason in {'legacy_default', 'legacy_unmanaged', 'orphan_owner'}):
            raise SessionSharingDenied('current owner project read denied')
        actions = {'view'}
        admin = self._storage._decision(data, project_id, owner.actor_id, 'admin')
        if (admin.allowed is True and admin.project_id == project_id and admin.actor_id == owner.actor_id
                and admin.action == 'admin' and type(admin.revision) is int and admin.revision == acl_revision):
            actions.add('manage')
            execute = self._storage._decision(data, project_id, owner.actor_id, 'execute')
            if (execute.allowed is True and execute.project_id == project_id
                    and execute.actor_id == owner.actor_id and execute.action == 'execute'
                    and type(execute.revision) is int and execute.revision == acl_revision):
                actions.add('execute')
        return frozenset(actions), acl_revision

    def _live_binding(self, data, session_id, owner, project_id):
        if self._known_actor(owner) is not True:
            raise SessionSharingDenied('owner no longer belongs to the live authority')
        # These existing files are atomically published. Read without acquiring
        # a lifecycle or history lock while the sidecar lock is held.
        metadata = lifecycle.raw_metadata(session_id)
        if not metadata or metadata.get('project_id') != project_id:
            raise SessionSharingDenied('Session project binding changed')
        lifecycle.guard(session_id, project_id)
        session_state, project_state = lifecycle.state('session', session_id), lifecycle.state('project', project_id)
        if session_state.get('deleted') or project_state.get('deleted'):
            raise SessionSharingDenied('Session source was deleted')
        session_generation = _component(session_state.get('generation', 0))
        project_generation = _component(project_state.get('generation', 0))
        actions, acl_revision = self._project(data, project_id, owner)
        return actions, acl_revision, session_generation, project_generation

    def _current(self, data, session_id):
        record, owner, source = self._record(data, session_id)
        facts = self._live_binding(data, session_id, owner, source['project_id'])
        if facts[2:] != (source['session_generation'], source['project_generation']):
            raise SessionSharingDenied('Session lifecycle generation changed')
        return record, owner, source, facts

    def resolve_source(self, session_id: str) -> SessionSharingAuthority | None:
        """Read-only even when called under SessionSharingStore's write guard."""
        try:
            with self._storage._locked():
                _, owner, source, facts = self._current(self._storage._load(), session_id)
                scope = SessionHistoryRange(**source['history'])
                if scope.session_id != session_id:
                    return None
                return SessionSharingAuthority(session_id, owner, _revision(source['epoch'], *facts[1:]),
                                               facts[0], scope)
        except Exception:
            return None

    def owner_current(self, session_id: str, identity: TrustedIdentity, action: str = 'view') -> bool:
        """Current owner ACL without requiring a history file to exist yet."""
        try:
            with self._storage._locked():
                _, owner, _, facts = self._current(self._storage._load(), session_id)
                return isinstance(identity, TrustedIdentity) and identity == owner and action in facts[0]
        except Exception:
            return False

    def owner_revision(self, session_id: str, identity: TrustedIdentity) -> int:
        """Pin current owner authority, including ACL/lifecycle changes.

        Unlike ``resolve_source``, this does not require or inspect a history
        file/range. A host may use it immediately after metadata publication.
        It never registers unknown owners or refreshes persistent source state.
        Errors deny admission; callers must not substitute a default revision.
        """
        with self._storage._locked():
            _, owner, source, facts = self._current(self._storage._load(), session_id)
            if not isinstance(identity, TrustedIdentity) or identity != owner or 'view' not in facts[0]:
                raise SessionSharingDenied('current Session owner required')
            return _revision(source['epoch'], *facts[1:])

    def register_owner_and_source(self, session_id: str, owner: TrustedIdentity, project_id: str, *,
                                  expected_owner_revision: int = 0) -> int:
        """Host-only prepublish registration; one sidecar save and no history IO.

        Session metadata need not exist yet. The first prepared source cannot
        become visible until its explicit project matches persisted metadata.
        A missing JSONL starts as history=None, never a fabricated inode.
        """
        self.store._session(session_id)
        self.store._identity(owner)
        with self._storage._locked():
            data = self._storage._load()
            owners = self.store._section(data)['owners']
            old = owners.get(session_id)
            if old is not None and (not isinstance(old, dict) or old.get('retired') is not True):
                raise SessionSharingConflict('Session owner already registered')
            previous = _component(old['revision'], positive=True) if old else 0
            if type(expected_owner_revision) is not int or expected_owner_revision != previous:
                raise SessionSharingConflict('owner revision changed')
            if self._known_actor(owner) is not True:
                raise SessionSharingDenied('trusted known owner required')
            self._project(data, project_id, owner)
            lifecycle.guard(project_id=project_id)
            session_state, project_state = lifecycle.state('session', session_id), lifecycle.state('project', project_id)
            # Creation after deletion requires the lifecycle owner to finish its
            # recreation first. Sharing must not reopen tombstoned resources.
            if session_state.get('deleted') or session_state.get('blocked'):
                raise SessionSharingDenied('Session lifecycle blocks registration')
            prior_epoch = _component(old.get('source', {}).get('epoch', previous), positive=True) if old else 0
            epoch = _component(prior_epoch + 1, positive=True)
            owners[session_id] = {'identity': asdict(owner), 'revision': _component(previous + 1, positive=True),
                'retired': False, 'source': {'schema_version': 1, 'epoch': epoch, 'active': True,
                'project_id': project_id, 'session_generation': _component(session_state.get('generation', 0)),
                'project_generation': _component(project_state.get('generation', 0)), 'history': None}}
            self._storage._save(data)
            return epoch

    def compensate_owner_registration(
        self, session_id: str, owner: TrustedIdentity, *,
        expected_revision: int, expected_epoch: int,
    ) -> int:
        """Atomically tombstone exactly one failed publication reservation.

        The receipt is only the trusted identity and two persisted CAS values.
        A completed receipt is idempotent; a newer reservation, even by the same
        actor, is never modified. Session files remain for controlled recovery.
        """
        self.store._session(session_id)
        identity = self.store._identity(owner)
        expected_revision = _component(expected_revision, positive=True)
        expected_epoch = _component(expected_epoch, positive=True)
        retired_revision = _component(expected_revision + 1, positive=True)
        retired_epoch = _component(expected_epoch + 1, positive=True)
        with self._storage._locked():
            data = self._storage._load()
            record = self.store._section(data)['owners'].get(session_id)
            if not isinstance(record, dict) or record.get('identity') != identity:
                raise SessionSharingConflict('owner reservation changed before compensation')
            source = record.get('source')
            if not isinstance(source, dict) or type(source.get('schema_version')) is not int or source['schema_version'] != 1:
                raise SessionSharingDenied('owner reservation source unavailable')
            revision = _component(record.get('revision'), positive=True)
            epoch = _component(source.get('epoch'), positive=True)
            if (record.get('retired') is True and revision == retired_revision
                    and source.get('active') is False and epoch == retired_epoch
                    and source.get('history') is None):
                return retired_revision
            _, current_owner, source = self._record(data, session_id)
            if current_owner != owner or revision != expected_revision or epoch != expected_epoch:
                raise SessionSharingConflict('owner reservation changed before compensation')
            source.update(active=False, epoch=retired_epoch, history=None)
            record.update(retired=True, revision=retired_revision)
            self._storage._save(data)
            return retired_revision

    def source_epoch(self, session_id: str) -> int:
        """Host-only persisted CAS token, including an invalidated source."""
        with self._storage._locked():
            _, _, source = self._record(self._storage._load(), session_id, active=False)
            return source['epoch']

    def invalidate_source(self, session_id: str, *, expected_epoch: int) -> int:
        """Host MUST call before metadata rebind/delete, including failed operations."""
        with self._storage._locked():
            data = self._storage._load()
            _, _, source = self._record(data, session_id, active=False)
            if type(expected_epoch) is not int or source['epoch'] != expected_epoch:
                raise SessionSharingConflict('source epoch changed')
            source.update(epoch=_component(expected_epoch + 1, positive=True), active=False, history=None)
            self._storage._save(data)
            return source['epoch']

    def activate_source(self, session_id: str, project_id: str, *, expected_epoch: int) -> int:
        """Host-only CAS after successful rebind/recovery; never restores old grants."""
        with self._storage._locked():
            data = self._storage._load()
            _, owner, source = self._record(data, session_id, active=False)
            if type(expected_epoch) is not int or source['epoch'] != expected_epoch or source['active']:
                raise SessionSharingConflict('source is not the expected invalidated epoch')
            facts = self._live_binding(data, session_id, owner, project_id)
            source.update(project_id=project_id, active=True, history=None,
                          session_generation=facts[2], project_generation=facts[3])
            self._storage._save(data)
            return expected_epoch

    def prepare_source(self, session_id: str, identity: TrustedIdentity) -> SessionHistoryRange:
        """Before owner's Adapter dispatch: compile outside sidecar, then CAS.

        Recheck on both sides of IO. A lifecycle/source/ACL change rejects the
        publish. The same append-only inode may extend the source envelope
        without changing its epoch; replacement/truncation needs a new epoch.
        """
        with self._storage._locked():
            _, owner, source, facts = self._current(self._storage._load(), session_id)
            if identity != owner or 'manage' not in facts[0]:
                raise SessionSharingDenied('current owner management required')
            previous = copy.deepcopy(source)
            revision = _revision(source['epoch'], *facts[1:])
        scope = compile_shared_history_range(session_id,
            authorize=lambda: self.owner_current(session_id, identity, 'manage'))
        with self._storage._locked():
            data = self._storage._load()
            _, owner, source, facts = self._current(data, session_id)
            if (identity != owner or 'manage' not in facts[0] or source != previous
                    or _revision(source['epoch'], *facts[1:]) != revision):
                raise SessionSharingConflict('source changed during history preparation')
            if source['history'] is not None:
                old = SessionHistoryRange(**source['history'])
                if not scope.contains(old):
                    source['epoch'] = _component(source['epoch'] + 1, positive=True)
            source['history'] = asdict(scope)
            self._storage._save(data)
            return scope

    def compile_history(self, session_id: str, identity: TrustedIdentity,
                        parent_share_id: str | None) -> SessionHistoryRange:
        """Direct Adapter port; delegated ranges never expand their parent."""
        if parent_share_id is None:
            return self.prepare_source(session_id, identity)
        records = self.store.list_for_actor(identity)
        record = next((r for r in records if r['share_id'] == parent_share_id
                       and r['session_id'] == session_id and r.get('target') == asdict(identity)
                       and 'manage' in r.get('actions', [])), None)
        if record is None:
            raise SessionSharingDenied('parent sharing authority unavailable')
        scope = SessionHistoryRange(**record['history'])
        if not self.store.authorize(session_id, identity, 'manage', history=scope, share_id=parent_share_id).allowed:
            raise SessionSharingDenied('parent sharing authority changed')
        return scope

    @contextmanager
    def session_action_guard(self, session_id: str, identity: TrustedIdentity, action: str, *,
                             history: SessionHistoryRange | None = None, share_id: str | None = None):
        """Synchronous sidecar guard only; NEVER hold across await or history IO.

        For asynchronous work, exit this guard before awaiting and re-enter
        immediately before each delivery. The history reader has its own
        authorization-before-IO/after-IO checks and file identity validation.
        """
        with self._storage._locked():
            if share_id is None and history is None:
                if not self.owner_current(session_id, identity, action):
                    raise SessionSharingDenied('current Session owner action denied')
            elif not isinstance(history, SessionHistoryRange) or not self.store.authorize(
                session_id, identity, action, history=history, share_id=share_id,
            ).allowed:
                raise SessionSharingDenied('current Session sharing action denied')
            yield
