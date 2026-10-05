"""Typed mutation history in the original project sidecar, never an authority.

Append mutates only the caller's locked snapshot; the caller must use its single
original save for both the authority mutation and this event. No IO or lock is
owned here. Missing history is legacy data, not reconstructed past evidence.
"""
from __future__ import annotations

import json
import logging
import math
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Callable

from jiuwenswarm.governance.contracts import TrustedIdentity
from jiuwenswarm.governance.resources import valid_expiry
from jiuwenswarm.governance.session_sharing import SHARING_ACTIONS, SessionHistoryRange, SessionSharingDenied

logger = logging.getLogger(__name__)
AUDIT_KEY = 'sharing_audit'
MAX_EVENT_BYTES = 32768


class SharingAuditError(SessionSharingDenied):
    """Audit unavailable; the message must not include stored values."""


def _text(value, *, optional=False):
    if optional and value is None:
        return
    if (not isinstance(value, str) or not value or value != value.strip()
            or len(value) > 1024 or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise SharingAuditError('invalid sharing audit field')


def _integer(value, *, optional=False, minimum=1):
    if optional and value is None:
        return
    if type(value) is not int or not minimum <= value < 2 ** 256:
        raise SharingAuditError('invalid sharing audit revision')


def _identity(value):
    if not isinstance(value, TrustedIdentity):
        raise SharingAuditError('trusted sharing audit identity required')
    for item in asdict(value).values():
        _text(item)


@dataclass(frozen=True, slots=True)
class SharingAuditContext:
    """Host-created request correlation; never decode it from wire params."""
    actor: TrustedIdentity
    request_id: str | None = None
    method: str | None = None
    attempt_id: str = field(default_factory=lambda: uuid.uuid4().hex)

    def __post_init__(self):
        _identity(self.actor)
        _text(self.request_id, optional=True)
        _text(self.attempt_id)
        if self.method not in (None, 'session.share.create', 'session.share.update',
                               'session.share.revoke', 'session.share.continue',
                               'chat.cancel', 'chat.interrupt', 'session.delete'):
            raise SharingAuditError('invalid sharing audit method')


@dataclass(frozen=True, slots=True)
class SharingAuditBounds:
    actions: tuple[str, ...]
    history: SessionHistoryRange
    expires_at: float | None

    def __post_init__(self):
        if (type(self.actions) is not tuple or not self.actions
                or any(type(a) is not str for a in self.actions)
                or tuple(sorted(set(self.actions))) != self.actions
                or not set(self.actions) <= SHARING_ACTIONS
                or not isinstance(self.history, SessionHistoryRange)
                or not valid_expiry(self.expires_at)):
            raise SharingAuditError('invalid sharing audit bounds')

    @classmethod
    def from_record(cls, record):
        return cls(tuple(record['actions']), SessionHistoryRange(**record['history']), record['expires_at'])


@dataclass(frozen=True, slots=True)
class SharingAuditFacts:
    action: str
    source_session_id: str
    share_id: str
    target: TrustedIdentity
    share_revision: int
    owner_revision: int
    source_revision: int
    decision_owner_revision: int | None = None
    decision_source_revision: int | None = None
    source_project_id: str | None = None
    target_session_id: str | None = None
    target_project_id: str | None = None
    target_revision: int | None = None
    before_revision: int | None = None
    after_revision: int | None = None
    parent_share_id: str | None = None
    parent_revision: int | None = None
    bounds_before: SharingAuditBounds | None = None
    bounds_after: SharingAuditBounds | None = None
    publication_id: str | None = None
    seed_digest: str | None = None
    phase: str = 'mutation'
    result: str = 'committed'

    def __post_init__(self):
        if self.action not in ('create', 'update', 'revoke', 'continue') or self.result != 'committed':
            raise SharingAuditError('invalid sharing audit operation')
        if self.phase != ('publication' if self.action == 'continue' else 'mutation'):
            raise SharingAuditError('invalid sharing audit phase')
        for name in ('source_session_id', 'share_id'):
            _text(getattr(self, name))
        for name in ('source_project_id', 'target_session_id', 'target_project_id', 'parent_share_id'):
            _text(getattr(self, name), optional=True)
        _identity(self.target)
        for name in ('share_revision', 'owner_revision', 'source_revision'):
            _integer(getattr(self, name))
        for name in ('target_revision', 'after_revision', 'parent_revision',
                     'decision_owner_revision', 'decision_source_revision'):
            _integer(getattr(self, name), optional=True)
        _integer(self.before_revision, optional=True, minimum=0)
        if (self.parent_share_id is None) != (self.parent_revision is None):
            raise SharingAuditError('invalid sharing audit parent')
        for value in (self.bounds_before, self.bounds_after):
            if value is not None and (not isinstance(value, SharingAuditBounds)
                                      or value.history.session_id != self.source_session_id):
                raise SharingAuditError('invalid sharing audit range')
        for value in (self.publication_id, self.seed_digest):
            if value is not None and (type(value) is not str or len(value) != 64
                                     or any(c not in '0123456789abcdef' for c in value)):
                raise SharingAuditError('invalid sharing audit reference')
        if self.action == 'continue':
            if (self.target_session_id is None or self.target_project_id is None
                    or self.target_revision is None or self.publication_id is None or self.seed_digest is None
                    or self.before_revision is not None or self.after_revision is not None
                    or self.bounds_before is not None or self.bounds_after is None):
                raise SharingAuditError('incomplete publication audit')
        elif (self.before_revision is None or self.after_revision != self.before_revision + 1
              or self.share_revision != self.after_revision or self.bounds_after is None
              or (self.action == 'create') != (self.before_revision == 0)
              or (self.action == 'create') != (self.bounds_before is None)):
            raise SharingAuditError('incomplete mutation audit')
        elif any(value is not None for value in (self.target_session_id, self.target_project_id,
                                                self.target_revision, self.publication_id, self.seed_digest)):
            raise SharingAuditError('unexpected mutation publication fields')


_LIFECYCLE_RESULTS = {'exit_requested': 'requested', 'cleanup_retry': 'retrying',
                      'exit_unconfirmed': 'unconfirmed', 'exit_confirmed': 'confirmed'}
LIFECYCLE_AUDIT_COVERAGE = 'confirmed_mutations_publications_and_owner_exit_observations_only'


@dataclass(frozen=True, slots=True)
class OwnerLifecycleAuditFacts:
    """Host-only observation copied from the original owner/cleanup receipt.

    This value does not authorize cleanup or establish exit. The Runtime must
    obtain these facts from its original receipt and actual resource-close
    result. Never decode it from request params, a producer terminal or latest
    Session ownership. Append owns neither a lifecycle nor a persistence lock.
    """
    source_session_id: str
    source_project_id: str
    owner_revision: int
    source_revision: int
    action: str
    phase: str
    result: str
    operation_id: str
    generation: int | None

    def __post_init__(self):
        if (type(self.action) is not str or type(self.phase) is not str or type(self.result) is not str
                or self.action not in ('cancel', 'delete')
                or self.phase not in _LIFECYCLE_RESULTS
                or self.result != _LIFECYCLE_RESULTS[self.phase]):
            raise SharingAuditError('invalid owner lifecycle observation')
        for name in ('source_session_id', 'source_project_id', 'operation_id'):
            _text(getattr(self, name))
        for name in ('owner_revision', 'source_revision'):
            _integer(getattr(self, name))
        _integer(self.generation, optional=self.action == 'cancel', minimum=0 if self.action == 'cancel' else 1)


def _expected_methods(facts):
    if isinstance(facts, OwnerLifecycleAuditFacts):
        return ('chat.cancel', 'chat.interrupt') if facts.action == 'cancel' else ('session.delete',)
    return ('session.share.' + facts.action,)


def _lifecycle_key(context, facts):
    # Authority facts are compared on collision, not used to evade collisions.
    key = tuple(facts[name] for name in ('source_session_id', 'action', 'operation_id', 'generation', 'phase'))
    return key if facts['phase'] == 'exit_confirmed' else (*key, context['attempt_id'])


@dataclass(frozen=True, slots=True)
class SharingAuditWriteResult:
    persisted: bool
    degraded: bool
    reason: str
    sequence: int | None = None
    event_id: str | None = None


AuditResultCallback = Callable[[SharingAuditWriteResult], None]


def _exact(value, keys):
    if type(value) is not dict or set(value) != set(keys):
        raise SharingAuditError('invalid sharing audit schema')


def _decode_identity(value):
    _exact(value, TrustedIdentity.__dataclass_fields__)
    return TrustedIdentity(**value)


def _validate_event(event):
    _exact(event, ('sequence', 'event_id', 'recorded_at', 'context', 'facts'))
    _integer(event['sequence'])
    _text(event['event_id'])
    if type(event['recorded_at']) not in (int, float) or not math.isfinite(event['recorded_at']):
        raise SharingAuditError('invalid sharing audit timestamp')
    _exact(event['context'], SharingAuditContext.__dataclass_fields__)
    context = dict(event['context'])
    context['actor'] = _decode_identity(context['actor'])
    SharingAuditContext(**context)
    if type(event['facts']) is not dict:
        raise SharingAuditError('invalid sharing audit facts')
    if event['facts'].get('action') in ('cancel', 'delete'):
        _exact(event['facts'], OwnerLifecycleAuditFacts.__dataclass_fields__)
        facts = OwnerLifecycleAuditFacts(**event['facts'])
    else:
        _exact(event['facts'], SharingAuditFacts.__dataclass_fields__)
        decoded = dict(event['facts'])
        decoded['target'] = _decode_identity(decoded['target'])
        for name in ('bounds_before', 'bounds_after'):
            if decoded[name] is not None:
                _exact(decoded[name], SharingAuditBounds.__dataclass_fields__)
                bounds = dict(decoded[name])
                if type(bounds['actions']) is not list:
                    raise SharingAuditError('invalid sharing audit actions')
                bounds['actions'] = tuple(bounds['actions'])
                _exact(bounds['history'], SessionHistoryRange.__dataclass_fields__)
                bounds['history'] = SessionHistoryRange(**bounds['history'])
                decoded[name] = SharingAuditBounds(**bounds)
        facts = SharingAuditFacts(**decoded)
    if context['method'] is not None and context['method'] not in _expected_methods(facts):
        raise SharingAuditError('sharing audit method mismatch')
    if len(json.dumps(event, ensure_ascii=False, allow_nan=False).encode()) > MAX_EVENT_BYTES:
        raise SharingAuditError('sharing audit event too large')



def validated_sharing_audit(data: dict) -> dict:
    """Validate existing history without creating, repairing or authorizing it."""
    section = data.get(AUDIT_KEY, {'schema_version': 1, 'next_sequence': 1, 'events': []})
    _exact(section, ('schema_version', 'next_sequence', 'events'))
    if type(section['schema_version']) is not int or section['schema_version'] != 1 or type(section['events']) is not list:
        raise SharingAuditError('unsupported sharing audit schema')
    _integer(section['next_sequence'])
    if section['next_sequence'] != len(section['events']) + 1:
        raise SharingAuditError('invalid sharing audit sequence')
    ids = set()
    for sequence, old in enumerate(section['events'], 1):
        _validate_event(old)
        if old['sequence'] != sequence or old['event_id'] in ids:
            raise SharingAuditError('invalid sharing audit order')
        ids.add(old['event_id'])
    return section


def audit_query_params(params: dict) -> tuple[str, int]:
    """Exact bounded selectors, validated before any Gateway preprocessing."""
    from .session_history import is_valid_session_id
    if type(params) is not dict or set(params) - {'session_id', 'limit'}:
        raise ValueError('invalid sharing audit query')
    session_id, limit = params.get('session_id'), params.get('limit', 50)
    if (not isinstance(session_id, str) or not is_valid_session_id(session_id)
            or type(limit) is not int or not 1 <= limit <= 100):
        raise ValueError('invalid sharing audit query')
    return session_id, limit


def project_audit_event(event: dict) -> dict:
    """Owner UI projection; no seed, file bounds, subject, or other Session IDs."""
    context, facts = event['context'], event['facts']
    result = {name: event[name] for name in ('sequence', 'event_id', 'recorded_at')}
    result.update({name: facts[name] for name in ('action', 'phase', 'result')})
    lifecycle_event = facts['action'] in ('cancel', 'delete')
    result.update({name: None if lifecycle_event else facts[name] for name in
                   ('share_id', 'share_revision', 'before_revision', 'after_revision')})
    result.update(actor_id=context['actor']['actor_id'],
                  target_actor_id=None if lifecycle_event else facts['target']['actor_id'],
                  request_id=context['request_id'], method=context['method'] or 'host_api')
    return result


def append_sharing_audit(data: dict, context: SharingAuditContext, facts: SharingAuditFacts | OwnerLifecycleAuditFacts,
                         *, recorded_at: float | None = None) -> SharingAuditWriteResult:
    """Stage an event. Its receipt is publishable only AFTER the caller saves."""
    try:
        if not isinstance(context, SharingAuditContext) or not isinstance(facts, (SharingAuditFacts, OwnerLifecycleAuditFacts)):
            raise SharingAuditError('typed sharing audit required')
        if context.method is not None and context.method not in _expected_methods(facts):
            raise SharingAuditError('sharing audit method mismatch')
        section = validated_sharing_audit(data)
        if isinstance(facts, OwnerLifecycleAuditFacts):
            immutable, correlation = asdict(facts), asdict(context)
            key = _lifecycle_key(correlation, immutable)
            for old in section['events']:
                if old['facts']['action'] not in ('cancel', 'delete'):
                    continue
                if _lifecycle_key(old['context'], old['facts']) == key:
                    if (old['facts'] != immutable or old['context']['actor'] != correlation['actor']
                            or facts.phase != 'exit_confirmed' and old['context'] != correlation):
                        raise SharingAuditError('owner lifecycle audit receipt conflict')
                    # Return the original event; never replace its correlation.
                    return SharingAuditWriteResult(False, False, 'audit_staged', old['sequence'], old['event_id'])
        event_id = uuid.uuid4().hex
        _integer(section['next_sequence'] + 1)
        event = {'sequence': section['next_sequence'], 'event_id': event_id,
                 'recorded_at': time.time() if recorded_at is None else recorded_at,
                 'context': asdict(context), 'facts': asdict(facts)}
        # Normalize tuples into the exact persisted JSON shape before validation.
        event = json.loads(json.dumps(event, ensure_ascii=False, allow_nan=False))
        _validate_event(event)
        # No mutation, even to a malformed old audit section, before validation.
        data[AUDIT_KEY] = {**section, 'next_sequence': event['sequence'] + 1,
                           'events': [*section['events'], event]}
        return SharingAuditWriteResult(False, False, 'audit_staged', event['sequence'], event_id)
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise SharingAuditError('sharing audit unavailable') from exc


def audit_context(actor: TrustedIdentity, context: SharingAuditContext | None) -> SharingAuditContext:
    if context is None:
        return SharingAuditContext(actor)
    if not isinstance(context, SharingAuditContext) or context.actor != actor:
        raise SharingAuditError('sharing audit actor mismatch')
    return context


def notify_audit_result(callback: AuditResultCallback | None, result: SharingAuditWriteResult):
    """Post-save observation cannot replay a mutation or leak callback errors."""
    if result.degraded:
        logger.warning('sharing_audit_degraded: audit_storage_invalid')
    else:
        result = SharingAuditWriteResult(True, False, 'audit_persisted', result.sequence, result.event_id)
    if callback is not None:
        try:
            callback(result)
        except BaseException:
            logger.warning('sharing_audit_result_callback_failed')


def source_project(section, session_id):
    """Use only the registered host source; legacy owners may have no project."""
    source = section['owners'][session_id].get('source')
    return source.get('project_id') if isinstance(source, dict) else None
