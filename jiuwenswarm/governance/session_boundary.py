"""Explicit organization Session request and delivery authority.

Routing IDs and project membership never establish Session ownership. The host
uses the same durable owner/source directory for admission and final delivery.
"""
from __future__ import annotations

import hashlib
import json

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from typing import Callable

from .contracts import TrustedIdentity
from .session_sharing import SessionHistoryRange, SessionSharingDenied

# Deliberately exact: adding a protocol method requires classifying its data.
OWNER_METHODS = frozenset({
    'session.get_metadata', 'session.preview', 'session.pin', 'session.color_set',
    'session.rename', 'session.switch', 'session.plan_status', 'session.input.intent',
    'history.get', 'history.list_turns', 'chat.send', 'chat.resume', 'chat.answer',
    'chat.cancel', 'chat.interrupt', 'session.stop', 'session.delete', 'session.rewind',
    'session.rewind_and_restore', 'session.rewind_compact', 'session.rewind_context',
    'session.restore_files', 'session.rebind_project', 'surface.capabilities.get',
})
SHARE_METHODS = frozenset({
    'session.share.list', 'session.share.create', 'session.share.update',
    'session.share.revoke', 'session.share.history.get',
})
GLOBAL_METHODS = frozenset({
    'config.get', 'models.list',
    'session.list', 'project.list', 'project.create', 'project.info',
    'project.content.get', 'project.content.update', 'project.get_sessions',
    'project.get_cron_sessions', 'project.pinned_sessions',
})


INVENTORY_METHODS = frozenset({
    'session.list', 'project.list', 'project.info', 'project.get_sessions',
    'project.get_cron_sessions', 'project.pinned_sessions', 'session.share.list',
})


def _inventory_revision(host):
    # A request-local version proof over the existing authority, not another
    # persistent policy store. Mutation during a scan invalidates its buffer.
    with host._storage._locked():
        data = host._storage._load()
        # Compiling an append-only source extent prepares a view; it does not
        # change authority. Replacement/truncation increments source.epoch.
        # Fixed granted ranges remain included under shares and revisions.
        for owner in data.get('session_sharing', {}).get('owners', {}).values():
            if isinstance(owner, dict) and isinstance(owner.get('source'), dict):
                owner['source'].pop('history', None)
        return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def organization_sharing_host():
    from .organization_auth import configured_authenticator
    from jiuwenswarm.server.runtime.session.sharing_host import SharingHostService
    auth = configured_authenticator()
    if auth is None:
        return None
    return SharingHostService(auth.resolve_actor, known_actor=auth.known_actor)


def _session(value):
    from jiuwenswarm.server.runtime.session.session_history import is_valid_session_id
    if not isinstance(value, str) or not is_valid_session_id(value):
        raise SessionSharingDenied('valid Session required')
    return value


@dataclass(frozen=True)
class SessionRequestPermit:
    identity: TrustedIdentity
    identity_resolver: Callable[[], TrustedIdentity | None]
    host: object
    owners: tuple[tuple[str, int], ...] = ()
    share: tuple[str, str, int, SessionHistoryRange] | None = None
    inventory_revision: str | None = None
    method: str = ""

    def revalidate(self) -> bool:
        try:
            if self.identity_resolver() != self.identity:
                return False
            if self.inventory_revision is not None and _inventory_revision(self.host) != self.inventory_revision:
                return False
            for session_id, epoch in self.owners:
                if (not self.host.owner_current(session_id, self.identity)
                        or self.host.owner_revision(session_id, self.identity) != epoch):
                    return False
            if self.share is not None:
                session_id, share_id, revision, history = self.share
                decision = self.host.store.authorize(session_id, self.identity, 'view',
                                                     share_id=share_id, history=history)
                if not decision.allowed or decision.revision != revision:
                    return False
            return True
        except Exception:
            return False


def admit_session_request(method: str, params: dict, *, identity_resolver: Callable,
                          host, envelope_session: str | None = None) -> SessionRequestPermit:
    identity = identity_resolver()
    if not isinstance(identity, TrustedIdentity) or not isinstance(params, dict):
        raise SessionSharingDenied('authenticated request required')
    owners = []
    share = None
    sid = params.get('session_id')
    if sid is not None:
        sid = _session(sid)
        if envelope_session and envelope_session != sid:
            raise SessionSharingDenied('conflicting Session references')
    if method in OWNER_METHODS:
        sid = _session(sid or envelope_session)
        owners.append((sid, host.owner_revision(sid, identity)))
        # These old routes may carry a second private stream or restore path.
        if params.get('share_id') is not None:
            raise SessionSharingDenied('sharing does not authorize the owner API')
    elif method == 'session.create':
        if sid is not None:
            raise SessionSharingDenied('Session creation cannot claim a supplied ID')
        if params.get('previous_session_id'):
            previous = _session(params['previous_session_id'])
            owners.append((previous, host.owner_revision(previous, identity)))
    elif method in SHARE_METHODS:
        if method == 'session.share.history.get':
            sid = _session(sid)
            share_id = params.get('share_id')
            records = host.store.list_for_actor(identity)
            record = next((r for r in records if r['session_id'] == sid and r['share_id'] == share_id
                           and r.get('target') == asdict(identity)), None)
            if record is None:
                raise SessionSharingDenied('shared history unavailable')
            history = SessionHistoryRange(**record['history'])
            decision = host.store.authorize(sid, identity, 'view', share_id=share_id, history=history)
            share = (sid, share_id, decision.revision, history)
        # Management is checked by the sharing adapter, never active subscription.
    elif method not in GLOBAL_METHODS:
        raise SessionSharingDenied('organization method requires an explicit policy')
    permit = SessionRequestPermit(identity, identity_resolver, host, tuple(owners), share,
                                  _inventory_revision(host) if method in INVENTORY_METHODS else None, method)
    if not permit.revalidate():
        raise SessionSharingDenied('Session authorization denied')
    return permit

# Per-request delivery scope; asynchronous history producers inherit the exact
# permit, while unrelated pushes have no implicit authority.
_delivery = ContextVar('organization_session_delivery', default=None)


@contextmanager
def delivery_scope():
    token = _delivery.set(None)
    try:
        yield
    finally:
        _delivery.reset(token)


def set_delivery_permit(permit):
    if not isinstance(permit, SessionRequestPermit):
        raise TypeError('host Session permit required')
    _delivery.set(permit)


def delivery_authorized():
    from .organization_auth import configured_authenticator
    if configured_authenticator() is None:
        return True
    permit = _delivery.get()
    return isinstance(permit, SessionRequestPermit) and permit.revalidate()


_inventory_identity = ContextVar('organization_inventory_identity', default=None)


@contextmanager
def inventory_identity_scope(resolver):
    token = _inventory_identity.set(resolver)
    try:
        yield
    finally:
        _inventory_identity.reset(token)


def current_inventory_identity():
    from .organization_auth import current_identity
    resolver = _inventory_identity.get()
    return resolver() if callable(resolver) else current_identity()


def filter_current_inventory(rows):
    host = organization_sharing_host()
    if host is None:
        return rows
    identity = current_inventory_identity()
    return [row for row in rows if host.owner_current(row.get('session_id'), identity)]
