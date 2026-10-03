"""Read-only shared history with private, identity-bound pagination handles."""
from __future__ import annotations

import secrets
import threading
import time
from collections import OrderedDict
from dataclasses import asdict

from jiuwenswarm.common.schema.agent import AgentResponse
from jiuwenswarm.governance.contracts import TrustedIdentity
from jiuwenswarm.governance.session_sharing import SessionHistoryRange, SessionSharingDenied
from jiuwenswarm.server.runtime.session.history_io import run_history_io
from jiuwenswarm.server.runtime.session.shared_history import read_shared_history_page
from .base import GatewayAdapter, build_error_response


class SharedHistoryAdapter(GatewayAdapter):
    methods = frozenset({'session.share.history.get'})

    def __init__(self, store, *, identity_resolver, clock=time.monotonic):
        self.store, self.identity_resolver, self.clock = store, identity_resolver, clock
        self._handles = OrderedDict()
        self._lock = threading.Lock()

    @staticmethod
    def _visible(record):
        return (record.get('role') in {'user', 'assistant'}
                and isinstance(record.get('content'), str) and bool(record['content'].strip()))

    def _read(self, request):
        params = request.params
        if not isinstance(params, dict) or set(params) - {'session_id', 'share_id', 'cursor', 'limit'}:
            raise ValueError('invalid shared history request')
        identity = self.identity_resolver(request)
        if not isinstance(identity, TrustedIdentity):
            raise SessionSharingDenied('authenticated identity required')
        session_id, share_id = params.get('session_id'), params.get('share_id')
        limit = params.get('limit', 50)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('invalid history page size')
        record = next((r for r in self.store.list_for_actor(identity)
                       if r['session_id'] == session_id and r['share_id'] == share_id
                       and r.get('target') == asdict(identity)), None)
        if record is None:
            raise SessionSharingDenied('shared history unavailable')
        scope = SessionHistoryRange(**record['history'])
        revision = record['revision']

        def authorized(candidate):
            if self.identity_resolver(request) != identity:
                return False
            decision = self.store.authorize(session_id, identity, 'view', history=candidate, share_id=share_id)
            return decision.allowed is True and decision.revision == revision

        handle = None
        cursor = params.get('cursor')
        if cursor is not None:
            if not isinstance(cursor, str) or not cursor or len(cursor) > 100:
                raise ValueError('invalid history cursor')
            with self._lock:
                saved = self._handles.get(cursor)
            if saved is None or saved[:5] != (identity, session_id, share_id, revision, scope) or saved[5] <= self.clock():
                raise SessionSharingDenied('history cursor unavailable')
            handle = saved[6]
        page = read_shared_history_page(scope, authorize=authorized, is_visible=self._visible,
                                        handle=handle, limit=limit)
        next_cursor = None
        if page.next_handle is not None:
            next_cursor = secrets.token_urlsafe(32)
            with self._lock:
                now = self.clock()
                for key in list(self._handles):
                    if self._handles[key][5] <= now:
                        del self._handles[key]
                while len(self._handles) >= 1024:
                    self._handles.popitem(last=False)
                self._handles[next_cursor] = (identity, session_id, share_id, revision, scope, now + 300, page.next_handle)
        # No attachment URLs, private paths, tool controls or activity metadata
        # are projected. Downloads require a separate asset/range authority.
        messages = [{'role': item['role'], 'content': item['content'],
                     **({'id': item['id']} if isinstance(item.get('id'), str) else {})}
                    for item in page.messages]
        page.revalidate()
        response = AgentResponse(request_id=request.request_id, channel_id=request.channel_id,
            ok=True, payload={'session_id': session_id, 'share_id': share_id, 'messages': messages,
                              'next_cursor': next_cursor, 'read_only': True}, metadata=request.metadata)
        # Private host callback, never encoded into protocol metadata. Host calls
        # after acquiring the actual send lock, immediately before delivery.
        response._delivery_guard = page.revalidate
        return response

    async def handle(self, request):
        try:
            return await run_history_io(self._read, request)
        except (SessionSharingDenied, PermissionError):
            return build_error_response(request, 'Shared history authorization denied.', code='FORBIDDEN')
        except (ValueError, TypeError):
            return build_error_response(request, 'Invalid shared history request.', code='BAD_REQUEST')
        except Exception:
            return build_error_response(request, 'Shared history unavailable.', code='FORBIDDEN')
