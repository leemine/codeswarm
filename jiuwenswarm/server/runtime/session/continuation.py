"""Compile complete bounded shared text without allocating or starting a Session.

Run in the existing history IO context. No sharing lock spans history IO. The
returned immutable facts are not authorization leases: callers must revalidate
at commit, delivery, every Turn and resource use, including after waits.
"""
from __future__ import annotations

from dataclasses import asdict

from jiuwenswarm.governance.continuation import (
    ContinuationInput, ContinuationMessage, ContinuationProof, ContinuationSeed,
)
from jiuwenswarm.governance.contracts import AuthorizationDecision, TrustedIdentity
from jiuwenswarm.governance.session_sharing import SessionHistoryRange, SessionSharingDenied
from .shared_history import SharedHistoryLimit, read_shared_history_page, visible_conversation_text

MAX_SEED_BYTES = 1024 * 1024
MAX_SEED_MESSAGES = 1000
MAX_SOURCE_BYTES = 8 * 1024 * 1024
MAX_SOURCE_PAGES = 256


class ContinuationCompiler:
    def __init__(self, host, *, identity_resolver, project_authorizer):
        self.host = host
        self.identity_resolver = identity_resolver
        self.project_authorizer = project_authorizer

    def _identity(self):
        identity = self.identity_resolver()
        if not isinstance(identity, TrustedIdentity):
            raise SessionSharingDenied('authenticated continuation identity required')
        return identity

    def _target(self, request, identity):
        decision = self.project_authorizer.authorize(request.target_project_id, identity.actor_id, 'execute')
        if (not isinstance(decision, AuthorizationDecision) or decision.allowed is not True
                or (decision.project_id, decision.actor_id, decision.action)
                != (request.target_project_id, identity.actor_id, 'execute')
                or decision.revision < 1
                or decision.reason in {'legacy_default', 'legacy_unmanaged', 'orphan_owner'}):
            raise SessionSharingDenied('target project execute denied')
        return decision.revision

    def _capture(self, request):
        if not isinstance(request, ContinuationInput):
            raise TypeError('validated continuation input required')
        identity = self._identity()
        target_revision = self._target(request, identity)
        records = [record for record in self.host.store.list_for_actor(identity)
                   if record.get('session_id') == request.session_id
                   and record.get('share_id') == request.share_id
                   and record.get('target') == asdict(identity)]
        if len(records) != 1 or records[0].get('revision') != request.expected_revision:
            raise SessionSharingDenied('continuation share unavailable or changed')
        record = records[0]
        history = SessionHistoryRange(**record['history'])
        # Both decisions concern the exact same share. A second grant cannot
        # supply the missing action. Existing sidecar lock is reentrant.
        with self.host.store.guard(request.session_id, identity, 'view', history=history,
                                   share_id=request.share_id) as view:
            with self.host.store.guard(request.session_id, identity, 'execute', history=history,
                                       share_id=request.share_id) as execute:
                owner = self.host.store.registered_owner(request.session_id)
                source = self.host.resolve_source(request.session_id)
                if (owner is None or source is None or source.owner != owner[0]
                        or view.revision != request.expected_revision
                        or execute.revision != request.expected_revision
                        or record['owner_revision'] != owner[1]
                        or record['source_revision'] != source.revision):
                    raise SessionSharingDenied('continuation source changed')
                proof = ContinuationProof(request, identity, source.owner, owner[1], source.revision,
                    view.revision, history, record['expires_at'], source.expires_at,
                    TrustedIdentity(**record['grantor']), record['parent_share_id'],
                    record['parent_revision'], target_revision)
        if (self._identity() != identity or self._target(request, identity) != target_revision
                or self._identity() != identity):
            raise SessionSharingDenied('continuation identity or target changed')
        return proof

    def revalidate(self, proof):
        """Check original facts, never replace them with newly granted rights."""
        if not isinstance(proof, ContinuationProof) or self._capture(proof.request) != proof:
            raise SessionSharingDenied('continuation authority changed')

    def compile(self, request):
        proof = self._capture(request)
        if proof.history.end - proof.history.start > MAX_SOURCE_BYTES:
            raise SharedHistoryLimit('continuation source range exceeds limit')

        def authorized(scope):
            if scope != proof.history:
                return False
            self.revalidate(proof)
            return True

        messages = []
        total_bytes = 0
        handle = None
        for _ in range(MAX_SOURCE_PAGES):
            page = read_shared_history_page(proof.history, authorize=authorized,
                is_visible=visible_conversation_text, handle=handle, limit=100)
            for item in page.messages:
                message = ContinuationMessage(item['role'], item['content'])
                total_bytes += len(message.content.encode('utf-8'))
                if total_bytes > MAX_SEED_BYTES or len(messages) >= MAX_SEED_MESSAGES:
                    raise SharedHistoryLimit('continuation seed exceeds limit')
                messages.append(message)
            page.revalidate()
            handle = page.next_handle
            if handle is None:
                # Reader is newest-first; reverse the complete collected stream.
                self.revalidate(proof)
                seed = ContinuationSeed(proof, tuple(reversed(messages)))
                self.revalidate(proof)
                return seed
        raise SharedHistoryLimit('continuation source requires too many pages')
