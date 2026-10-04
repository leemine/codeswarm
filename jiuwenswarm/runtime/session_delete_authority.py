"""One original authenticated Single deletion through the existing lifecycle.

No new queue or persistent transaction state: the original owner receipt and
Session lifecycle retain intent, while this object pins one live Runtime call.
"""
from __future__ import annotations

import json
from contextvars import copy_context

from jiuwenswarm.common.mode_matrix import is_team_mode
from jiuwenswarm.governance.session_boundary import admit_session_request
from jiuwenswarm.governance.session_sharing import SessionSharingDenied
from jiuwenswarm.runtime.session.model import SessionCloseTimeoutError
from jiuwenswarm.server.runtime.session import lifecycle as lc
from jiuwenswarm.server.runtime.session.deletion_receipt import DeletionAuditPending
from jiuwenswarm.server.runtime.session.sharing_audit import SharingAuditContext


class OwnedSessionDeletion:
    def __init__(self, runtime, request, permit=None):
        self.runtime = runtime
        self.request = request
        self.host = runtime._organization_session_host
        self.session_id = request.session_id
        self.channel_id = request.channel_id
        self._quiesced = False
        self._context = copy_context()
        self._identity = self._context.run(runtime._governance_identity, request)
        self._wire = self._request_facts()
        self._audit_context = SharingAuditContext(
            self._identity, request.request_id or None, "session.delete")
        self._resolver = lambda: self._context.run(runtime._governance_identity, request)
        self._permit = permit or admit_session_request(
            'session.delete', request.params or {}, identity_resolver=self._resolver,
            host=self.host, envelope_session=self.session_id,
        )
        if not self._permit.allows_cleanup('session.delete', request.params or {},
                                           self._identity, self.session_id):
            raise SessionSharingDenied('exact original delete permit required')
        self.receipt = getattr(self._permit, 'deletion_receipt', None)
        self._capture = (None if self.receipt is not None else self.host.capture_deletion(
            self.session_id, self._identity, self._permit))
        self.descriptor = dict((self.receipt or self._capture).descriptor)
        # Routing channels authenticate the caller; only the durable original
        # binding selects the cached execution that deletion must stop.
        self.channel_id = self.descriptor.get('channel_id')
        if not isinstance(self.channel_id, str) or not self.channel_id.strip():
            raise SessionSharingDenied('original deletion channel binding required')
        if (is_team_mode(self.descriptor.get('mode')) or self.descriptor.get('team_name')
                or self.session_id.startswith(('cron_', 'heartbeat_'))):
            raise SessionSharingDenied('owned deletion currently requires an ordinary Single Session')
        snapshot = runtime._session_coordinator.snapshot_session(self.session_id)
        self.generation = snapshot.generation if snapshot else None
        self.executions = frozenset(item.execution_id for item in snapshot.executions
                                    if not item.state.terminal) if snapshot else frozenset()
        self.check_before_begin()

    def _request_facts(self):
        request = self.request
        return (request.request_id, request.session_id, request.channel_id,
                request.req_method.value if request.req_method else '',
                json.dumps(request.params or {}, sort_keys=True, separators=(',', ':')))

    def _check_original(self):
        if (self.runtime._closed or self._request_facts() != self._wire
                or self._resolver() != self._identity):
            raise SessionSharingDenied('original delete request changed')
        current = self.runtime._session_coordinator.snapshot_session(self.session_id)
        if ((current.generation if current else None) != self.generation
                or current is not None and any(not item.state.terminal
                    and item.execution_id not in self.executions for item in current.executions)):
            raise SessionSharingDenied('delete cannot select a newer execution')

    def check_before_begin(self):
        self._check_original()
        if self.receipt is not None:
            self.host.check_deletion(self.receipt, for_admission=True)
        elif not self._permit.revalidate():
            raise SessionSharingDenied('original delete permission changed before lock acquisition')

    def enter(self, operation):
        self._check_original()
        if self.receipt is None:
            self.receipt = self.host.begin_deletion(
                self._capture, operation, audit_context=self._audit_context)
        else:
            try:
                self.receipt = self.host.adopt_deletion(
                    self.receipt, operation, audit_context=self._audit_context)
            except DeletionAuditPending:
                # Claiming the SAME persisted operation may advance its owner
                # generation. Recover that exact nonce, never another deletion.
                original = json.loads(self.receipt._record_json)
                recovered = self.host.resume_deletion(
                    self.session_id, self._identity, identity_resolver=self._resolver)
                current = json.loads(recovered._record_json)
                if {k: v for k, v in original.items() if k != 'audit_pending'} != {
                        k: v for k, v in current.items() if k != 'audit_pending'}:
                    raise SessionSharingDenied('original deletion receipt changed')
                self.receipt = recovered
                self.repair_audit()
        self._check_original()
        self.host.check_deletion(self.receipt, for_admission=True)

    def check(self):
        self._check_original()
        if self.receipt is None:
            raise SessionSharingDenied('delete has not entered its original lifecycle')
        self.host.check_deletion(self.receipt)

    async def quiesce(self, **_):
        self.check()
        if self._quiesced:
            return
        lc.assert_runtime_owner(self.session_id)
        manager, runtime = self.runtime._agent_manager, self.runtime
        if self.generation is None:
            if (manager.get_agent_for_session_nowait(self.channel_id, self.session_id) is not None
                    or runtime.is_session_running(self.session_id)):
                raise SessionSharingDenied('existing execution has no original deletion generation')
            self._quiesced = True
            return

        async def release():
            self.check()
            await manager.release_subagent_runtime_for_session(
                channel_id=self.channel_id, session_id=self.session_id, reason='session_deleted')
            self.check()
            await manager.stop_existing_session_runtime(channel_id=self.channel_id, session_id=self.session_id)
            self.check()
            await manager.cleanup_session_runtime(channel_id=self.channel_id, session_id=self.session_id)
            self.check()
            await runtime._forget_agent_execution_owner(channel_id=self.channel_id, session_id=self.session_id)
            self.check()

        closed = await runtime._session_coordinator.close_session(
            self.session_id, generation=self.generation, wait_timeout=10, release_resources=release)
        if closed.timed_out:
            raise SessionCloseTimeoutError(self.session_id, closed.timed_out)
        self.check()
        lc.release_runtime(self.session_id)
        self._quiesced = True

    async def disposed(self, **_):
        # The original Coordinator resource-close callback has already joined
        # the exact existing Provider and removed its binding, without new IO.
        self.check()

    def commit_owner(self):
        self.check()
        try:
            self.host.commit_deletion(self.receipt, audit_context=self._audit_context)
        except DeletionAuditPending as exc:
            if exc.receipt is not self.receipt or not self.host.confirms_deletion(self.receipt):
                raise
            self.host.deletion_audit_pending(self.receipt)
        except Exception:
            # An atomic replace can succeed before a later IO error. Reconcile
            # both the actual retirement and its strict persisted audit status.
            if not self.host.confirms_deletion(self.receipt):
                raise
            self.host.deletion_audit_pending(self.receipt)
        self._check_original()
        self.host.check_deletion(self.receipt, for_admission=True)

    def repair_audit(self):
        """Audit-only retry from the original persisted receipt/context."""
        self._check_original()
        if not self.host.confirms_deletion(self.receipt):
            raise SessionSharingDenied('original deletion commit is unconfirmed')
        if self.host.deletion_audit_pending(self.receipt):
            try:
                self.host.supplement_deletion_audit(self.receipt)
            except DeletionAuditPending as exc:
                if exc.receipt is not self.receipt:
                    raise
            except Exception:
                # A failed save is never treated as a successful audit write.
                # Only the exact durable receipt can resolve its current state.
                self.host.deletion_audit_pending(self.receipt)
        self._check_original()

    def acknowledge(self):
        self._check_original()
        self.host.check_deletion(self.receipt, for_admission=True)
        state = lc.state('session', self.session_id)
        operation = state.get('operation') or {}
        if (not self.host.confirms_deletion(self.receipt)
                or state.get('deleted') is not True or operation.get('status') != 'completed'
                or (operation.get('result') or {}).get('session_id') != self.session_id):
            raise SessionSharingDenied('original deletion commit is unconfirmed')
        return {'session_id': self.session_id, 'ok': True, 'deleted': True, 'exit_confirmed': True,
                'audit_pending': self.host.deletion_audit_pending(self.receipt)}


def capture_deletion(runtime, request, permit=None):
    if runtime._organization_session_host is None:
        return None
    existing = getattr(request, '_deletion_authority', None)
    if existing is not None:
        if not isinstance(existing, OwnedSessionDeletion) or existing.runtime is not runtime or existing.request is not request:
            raise SessionSharingDenied('deletion authority belongs to another request')
        existing.check_before_begin()
        return existing
    authority = OwnedSessionDeletion(runtime, request, permit)
    request._deletion_authority = authority
    return authority
