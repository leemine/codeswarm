"""Organization sharing RPCs; host identity/directory/history are mandatory.

Transport registration and bounded history delivery remain the host's concern.
No caller may provide authority, subject, owner mapping, or a history cursor.
"""
from __future__ import annotations

from dataclasses import asdict
from typing import Callable

from jiuwenswarm.common.schema.agent import AgentResponse
from jiuwenswarm.governance.contracts import TrustedIdentity
from jiuwenswarm.governance.resources import valid_expiry
from jiuwenswarm.governance.session_sharing import (
    SHARING_ACTIONS, SessionHistoryRange, SessionSharingConflict, SessionSharingDenied,
)
from jiuwenswarm.server.runtime.session.history_io import run_history_io
from jiuwenswarm.server.runtime.session.session_sharing import SessionSharingStore
from jiuwenswarm.server.runtime.session.sharing_audit import SharingAuditContext, SharingAuditWriteResult, audit_query_params
from .base import GatewayAdapter, build_error_response

_FIELDS = {
    'session.share.audit.list': {'session_id', 'limit'},
    'session.share.list': {'session_id'},
    'session.share.create': {'session_id', 'target_actor', 'actions', 'history_scope', 'expires_at', 'parent_share_id'},
    'session.share.update': {'session_id', 'share_id', 'actions', 'expires_at', 'expected_revision'},
    'session.share.revoke': {'session_id', 'share_id', 'expected_revision'},
}


class SessionSharingAdapter(GatewayAdapter):
    methods = frozenset(_FIELDS)

    def __init__(self, store: SessionSharingStore, *, identity_resolver: Callable,
                 target_resolver: Callable[[TrustedIdentity, str], TrustedIdentity | None],
                 compile_history: Callable[[str, TrustedIdentity, str | None], SessionHistoryRange],
                 after_mutation=None, audit_owner_revision=None):
        self.store = store
        self.identity_resolver = identity_resolver
        self.target_resolver = target_resolver
        self.compile_history = compile_history
        self.after_mutation = after_mutation
        self.audit_owner_revision = audit_owner_revision

    def _identity(self, request):
        identity = self.identity_resolver(request)
        if not isinstance(identity, TrustedIdentity):
            raise SessionSharingDenied('authenticated identity required')
        return identity

    @staticmethod
    def _text(params, field):
        value = params.get(field)
        if not isinstance(value, str) or not value or value.strip() != value:
            raise ValueError('invalid sharing request')
        return value

    @staticmethod
    def _bounds(params):
        actions = params.get('actions')
        if (not isinstance(actions, list) or not actions or any(not isinstance(a, str) for a in actions)
                or not set(actions) <= SHARING_ACTIONS or len(set(actions)) != len(actions)
                or 'expires_at' not in params or not valid_expiry(params['expires_at'])):
            raise ValueError('invalid sharing bounds')
        return actions, params['expires_at']

    def _managed(self, session_id, identity):
        mapping = self.store.registered_owner(session_id)
        if mapping and mapping[0] == identity:
            return self.store.list_for_session(session_id, identity)
        return [record for record in self.store.list_for_actor(identity)
                if record['session_id'] == session_id and (record.get('grantor') == asdict(identity) or record.get('state') == 'unavailable')]

    def _project(self, record, identity):
        mine = record.get('grantor') == asdict(identity) or record.get('state') == 'unavailable'
        mapping = self.store.registered_owner(record['session_id'])
        owner = bool(mapping and mapping[0] == identity)
        state = record.get('state', 'active')
        result = {key: record[key] for key in ('share_id', 'session_id', 'revision')}
        result.update(state=state, can_update=mine and state == 'active', can_revoke=mine or owner)
        if state == 'active':
            result.update(target_actor=record['target']['actor_id'], grantor_actor=record['grantor']['actor_id'],
                          actions=list(record['actions']), expires_at=record['expires_at'],
                          history_scope='fixed_snapshot')
        return result

    def _audit_query(self, request):
        from jiuwenswarm.governance.organization_auth import AuthenticatedPrincipal, current_principal
        from jiuwenswarm.governance.session_boundary import SessionRequestPermit, _delivery

        session_id, limit = audit_query_params(request.params)
        identity = self._identity(request)
        principal, permit = current_principal(), _delivery.get()
        revision_resolver = self.audit_owner_revision
        if (not isinstance(principal, AuthenticatedPrincipal) or not callable(revision_resolver)
                or type(permit) is not SessionRequestPermit or getattr(permit.host, 'store', None) is not self.store
                or permit.identity != identity or permit.method != 'session.share.audit.list'
                or len(permit.owners) != 1 or permit.owners[0][0] != session_id
                or permit.share is not None or permit.cleanup is not None or permit.workspace_download is not None):
            raise SessionSharingDenied('original owner audit permit required')
        revision = permit.owners[0][1]
        if type(revision) is not int or revision < 1:
            raise SessionSharingDenied('current owner revision required')
        original_request = (request.request_id, request.channel_id, request.session_id,
                            getattr(request.req_method, 'value', request.req_method), dict(request.params))

        def check():
            current = (request.request_id, request.channel_id, request.session_id,
                       getattr(request.req_method, 'value', request.req_method), request.params)
            if (current != original_request or current[3] != 'session.share.audit.list'
                    or (request.session_id is not None and request.session_id != session_id)
                    or _delivery.get() is not permit or current_principal() is not principal
                    or principal.identity() != identity or self._identity(request) != identity
                    or not permit.revalidate() or revision_resolver(session_id, identity) != revision):
                raise SessionSharingDenied('original owner audit request changed')
            return revision

        check()
        payload = self.store.query_audit(session_id, identity, owner_guard=check, limit=limit)
        response = AgentResponse(request_id=request.request_id, channel_id=request.channel_id,
                                 ok=True, payload=payload)
        response._delivery_guard = check
        return response

    def _dispatch(self, request):
        method = getattr(request.req_method, 'value', request.req_method)
        params = request.params
        if method not in self.methods or not isinstance(params, dict) or set(params) - _FIELDS[method]:
            raise ValueError('invalid sharing request fields')
        if method == 'session.share.audit.list':
            return self._audit_query(request)
        identity = self._identity(request)
        audit_results = []
        audit_context = (SharingAuditContext(identity, request.request_id, method)
                         if method != 'session.share.list' else None)
        mutation = None
        if method == 'session.share.list':
            records = (self.store.list_for_session(self._text(params, 'session_id'), identity)
                       if 'session_id' in params else self.store.list_for_actor(identity))
            result = {'shares': [self._project(record, identity) for record in records]}
        elif method == 'session.share.create':
            session_id, target_actor = self._text(params, 'session_id'), self._text(params, 'target_actor')
            actions, expires_at = self._bounds(params)
            if params.get('history_scope') != 'current_snapshot':
                raise ValueError('history must be compiled by the host')
            parent_id = self._text(params, 'parent_share_id') if 'parent_share_id' in params else None
            # Authorize before compiling or looking up target directory entries.
            if parent_id is None:
                self.store.list_for_session(session_id, identity)
            else:
                candidates = self.store.list_for_actor(identity)
                if not any(record['share_id'] == parent_id and record['session_id'] == session_id
                           and record.get('target') == asdict(identity) and 'manage' in record.get('actions', [])
                           for record in candidates):
                    raise SessionSharingDenied('delegation denied')
            target = self.target_resolver(identity, target_actor)
            if (not isinstance(target, TrustedIdentity) or target.authority != identity.authority
                    or target.actor_id != target_actor):
                raise SessionSharingDenied('sharing target unavailable')
            history = self.compile_history(session_id, identity, parent_id)
            if self._identity(request) != identity:
                raise SessionSharingDenied('identity changed')
            record = self.store.grant(session_id, identity, target, actions=actions, history=history,
                                      expires_at=expires_at, parent_share_id=parent_id,
                                      audit_context=audit_context, audit_result=audit_results.append)
            result = {'share': self._project(record, identity)}
            mutation = {'committed': True, 'method': method, 'session_id': session_id,
                        'share_id': record['share_id'], 'revision': record['revision']}
        else:
            session_id, share_id = self._text(params, 'session_id'), self._text(params, 'share_id')
            revision = params.get('expected_revision')
            if type(revision) is not int or revision < 1:
                raise ValueError('expected revision required')
            record = next((r for r in self._managed(session_id, identity) if r['share_id'] == share_id), None)
            if record is None:
                raise SessionSharingDenied('share unavailable')
            if self._identity(request) != identity:
                raise SessionSharingDenied('identity changed')
            if method == 'session.share.update':
                actions, expires_at = self._bounds(params)
                if record.get('state') != 'active' or record.get('grantor') != asdict(identity):
                    raise SessionSharingDenied('share update denied')
                updated = self.store.revise(share_id, identity, actions=actions,
                                            history=SessionHistoryRange(**record['history']),
                                            expires_at=expires_at, expected_revision=revision,
                                            audit_context=audit_context, audit_result=audit_results.append)
                result = {'share': self._project(updated, identity)}
            else:
                result = {'share_id': share_id, 'revision': self.store.revoke(share_id, identity,
                    expected_revision=revision, audit_context=audit_context, audit_result=audit_results.append)}
            mutation = {'committed': True, 'method': method, 'session_id': session_id,
                        'share_id': share_id, 'revision': revision + 1}
        # Queued requests and long compiles cannot retain a stale credential.
        if self._identity(request) != identity:
            raise SessionSharingDenied('identity changed')
        if mutation is not None:
            if len(audit_results) != 1 or type(audit_results[0]) is not SharingAuditWriteResult:
                raise SessionSharingDenied('sharing audit result unavailable')
            result['audit'] = asdict(audit_results[0])
        response = AgentResponse(request_id=request.request_id, channel_id=request.channel_id,
                                 ok=True, payload=result, metadata=request.metadata)
        response._sharing_mutation = mutation  # Private original committed facts, never request params.
        return response

    async def handle(self, request):
        try:
            result = await run_history_io(self._dispatch, request)
            guard = getattr(result, '_delivery_guard', None)
            if guard is not None:
                guard()
            method = getattr(request.req_method, 'value', request.req_method)
            if self.after_mutation is not None and method in {'session.share.update', 'session.share.revoke'}:
                try:
                    await self.after_mutation()
                except Exception:
                    return build_error_response(request,
                        'Sharing changed; execution exit remains unconfirmed. Refresh; do not repeat the change.',
                        code='EXIT_UNCONFIRMED', extra={
                            'mutation': result._sharing_mutation,
                            'audit': result.payload['audit'], 'exit_confirmed': False,
                        })
            return result
        except SessionSharingConflict:
            return build_error_response(request, 'Sharing revision changed; refresh and retry.', code='CONFLICT')
        except (SessionSharingDenied, PermissionError):
            return build_error_response(request, 'Sharing authorization denied.', code='FORBIDDEN')
        except (ValueError, TypeError):
            return build_error_response(request, 'Invalid sharing request.', code='BAD_REQUEST')
        except Exception:
            return build_error_response(request, 'Sharing authority unavailable.', code='FORBIDDEN')
