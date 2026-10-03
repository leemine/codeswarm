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
from .base import GatewayAdapter, build_error_response

_FIELDS = {
    'session.share.list': {'session_id'},
    'session.share.create': {'session_id', 'target_actor', 'actions', 'history_scope', 'expires_at', 'parent_share_id'},
    'session.share.update': {'session_id', 'share_id', 'actions', 'expires_at', 'expected_revision'},
    'session.share.revoke': {'session_id', 'share_id', 'expected_revision'},
}


class SessionSharingAdapter(GatewayAdapter):
    methods = frozenset(_FIELDS)

    def __init__(self, store: SessionSharingStore, *, identity_resolver: Callable,
                 target_resolver: Callable[[TrustedIdentity, str], TrustedIdentity | None],
                 compile_history: Callable[[str, TrustedIdentity, str | None], SessionHistoryRange]):
        self.store = store
        self.identity_resolver = identity_resolver
        self.target_resolver = target_resolver
        self.compile_history = compile_history

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

    def _dispatch(self, request):
        method = getattr(request.req_method, 'value', request.req_method)
        params = request.params
        if method not in self.methods or not isinstance(params, dict) or set(params) - _FIELDS[method]:
            raise ValueError('invalid sharing request fields')
        identity = self._identity(request)
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
                                      expires_at=expires_at, parent_share_id=parent_id)
            result = {'share': self._project(record, identity)}
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
                                            expires_at=expires_at, expected_revision=revision)
                result = {'share': self._project(updated, identity)}
            else:
                result = {'share_id': share_id, 'revision': self.store.revoke(share_id, identity, expected_revision=revision)}
        # Queued requests and long compiles cannot retain a stale credential.
        if self._identity(request) != identity:
            raise SessionSharingDenied('identity changed')
        return AgentResponse(request_id=request.request_id, channel_id=request.channel_id,
                             ok=True, payload=result, metadata=request.metadata)

    async def handle(self, request):
        try:
            return await run_history_io(self._dispatch, request)
        except SessionSharingConflict:
            return build_error_response(request, 'Sharing revision changed; refresh and retry.', code='CONFLICT')
        except (SessionSharingDenied, PermissionError):
            return build_error_response(request, 'Sharing authorization denied.', code='FORBIDDEN')
        except (ValueError, TypeError):
            return build_error_response(request, 'Invalid sharing request.', code='BAD_REQUEST')
        except Exception:
            return build_error_response(request, 'Sharing authority unavailable.', code='FORBIDDEN')
