"""Persistent Session grants in the existing project sidecar and OS lock.

Only host registration establishes ownership. No metadata, project defaults,
subscription or caller-supplied identity fields can claim an unknown Session.
All reads reload the authority; no authorization cache or second lock exists.
"""
from __future__ import annotations

import copy
import time
import uuid
from dataclasses import asdict
from contextlib import contextmanager
from typing import Any

from jiuwenswarm.governance.contracts import TrustedIdentity
from jiuwenswarm.governance.resources import valid_expiry
from jiuwenswarm.governance.session_sharing import (
    SHARING_ACTIONS, SessionAuthorityResolver, SessionHistoryRange,
    SessionSharingAuthority, SessionSharingConflict, SessionSharingDecision,
    SessionSharingDenied,
)
from .project_access import ProjectAccessStore
from .session_history import is_valid_session_id
from .sharing_audit import (
    SharingAuditBounds, SharingAuditFacts, SharingAuditError, SharingAuditWriteResult,
    SharingAuditContext, AuditResultCallback, append_sharing_audit,
    audit_context as checked_audit_context, notify_audit_result, source_project,
    audit_query_params, validated_sharing_audit, project_audit_event,
)


class SessionSharingStore:
    def __init__(self, authority_resolver: SessionAuthorityResolver, *, storage=None, clock=time.time):
        self._resolve = authority_resolver
        self._storage = storage if storage is not None else ProjectAccessStore()
        self._clock = clock

    @staticmethod
    def _section(data):
        section = data.setdefault('session_sharing', {'schema_version': 1, 'owners': {}, 'shares': {}})
        if (not isinstance(section, dict) or section.get('schema_version') != 1
                or not isinstance(section.get('owners'), dict) or not isinstance(section.get('shares'), dict)):
            raise SessionSharingDenied('sharing storage unavailable')
        return section

    @staticmethod
    def _identity(value):
        if not isinstance(value, TrustedIdentity):
            raise SessionSharingDenied('trusted identity required')
        return asdict(value)

    @staticmethod
    def _session(session_id):
        if not isinstance(session_id, str) or not is_valid_session_id(session_id):
            raise ValueError('invalid product Session')
        return session_id

    def register_owner(self, session_id: str, owner: TrustedIdentity, *, expected_revision: int = 0) -> int:
        """Host-only creation/migration; never expose as an end-user claim API.

        Existing ownership is immutable. Retired IDs may only be recreated via
        an explicit expected tombstone revision, invalidating every old share.
        """
        self._session(session_id)
        identity = self._identity(owner)
        with self._storage._locked():
            data = self._storage._load()
            owners = self._section(data)['owners']
            old = owners.get(session_id)
            if session_id in owners and (not isinstance(old, dict)
                    or type(old.get('revision')) is not int or old['revision'] < 1
                    or old.get('retired') is not True):
                raise SessionSharingConflict('Session owner already registered or unavailable')
            if isinstance(old, dict) and 'deletion' in old:
                raise SessionSharingConflict('deletion Session IDs cannot be reused')
            revision = old['revision'] if old is not None else 0
            if type(expected_revision) is not int or expected_revision != revision:
                raise SessionSharingConflict('owner revision changed')
            if old and old.get('retired') is not True:
                raise SessionSharingConflict('Session owner already registered')
            owners[session_id] = {'identity': identity, 'revision': revision + 1, 'retired': False}
            self._storage._save(data)
            return revision + 1

    def retire_owner(self, session_id: str, *, expected_revision: int) -> int:
        """Host-only deletion tombstone; preserves old grant audit records."""
        self._session(session_id)
        with self._storage._locked():
            data = self._storage._load()
            old = self._section(data)['owners'].get(session_id)
            if not old or type(expected_revision) is not int or old['revision'] != expected_revision:
                raise SessionSharingConflict('owner revision changed')
            old.update(revision=expected_revision + 1, retired=True)
            self._storage._save(data)
            return expected_revision + 1

    def registered_owner(self, session_id: str) -> tuple[TrustedIdentity, int] | None:
        """Host-only persisted mapping; unknown/retired Sessions have no owner.

        The host resolver must additionally check Session existence and current
        source authorization. This mapping alone is not permission to read.
        """
        self._session(session_id)
        with self._storage._locked():
            record = self._section(self._storage._load())['owners'].get(session_id)
            if record is None or (isinstance(record, dict) and record.get('retired') is True):
                return None
            if (not isinstance(record, dict) or record.get('retired') is not False
                    or type(record.get('revision')) is not int or record['revision'] < 1):
                raise SessionSharingDenied('Session owner unavailable')
            try:
                return TrustedIdentity(**record['identity']), record['revision']
            except (KeyError, TypeError, ValueError) as exc:
                raise SessionSharingDenied('Session owner unavailable') from exc

    def _source(self, section, session_id):
        owner = section['owners'].get(session_id)
        if not isinstance(owner, dict) or owner.get('retired') is not False:
            raise SessionSharingDenied('Session owner unavailable')
        if type(owner.get('revision')) is not int or owner['revision'] < 1:
            raise SessionSharingDenied('Session owner unavailable')
        source = self._resolve(session_id)
        if (not isinstance(source, SessionSharingAuthority) or source.session_id != session_id
                or self._identity(source.owner) != owner.get('identity') or 'view' not in source.actions
                or (source.expires_at is not None and source.expires_at <= self._clock())):
            raise SessionSharingDenied('source authority unavailable')
        return owner, source

    @staticmethod
    def _expiry_within(expiry, parent):
        return valid_expiry(expiry) and (parent is None or (expiry is not None and expiry <= parent))

    def _active(self, section, share_id, owner, source, seen=None):
        seen = set() if seen is None else seen
        if share_id in seen or len(seen) >= 16:
            raise SessionSharingDenied('invalid sharing ancestry')
        seen.add(share_id)
        record = section['shares'].get(share_id)
        if (not isinstance(record, dict) or record.get('revoked') is not False
                or record.get('session_id') != source.session_id
                or record.get('owner_revision') != owner['revision']
                or record.get('source_revision') != source.revision
                or type(record.get('revision')) is not int or record['revision'] < 1):
            raise SessionSharingDenied('share unavailable')
        if (any(type(record.get(key)) is not int or record[key] < 1
                for key in ('owner_revision', 'source_revision'))
                or not isinstance(record.get('actions'), list)
                or any(not isinstance(action, str) for action in record['actions'])):
            raise SessionSharingDenied('invalid sharing record')
        # Malformed persisted identities cannot compare equal by partial fields.
        TrustedIdentity(**record['grantor'])
        TrustedIdentity(**record['target'])
        history = SessionHistoryRange(**record['history'])
        actions = frozenset(record['actions'])
        expiry = record['expires_at']
        if (not actions or not actions <= source.actions or not source.history.contains(history)
                or not self._expiry_within(expiry, source.expires_at)
                or (expiry is not None and expiry <= self._clock())):
            raise SessionSharingDenied('share authority expired')
        parent_id = record['parent_share_id']
        if parent_id is None:
            if record['grantor'] != self._identity(source.owner):
                raise SessionSharingDenied('source grantor changed')
        else:
            if not isinstance(parent_id, str) or type(record.get('parent_revision')) is not int:
                raise SessionSharingDenied('invalid sharing ancestry')
            parent = self._active(section, parent_id, owner, source, seen)
            if (parent['revision'] != record['parent_revision'] or parent['target'] != record['grantor']
                    or 'manage' not in parent['actions'] or not actions <= frozenset(parent['actions'])
                    or not SessionHistoryRange(**parent['history']).contains(history)
                    or not self._expiry_within(expiry, parent['expires_at'])):
                raise SessionSharingDenied('parent authority changed')
        return record

    def _grant_limit(self, section, owner, source, grantor, parent_share_id):
        if parent_share_id is None:
            if self._identity(grantor) != self._identity(source.owner) or 'manage' not in source.actions:
                raise SessionSharingDenied('delegation denied')
            return source.actions, source.history, source.expires_at, None
        parent = self._active(section, parent_share_id, owner, source)
        if parent['target'] != self._identity(grantor) or 'manage' not in parent['actions']:
            raise SessionSharingDenied('delegation denied')
        return frozenset(parent['actions']), SessionHistoryRange(**parent['history']), parent['expires_at'], parent['revision']

    def grant(self, session_id: str, grantor: TrustedIdentity, target: TrustedIdentity, *,
              actions, history: SessionHistoryRange, expires_at: float | None,
              parent_share_id: str | None = None, audit_context: SharingAuditContext | None = None,
              audit_result: AuditResultCallback | None = None) -> dict[str, Any]:
        record, result = self._write_grant(session_id, grantor, target, actions=actions, history=history,
                                          expires_at=expires_at, parent_share_id=parent_share_id,
                                          audit_context=audit_context)
        notify_audit_result(audit_result, result)
        return record

    def revise(self, share_id: str, grantor: TrustedIdentity, *, actions,
               history: SessionHistoryRange, expires_at: float | None, expected_revision: int,
               audit_context: SharingAuditContext | None = None,
               audit_result: AuditResultCallback | None = None) -> dict[str, Any]:
        """Replace a grant's bounds; descendants pin its old revision and expire."""
        with self._storage._locked():
            data = self._storage._load()
            record = self._section(data)['shares'].get(share_id)
            if not record or record.get('grantor') != self._identity(grantor):
                raise SessionSharingDenied('share unavailable')
            updated, result = self._write_grant(record['session_id'], grantor, TrustedIdentity(**record['target']),
                                                actions=actions, history=history, expires_at=expires_at,
                                                parent_share_id=record['parent_share_id'], share_id=share_id,
                                                expected_revision=expected_revision, audit_context=audit_context)
        notify_audit_result(audit_result, result)
        return updated

    def _write_grant(self, session_id, grantor, target, *, actions, history, expires_at,
                     parent_share_id, share_id=None, expected_revision=0, audit_context=None):
        self._session(session_id)
        grantor_data, target_data = self._identity(grantor), self._identity(target)
        actions = frozenset(actions)
        if not actions or not actions <= SHARING_ACTIONS or not isinstance(history, SessionHistoryRange):
            raise ValueError('invalid sharing bounds')
        with self._storage._locked():
            data = self._storage._load()
            section = self._section(data)
            owner, source = self._source(section, session_id)
            allowed, scope, expiry, parent_revision = self._grant_limit(section, owner, source, grantor, parent_share_id)
            if (not actions <= allowed or not scope.contains(history)
                    or not self._expiry_within(expires_at, expiry)
                    or (expires_at is not None and expires_at <= self._clock())):
                raise SessionSharingDenied('delegation exceeds current authority')
            ancestor = parent_share_id
            depth = 1
            while ancestor is not None:
                depth += 1
                if depth > 16:
                    raise SessionSharingDenied('sharing ancestry limit reached')
                ancestor = section['shares'][ancestor]['parent_share_id']
            old = section['shares'].get(share_id) if share_id else None
            if share_id:
                if not old or type(expected_revision) is not int or old['revision'] != expected_revision:
                    raise SessionSharingConflict('share revision changed')
                self._active(section, share_id, owner, source)
            else:
                share_id = uuid.uuid4().hex
            record = {'share_id': share_id, 'session_id': session_id, 'grantor': grantor_data,
                      'target': target_data, 'actions': sorted(actions), 'history': asdict(history),
                      'expires_at': expires_at, 'revision': expected_revision + 1, 'revoked': False,
                      'owner_revision': owner['revision'], 'source_revision': source.revision,
                      'parent_share_id': parent_share_id, 'parent_revision': parent_revision,
                      'created_at': old['created_at'] if old else self._clock(), 'updated_at': self._clock()}
            section['shares'][share_id] = record
            result = append_sharing_audit(data, checked_audit_context(grantor, audit_context),
                SharingAuditFacts('update' if old else 'create', session_id, share_id, target,
                    record['revision'], owner['revision'], source.revision,
                    decision_owner_revision=owner['revision'], decision_source_revision=source.revision,
                    source_project_id=source_project(section, session_id),
                    before_revision=expected_revision, after_revision=record['revision'],
                    parent_share_id=parent_share_id, parent_revision=parent_revision,
                    bounds_before=SharingAuditBounds.from_record(old) if old else None,
                    bounds_after=SharingAuditBounds.from_record(record)), recorded_at=self._clock())
            self._storage._save(data)
            return copy.deepcopy(record), result

    def revoke(self, share_id: str, identity: TrustedIdentity, *, expected_revision: int,
               audit_context: SharingAuditContext | None = None,
               audit_result: AuditResultCallback | None = None) -> int:
        with self._storage._locked():
            data = self._storage._load()
            section = self._section(data)
            record = section['shares'].get(share_id)
            if not record:
                raise SessionSharingDenied('share unavailable')
            owner, source = self._source(section, record['session_id'])
            identity_data = self._identity(identity)
            if identity_data == self._identity(source.owner):
                if 'manage' not in source.actions:
                    raise SessionSharingDenied('management denied')
            elif identity_data == record['grantor']:
                self._grant_limit(section, owner, source, identity, record['parent_share_id'])
            else:
                raise SessionSharingDenied('management denied')
            if type(expected_revision) is not int or record['revision'] != expected_revision:
                raise SessionSharingConflict('share revision changed')
            record.update(revoked=True, revision=expected_revision + 1, revoked_at=self._clock(),
                          revoked_by=identity_data)
            try:
                bounds = SharingAuditBounds.from_record(record)
                result = append_sharing_audit(data, checked_audit_context(identity, audit_context),
                    SharingAuditFacts('revoke', record['session_id'], share_id, TrustedIdentity(**record['target']),
                        record['revision'], record['owner_revision'], record['source_revision'],
                        decision_owner_revision=owner['revision'], decision_source_revision=source.revision,
                        source_project_id=source_project(section, record['session_id']),
                        before_revision=expected_revision, after_revision=record['revision'],
                        parent_share_id=record['parent_share_id'], parent_revision=record['parent_revision'],
                        bounds_before=bounds, bounds_after=bounds), recorded_at=self._clock())
            except (SharingAuditError, KeyError, TypeError, ValueError):
                # Revocation remains effective even if this non-authority domain
                # is corrupt. Preserve it verbatim, never reset or call it audited.
                result = SharingAuditWriteResult(False, True, 'audit_storage_invalid')
            self._storage._save(data)
        notify_audit_result(audit_result, result)
        return expected_revision + 1

    def authorize(self, session_id: str, identity: TrustedIdentity, action: str, *,
                  history: SessionHistoryRange, share_id: str | None = None) -> SessionSharingDecision:
        """Check immediately before each read/send/chunk/action; no durable lease."""
        try:
            self._session(session_id)
            identity_data = self._identity(identity)
            if action not in SHARING_ACTIONS or not isinstance(history, SessionHistoryRange):
                raise SessionSharingDenied('invalid request')
            with self._storage._locked():
                section = self._section(self._storage._load())
                owner, source = self._source(section, session_id)
                if share_id is None:
                    allowed = identity_data == self._identity(source.owner) and action in source.actions and source.history.contains(history)
                    revision = owner['revision']
                else:
                    record = self._active(section, share_id, owner, source)
                    allowed = record['target'] == identity_data and action in record['actions'] and SessionHistoryRange(**record['history']).contains(history)
                    revision = record['revision']
                return SessionSharingDecision(bool(allowed), session_id, action, share_id, revision,
                                              'allowed' if allowed else 'permission_denied')
        except Exception:
            # Resolver/storage errors are unknown authority, never legacy allow.
            return SessionSharingDecision(False, session_id, action, share_id, reason='authority_unavailable')

    @contextmanager
    def guard(self, session_id: str, identity: TrustedIdentity, action: str, *,
              history: SessionHistoryRange, share_id: str | None = None):
        """Keep same-sidecar mutations out of a synchronous operation.

        Never hold across await. Independent host authorities must still supply
        their own operation guard when they can change outside this sidecar.
        """
        with self._storage._locked():
            decision = self.authorize(session_id, identity, action, history=history, share_id=share_id)
            if not decision.allowed:
                raise SessionSharingDenied('sharing authorization denied')
            yield decision

    def _listing_record(self, section, record, owner, source):
        try:
            active = self._active(section, record['share_id'], owner, source)
        except Exception:
            # A manager can remove an obsolete grant without receiving its
            # historical range or former participants through this listing.
            return {'share_id': record['share_id'], 'session_id': record['session_id'],
                    'revision': record['revision'], 'state': 'unavailable'}
        return {**copy.deepcopy(active), 'state': 'active'}

    def list_for_actor(self, identity: TrustedIdentity) -> list[dict[str, Any]]:
        """Current incoming grants plus currently managed grants by this actor.

        Incoming items require a current action decision. Outgoing obsolete
        items are redacted and only visible under current delegation authority.
        """
        identity_data = self._identity(identity)
        with self._storage._locked():
            section = self._section(self._storage._load())
            results = []
            for record in section['shares'].values():
                try:
                    owner, source = self._source(section, record['session_id'])
                    if record['target'] == identity_data:
                        history = SessionHistoryRange(**record['history'])
                        if any(self.authorize(record['session_id'], identity, action,
                                              history=history, share_id=record['share_id']).allowed
                               for action in record['actions']):
                            results.append({**copy.deepcopy(record), 'state': 'active'})
                            continue
                    if record['grantor'] == identity_data:
                        self._grant_limit(section, owner, source, identity, record['parent_share_id'])
                        results.append(self._listing_record(section, record, owner, source))
                except Exception:
                    # A failed/missing current authority hides this item.
                    continue
            return results

    def list_for_session(self, session_id: str, identity: TrustedIdentity) -> list[dict[str, Any]]:
        """Owner management inventory, with obsolete ranges withheld."""
        self._session(session_id)
        identity_data = self._identity(identity)
        with self._storage._locked():
            section = self._section(self._storage._load())
            owner, source = self._source(section, session_id)
            if identity_data != self._identity(source.owner) or 'manage' not in source.actions:
                raise SessionSharingDenied('management denied')
            return [self._listing_record(section, record, owner, source)
                    for record in section['shares'].values()
                    if isinstance(record, dict) and record.get('session_id') == session_id]


    def query_audit(self, session_id: str, identity: TrustedIdentity, *, owner_guard, limit: int = 50) -> dict:
        """Read confirmed source-session history under the original sidecar lock.

        The injected live guard must verify current project/source/lifecycle and
        original credential. A registered owner alone never authorizes a read.
        No history file, share grant or client-provided range is consulted.
        """
        audit_query_params({'session_id': session_id, 'limit': limit})
        identity_data = self._identity(identity)
        if not callable(owner_guard):
            raise SessionSharingDenied('current owner guard required')
        with self._storage._locked():
            revision = owner_guard()
            if type(revision) is not int or revision < 1:
                raise SessionSharingDenied('current owner revision required')
            data = self._storage._load()
            owner = self._section(data)['owners'].get(session_id)
            if (not isinstance(owner, dict) or owner.get('retired') is not False
                    or owner.get('identity') != identity_data or type(owner.get('revision')) is not int
                    or owner['revision'] < 1):
                raise SessionSharingDenied('current Session owner required')
            section = validated_sharing_audit(data)
            selected = []
            for event in reversed(section['events']):
                facts = event['facts']
                if facts['source_session_id'] == session_id and facts['owner_revision'] == owner['revision']:
                    selected.append(event)
                    if len(selected) > limit:
                        break
            result = {'session_id': session_id, 'events': [project_audit_event(e) for e in selected[:limit]],
                      'has_more': len(selected) > limit,
                      'coverage': 'confirmed_mutations_and_publications_only'}
            current_revision = owner_guard()
            if type(current_revision) is not int or current_revision != revision:
                raise SessionSharingDenied('owner revision changed')
            return result
