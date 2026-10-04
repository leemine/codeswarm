"""Explicit organization Session request and delivery authority.

Routing IDs and project membership never establish Session ownership. The host
uses the same durable owner/source directory for admission and final delivery.
"""
from __future__ import annotations

import hashlib
import json

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, replace
from typing import Callable

from .continuation import ContinuationInput
from .contracts import TrustedIdentity
from .session_sharing import SessionHistoryRange, SessionSharingDenied

# Deliberately exact: adding a protocol method requires classifying its data.
OWNER_METHODS = frozenset({
    'session.share.audit.list',
    'session.get_metadata', 'session.preview', 'session.pin', 'session.color_set',
    'session.rename', 'session.switch', 'session.plan_status', 'session.input.intent',
    'history.get', 'history.list_turns', 'chat.send', 'chat.resume', 'chat.answer',
    'chat.cancel', 'chat.interrupt', 'session.stop', 'session.delete', 'session.rewind',
    'session.rewind_and_restore', 'session.rewind_compact', 'session.rewind_context',
    'session.restore_files', 'session.rebind_project', 'surface.capabilities.get',
})
CLEANUP_METHODS = frozenset({'chat.cancel', 'chat.interrupt', 'session.stop', 'session.delete'})

SHARE_METHODS = frozenset({
    'session.share.audit.list', 'session.share.list', 'session.share.create', 'session.share.update',
    'session.share.revoke', 'session.share.history.get',
    'session.share.continuation.options', 'session.share.continue',
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
        # Mutation history is not authority. Only this explicit audit domain is
        # excluded; shares/ACL/owners/resources/publications remain in the proof.
        data.pop('sharing_audit', None)
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


def parse_continuation_options(params):
    """Exact public source/target selectors, without execution or authority data."""
    fields = {'session_id', 'share_id', 'expected_revision', 'target_project_id'}
    if type(params) is not dict or set(params) != fields:
        raise ValueError('invalid continuation options fields')
    for key in ('session_id', 'share_id', 'target_project_id'):
        value = params[key]
        if (not isinstance(value, str) or not value or len(value) > 200
                or value != value.strip() or any(ord(char) < 32 for char in value)):
            raise ValueError('invalid continuation options selector')
    revision = params['expected_revision']
    if type(revision) is not int or not 1 <= revision < 2 ** 63:
        raise ValueError('invalid continuation options revision')
    _session(params['session_id'])
    return dict(params)


def is_cleanup_request(method: str, params: dict) -> bool:
    """Classify pure cleanup only; routing hints never establish authority.

    Pause/resume/supplement retain the normal owner/execution path. In particular,
    a cancel envelope may not smuggle new input into a cleanup-only decision.
    """
    if method not in CLEANUP_METHODS:
        return False
    if type(params) is not dict:
        raise SessionSharingDenied('cleanup parameters required')
    intent = params.get('intent', 'cancel')
    if isinstance(intent, str) and method in {'chat.cancel', 'chat.interrupt'} and intent in {'pause', 'resume', 'supplement'}:
        return False
    if intent != 'cancel':
        raise SessionSharingDenied('pure cancel intent required')
    fields = {'session_id', 'intent', 'target_request_id', 'mode', 'team',
              'work_mode', 'project_dir', 'project_id', 'trusted_dirs'}
    if set(params) - fields:
        raise SessionSharingDenied('cleanup cannot carry execution parameters')
    for key, value in params.items():
        if key == 'team':
            valid = type(value) is bool
        elif key == 'trusted_dirs':
            valid = (type(value) is list and len(value) <= 100
                     and all(isinstance(item, str) and len(item) <= 32768 for item in value))
        else:
            valid = isinstance(value, str) and len(value) <= 32768
        if not valid:
            raise SessionSharingDenied('invalid cleanup routing hint')
    return True


@dataclass(frozen=True)
class SessionRequestPermit:
    identity: TrustedIdentity
    identity_resolver: Callable[[], TrustedIdentity | None]
    host: object
    owners: tuple[tuple[str, int], ...] = ()
    share: tuple[str, str, int, SessionHistoryRange] | None = None
    inventory_revision: str | None = None
    method: str = ""
    share_actions: tuple[str, ...] = ('view',)
    continuation_input: ContinuationInput | None = None
    continuation_options: tuple[tuple[str, object], ...] | None = None

    cleanup: tuple[str, tuple] | None = None
    cleanup_params: str | None = None
    deletion_receipt: object | None = None
    workspace_download: object | None = None
    goal_read_route: object | None = None
    goal_read_result: object | None = None
    goal_mutation_route: object | None = None
    goal_mutation_result: object | None = None

    def allows_cleanup(self, method: str, params: dict, identity: TrustedIdentity,
                       envelope_session: str | None = None) -> bool:
        """Consume this exact local permit, never a method-based ACL exemption."""
        try:
            if (self.cleanup is None or self.identity != identity or self.method != method
                    or not is_cleanup_request(method, params)):
                return False
            sid = _session(params.get('session_id') or envelope_session)
            if envelope_session and envelope_session != sid:
                return False
            return (sid == self.cleanup[0]
                    and json.dumps(params, sort_keys=True, separators=(',', ':')) == self.cleanup_params
                    and self.revalidate())
        except Exception:
            return False

    def revalidate(self) -> bool:
        try:
            if self.identity_resolver() != self.identity:
                return False
            if self.goal_read_route is not None:
                self.goal_read_route.check()
            if self.goal_read_result is not None:
                self.goal_read_result.final_check()
            if self.goal_mutation_route is not None:
                self.goal_mutation_route.check()
            if self.goal_mutation_result is not None:
                if (self.goal_mutation_route is None
                        or self.goal_mutation_result.final_check() is not None):
                    return False
                self.goal_mutation_route.check()
            if self.workspace_download is not None:
                self.workspace_download.check()
            if self.inventory_revision is not None and _inventory_revision(self.host) != self.inventory_revision:
                return False
            if self.cleanup is not None:
                session_id, stamp = self.cleanup
                if self.deletion_receipt is not None:
                    if self.method != 'session.delete':
                        return False
                    self.host.check_deletion(self.deletion_receipt, for_admission=True)
                elif self.host.cleanup_owner_stamp(session_id, self.identity) != stamp:
                    return False
            for session_id, epoch in self.owners:
                if (not self.host.owner_current(session_id, self.identity)
                        or self.host.owner_revision(session_id, self.identity) != epoch):
                    return False
            if self.share is not None:
                session_id, share_id, revision, history = self.share
                for action in self.share_actions:
                    decision = self.host.store.authorize(session_id, self.identity, action,
                                                         share_id=share_id, history=history)
                    if decision.allowed is not True or decision.revision != revision:
                        return False
            return True
        except Exception:
            return False


def admit_session_request(method: str, params: dict, *, identity_resolver: Callable,
                          host, envelope_session: str | None = None) -> SessionRequestPermit:
    if method == 'command.goal' and type(params) is dict and params.get('action', 'get') != 'get':
        from .goal_mutation import admit_goal_mutation
        return admit_goal_mutation(params, identity_resolver=identity_resolver,
                                   host=host, envelope_session=envelope_session)
    identity = identity_resolver()
    if not isinstance(identity, TrustedIdentity) or not isinstance(params, dict):
        raise SessionSharingDenied('authenticated request required')
    if method == 'session.share.audit.list':
        from jiuwenswarm.server.runtime.session.sharing_audit import audit_query_params
        audit_query_params(params)
    owners = []
    workspace_download = None
    goal_read_route = None
    cleanup = None
    deletion_receipt = None
    share = None
    share_actions = ('view',)
    continuation_input = None
    continuation_options = None
    sid = params.get('session_id')
    if sid is not None:
        sid = _session(sid)
        if envelope_session and envelope_session != sid:
            raise SessionSharingDenied('conflicting Session references')
    if is_cleanup_request(method, params):
        sid = _session(sid or envelope_session)
        if method == 'session.delete':
            # A pending or retired exact delete receipt is the only authority
            # after metadata disappears. It grants no normal owner capability.
            try:
                deletion_receipt = host.resume_deletion(sid, identity, identity_resolver=identity_resolver)
            except SessionSharingDenied:
                cleanup = (sid, host.cleanup_owner_stamp(sid, identity))
            else:
                cleanup = (sid, None)
        else:
            cleanup = (sid, host.cleanup_owner_stamp(sid, identity))
    elif method == 'command.goal':
        from .goal_read import capture_goal_read_route, validate_goal_get
        validate_goal_get(params)
        sid = _session(sid or envelope_session)
        epoch = host.owner_revision(sid, identity)
        owners.append((sid, epoch))

        def check_goal_owner():
            if (identity_resolver() != identity or not host.owner_current(sid, identity)
                    or host.owner_revision(sid, identity) != epoch):
                raise SessionSharingDenied('Original Goal read owner changed')

        goal_read_route = capture_goal_read_route(sid, params, check_goal_owner)
    elif method == 'file.download_workspace_chunk':
        from .workspace_download import capture_workspace_request
        sid = _session(sid or envelope_session)
        workspace_download = capture_workspace_request(host, identity_resolver, sid, params)
        owners.append((sid, host.owner_revision(sid, identity)))
    elif method in OWNER_METHODS:
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
        if method in {'session.share.history.get', 'session.share.continuation.options', 'session.share.continue'}:
            if method == 'session.share.continue':
                continuation_input = ContinuationInput.from_wire(params)
                share_actions = ('view', 'execute')
            elif method == 'session.share.continuation.options':
                continuation_options = tuple(sorted(parse_continuation_options(params).items()))
                share_actions = ('view', 'execute')
            sid = _session(sid)
            share_id = params.get('share_id')
            records = host.store.list_for_actor(identity)
            record = next((r for r in records if r['session_id'] == sid and r['share_id'] == share_id
                           and r.get('target') == asdict(identity)), None)
            if record is None:
                raise SessionSharingDenied('shared history unavailable')
            if len(share_actions) > 1 and record.get('revision') != params['expected_revision']:
                raise SessionSharingDenied('continuation share changed')
            history = SessionHistoryRange(**record['history'])
            decision = host.store.authorize(sid, identity, 'view', share_id=share_id, history=history)
            share = (sid, share_id, decision.revision, history)
        # Management is checked by the sharing adapter, never active subscription.
    elif method not in GLOBAL_METHODS:
        raise SessionSharingDenied('organization method requires an explicit policy')
    permit = SessionRequestPermit(identity, identity_resolver, host, tuple(owners), share,
                                  _inventory_revision(host) if method in INVENTORY_METHODS else None, method,
                                  share_actions, continuation_input, continuation_options, cleanup,
                                  json.dumps(params, sort_keys=True, separators=(',', ':')) if cleanup else None,
                                  deletion_receipt, workspace_download, goal_read_route)
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


def bind_goal_read_delivery(session_id, identity, result):
    """Retain the actual reader check in the original AgentServer delivery scope."""
    from jiuwenswarm.runtime.goal_read import NativeGoalRead
    if type(result) is not NativeGoalRead:
        raise TypeError('Actual Native Goal read result required')
    permit = _delivery.get()
    if permit is None:  # Direct Runtime caller has no transport delivery scope.
        return
    if (permit.method != 'command.goal' or permit.identity != identity
            or len(permit.owners) != 1 or permit.owners[0][0] != session_id
            or permit.goal_read_route is None or permit.goal_read_result is not None
            or not permit.revalidate()):
        raise SessionSharingDenied('Original Goal delivery permit unavailable')
    result.final_check()
    _delivery.set(replace(permit, goal_read_result=result))


def delivery_authorized():
    from .organization_auth import configured_authenticator
    if configured_authenticator() is None:
        return True
    permit = _delivery.get()
    return isinstance(permit, SessionRequestPermit) and permit.revalidate()


def bind_goal_mutation_delivery(session_id, identity, result):
    """Retain only the local Runtime receipt; never accept a wire callback."""
    from jiuwenswarm.runtime.native_goal_mutation import NativeGoalMutationDelivery
    if type(result) is not NativeGoalMutationDelivery:
        raise TypeError('Actual Native Goal mutation result required')
    if result.session_id != session_id or result.identity != identity:
        raise SessionSharingDenied('Original Goal mutation result differs')
    permit = _delivery.get()
    if permit is not None and (
            permit.method != 'command.goal' or permit.identity != identity
            or len(permit.owners) != 1 or permit.owners[0][0] != session_id
            or permit.goal_mutation_route is None or permit.goal_mutation_result is not None
            or not permit.revalidate()):
        raise SessionSharingDenied('Original Goal mutation delivery permit unavailable')
    if result.final_check() is not None:
        raise SessionSharingDenied('Goal mutation result check failed')
    if permit is None:  # A direct Runtime result does not authorize wire delivery.
        return
    if _delivery.get() is not permit or not permit.revalidate():
        raise SessionSharingDenied('Original Goal mutation delivery permit changed')
    _delivery.set(replace(permit, goal_mutation_result=result))


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
