"""Safe resource management projection over the original project sidecar.

These host ports never register resources or publish references, scopes or keys.
The original grant/revoke algorithms remain the only mutation authority.
"""
from __future__ import annotations

import copy
import inspect
import time
from dataclasses import dataclass, field
from typing import Callable

from jiuwenswarm.governance.contracts import TrustedIdentity
from jiuwenswarm.governance.resources import ResourceAccessDenied, valid_expiry, _ACTIONS
from .project_access import ProjectAccessStore, ProjectRevisionConflict

MAX_RESOURCES = 256
MAX_GRANTS = 1024


class ResourceMutationUnknown(RuntimeError):
    """Storage may have committed; refresh, never blindly repeat the mutation."""


def public_id(value):
    if (type(value) is not str or not 1 <= len(value) <= 200 or value != value.strip()
            or '/' in value or '\\' in value
            or any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in value)):
        raise ValueError('invalid resource management identifier')
    return value


def _identity(identity):
    if type(identity) is not TrustedIdentity or identity.actor_id != identity.subject_id:
        raise ResourceAccessDenied('authenticated independent subject required')


def _snapshot(store, project_id):
    data = store._load()
    record = store._resource_record(data, project_id)
    revision = record.get('acl_revision')
    if type(revision) is not int or revision < 1:
        raise ResourceAccessDenied('managed project authority unavailable')
    store._resource_state(record)
    return data, copy.deepcopy(record), copy.deepcopy(store._record(project_id))


def _can_revoke(store, data, project_id, actor, target, grant):
    return (actor in {target, grant.get('parent')}
            or store._decision(data, project_id, actor, 'admin').allowed)


def _active(store, state, definition, subject):
    try:
        return store._active_resource_grant(state, definition, subject, time.time())
    except (ResourceAccessDenied, ValueError, TypeError, KeyError, AttributeError):
        return None


def _inventory(store, data, project_id, identity):
    record = store._resource_record(data, project_id)
    state = store._resource_state(record)
    if record['acl_revision'] > 2 ** 53 - 1 or state['revision'] > 2 ** 53 - 1:
        raise ResourceAccessDenied('resource versions cannot be represented by the client')
    if len(state['catalog']) > MAX_RESOURCES:
        raise ResourceAccessDenied('resource management inventory limit exceeded')
    read = store._decision(data, project_id, identity.actor_id, 'read').allowed
    execute = store._decision(data, project_id, identity.actor_id, 'execute').allowed
    rows, cleanup, count = [], False, 0
    for resource_id in sorted(state['catalog']):
        # Host IDs were historically arbitrary normalized strings. Do not leak
        # path-shaped identifiers through the new public metadata surface.
        try:
            public_id(resource_id)
        except ValueError:
            continue
        definition = store._resource_definition(state, resource_id)
        raw_grants = state['grants'].get(resource_id, {})
        if not isinstance(raw_grants, dict):
            raise ResourceAccessDenied('resource management unavailable')
        count += len(raw_grants)
        if count > MAX_GRANTS:
            raise ResourceAccessDenied('resource management inventory limit exceeded')
        own = _active(store, state, definition, identity.subject_id)
        visible = []
        for target, raw in sorted(raw_grants.items()):
            if not isinstance(raw, dict):
                raise ResourceAccessDenied('resource management unavailable')
            if not _can_revoke(store, data, project_id, identity.actor_id, target, raw):
                continue
            cleanup = True
            try:
                public_id(target)
            except ValueError:
                continue
            active = _active(store, state, definition, target)
            # Inactive chains remain selectable for removal, without serializing
            # arbitrary/corrupt historical fields or implying usable authority.
            raw_actions = raw.get('actions')
            actions = (list(raw_actions) if type(raw_actions) is list and raw_actions
                       and all(type(action) is str and action in _ACTIONS[definition.kind]
                               for action in raw_actions) else [])
            expiry = raw.get('expires_at') if valid_expiry(raw.get('expires_at')) else None
            usable = active is not None and store._decision(data, project_id, target, 'execute').allowed
            visible.append({'target_actor': target, 'actions': actions,
                            'expires_at': expiry, 'can_revoke': True,
                            'state': 'active' if usable else 'unavailable'})
        if own is None and not visible:
            continue
        rows.append({'resource_id': resource_id, 'kind': definition.kind,
                     'actions': list(own['actions']) if own and execute else [],
                     'can_grant': bool(own and execute and own['delegable']),
                     'expires_at': own['expires_at'] if own else None,
                     'grants': visible})
    if not read and not cleanup:
        raise ResourceAccessDenied('resource management denied')
    return {'project_id': project_id, 'acl_revision': record['acl_revision'],
            'resource_revision': state['revision'], 'resources': rows}


@dataclass(frozen=True, repr=False)
class ResourceDelegationResult:
    """Original read/committed facts; check never confers execution authority."""
    _store: ProjectAccessStore
    _project: str
    _identity: TrustedIdentity
    _current: Callable = field(repr=False)
    _record: dict = field(repr=False)
    _registry: dict = field(repr=False)
    _payload: dict = field(repr=False)
    _listing: bool

    @property
    def payload(self):
        return copy.deepcopy(self._payload)

    def check(self):
        with self._store._locked():
            self._current()
            data, record, registry = _snapshot(self._store, self._project)
            if record != self._record or registry != self._registry:
                raise ResourceAccessDenied('resource management authorization changed')
            if self._listing and _inventory(self._store, data, self._project, self._identity) != self._payload:
                raise ResourceAccessDenied('resource management visibility changed')
            self._current()
            # Identity callbacks are host code but can re-enter the sidecar.
            data, record, registry = _snapshot(self._store, self._project)
            if record != self._record or registry != self._registry:
                raise ResourceAccessDenied('resource management authorization changed')
            if self._listing and _inventory(self._store, data, self._project, self._identity) != self._payload:
                raise ResourceAccessDenied('resource management visibility changed')


def delegation_inventory(store, project_id, identity, *, check_current):
    _identity(identity)
    with store._locked():
        check_current()
        data, record, registry = _snapshot(store, project_id)
        payload = _inventory(store, data, project_id, identity)
        result = ResourceDelegationResult(store, project_id, identity, check_current,
                                          record, registry, payload, True)
        result.check()
        return result


def mutate_delegation(store, method, params, identity, *, check_current, target_resolver):
    """Dual CAS and original mutation under the same reentrant sidecar lock."""
    _identity(identity)
    project_id, resource_id, target_actor = (params[k] for k in ('project_id', 'resource_id', 'target_actor'))
    with store._locked():
        check_current()
        data, before, registry = _snapshot(store, project_id)
        state = store._resource_state(before)
        if (before['acl_revision'] != params['expected_acl_revision']
                or state['revision'] != params['expected_resource_revision']):
            raise ProjectRevisionConflict('resource management revision changed')
        if method == 'project.resources.grant':
            target = target_resolver(identity, target_actor)
            if inspect.isawaitable(target):
                if inspect.iscoroutine(target):
                    target.close()
                raise ResourceAccessDenied('synchronous trusted directory required')
            if (type(target) is not TrustedIdentity or target.authority != identity.authority
                    or target.actor_id != target_actor or target.subject_id != target_actor
                    or target == identity):
                raise ResourceAccessDenied('resource recipient unavailable')
        elif method != 'project.resources.revoke':
            raise ValueError('unsupported resource mutation')
        check_current()
        _, current, current_registry = _snapshot(store, project_id)
        if current != before or current_registry != registry:
            raise ProjectRevisionConflict('resource management changed during admission')
        expected = copy.deepcopy(before)
        expected_state = store._resource_state(expected)
        expected_state['revision'] += 1
        if method == 'project.resources.grant':
            definition = store._resource_definition(state, resource_id)
            source = store._active_resource_grant(state, definition, identity.subject_id, time.time())
            expected_state['grants'].setdefault(resource_id, {})[target_actor] = {
                'actions': sorted(params['actions']), 'scope': source['scope'],
                'delegable': False, 'expires_at': source['expires_at'] if params['expires_at'] is None else params['expires_at'],
                'parent': identity.subject_id, 'parent_revision': source['revision'],
                'revision': expected_state['revision'],
            }
        else:
            expected_state['grants'].get(resource_id, {}).pop(target_actor, None)
        try:
            if method == 'project.resources.grant':
                revision = store.grant_resource(project_id, identity, resource_id,
                    subject_id=target_actor, actions=tuple(params['actions']),
                    expected_revision=params['expected_resource_revision'],
                    expires_at=params['expires_at'], delegable=False)
            else:
                # The original store permits self/parent/admin cleanup even
                # when execute, source grant or recipient credential expired.
                revision = store.revoke_resource(project_id, identity, resource_id,
                    subject_id=target_actor, expected_revision=params['expected_resource_revision'])
            _, after, current_registry = _snapshot(store, project_id)
            if (after != expected or current_registry != registry
                    or revision != expected_state['revision']):
                raise ResourceMutationUnknown('resource mutation result changed')
        except Exception:
            try:
                _, current, current_registry = _snapshot(store, project_id)
                unchanged = current == before and current_registry == registry
            except Exception:
                unchanged = False
            if not unchanged:
                raise ResourceMutationUnknown('resource mutation outcome unknown') from None
            raise
        facts = {'committed': True, 'project_id': project_id, 'resource_id': resource_id,
                 'target_actor': target_actor, 'resource_revision': revision}
        # The committed receipt exists before any later identity check/await.
        return ResourceDelegationResult(store, project_id, identity, check_current,
                                         after, registry, {'mutation': facts}, False)
