"""Create an owned continuation using the existing Session provision transaction.

No Provider fork, imported credentials, scheduler, or secondary ACL is created.
The owner-sidecar publication is the durable business commit. A lost response
never permits an old prepare receipt to erase that committed Session.
"""
from __future__ import annotations

from contextvars import copy_context
from dataclasses import asdict, dataclass, field
from typing import Callable

from jiuwenswarm.governance.continuation import ContinuationInput
from jiuwenswarm.governance.continuation_publication import ContinuationPublication
from jiuwenswarm.governance.contracts import TrustedIdentity
from jiuwenswarm.governance.session_sharing import SessionSharingConflict, SessionSharingDenied
from jiuwenswarm.runtime.continuation_targets import ContinuationTargets
from jiuwenswarm.runtime.session_provisioner import SessionProvisionCommitTiming, SessionProvisionState
from jiuwenswarm.server.runtime.session import lifecycle
from jiuwenswarm.server.runtime.session.continuation import ContinuationCompiler
from jiuwenswarm.server.runtime.session.continuation_publication import parse_proof, read_seed, read_target_snapshot
from jiuwenswarm.server.runtime.session.history_io import run_history_io


@dataclass(frozen=True)
class ContinuedSession:
    """Safe result plus a private final-delivery guard; no seed or credentials."""
    session_id: str
    project_id: str
    project_dir: str
    work_mode: str
    mode: str
    execution_profile_id: str
    model_name: str
    title: str
    source_session_id: str
    share_id: str
    share_revision: int
    _check: Callable = field(repr=False, compare=False)

    def revalidate(self):
        self._check()

    def to_payload(self):
        return {'session_id': self.session_id, 'project_id': self.project_id,
                'project_dir': self.project_dir, 'work_mode': self.work_mode,
                'mode': self.mode, 'execution_profile_id': self.execution_profile_id,
                'model_name': self.model_name, 'title': self.title, 'persist_session': True,
                'continued_from': {'session_id': self.source_session_id,
                                   'share_id': self.share_id, 'revision': self.share_revision}}


@dataclass(frozen=True)
class ContinuationOptions:
    _payload: dict = field(repr=False)
    _check: Callable = field(repr=False, compare=False)

    def revalidate(self):
        self._check()

    def to_payload(self):
        from copy import deepcopy
        return deepcopy(self._payload)


async def continuation_options(runtime, params):
    runtime._require_started()
    host = runtime._organization_session_host
    if host is None:
        raise SessionSharingDenied('trusted continuation host required')
    check_identity = runtime._publication_identity_check(params)
    from jiuwenswarm.runtime.continuation_delivery import (
        continuation_options as collect, capture_continuation_options_delivery,
    )
    # Preserve the original live principal across any final-delivery Task.
    captured = copy_context()
    payload, check = collect(host, check_identity, params)
    final_check = capture_continuation_options_delivery(host, check_identity, params, payload)
    def validate():
        if runtime._closed:
            raise SessionSharingDenied('continuation Runtime closed')
        captured.run(check)
        captured.run(final_check)
    return ContinuationOptions(payload, validate)


def _existing(host, request, identity):
    """Read the original owner records; registration performs the atomic check."""
    matches = []
    with host._storage._locked():
        owners = host.store._section(host._storage._load())['owners']
        for session_id, record in owners.items():
            publication = record.get('continuation') if isinstance(record, dict) else None
            if not isinstance(publication, dict):
                continue
            raw = publication.get('proof')
            if (not isinstance(raw, dict) or raw.get('identity') != asdict(identity)
                    or not isinstance(raw.get('request'), dict)
                    or raw['request'].get('create_token') != request.create_token):
                continue
            proof = parse_proof(raw)
            if proof.request != request:
                raise SessionSharingConflict('creation token is bound to different continuation input')
            if record.get('retired') is not False or publication.get('state') != 'committed':
                raise SessionSharingConflict('continuation creation is pending or unavailable')
            matches.append(session_id)
    if len(matches) > 1:
        raise SessionSharingDenied('ambiguous continuation creation')
    return matches[0] if matches else None


async def continue_session(runtime, request: ContinuationInput):
    """Commit before returning; the adapter must still guard final delivery."""
    runtime._require_started()
    host = runtime._organization_session_host
    if host is None or not isinstance(request, ContinuationInput):
        raise SessionSharingDenied('trusted continuation host and validated input required')
    check_identity = runtime._publication_identity_check(request)
    identity = check_identity()
    if not isinstance(identity, TrustedIdentity):
        raise SessionSharingDenied('trusted continuation identity required')
    captured = copy_context()
    targets = ContinuationTargets(runtime)
    target = targets.select(request)
    from jiuwenswarm.runtime.continuation_execution import target_snapshot as snapshot_for
    target_snapshot = snapshot_for(target)

    compiler = ContinuationCompiler(host, identity_resolver=check_identity,
                                    project_authorizer=runtime._submission_guard._authorizer)

    def check_target():
        if check_identity() != identity or runtime._closed:
            raise SessionSharingDenied('continuation identity or Runtime changed')
        captured.run(targets.revalidate, target)

    def result_for(session_id):
        seed = read_seed(host, session_id, identity)
        if read_target_snapshot(host, session_id, identity) != target_snapshot:
            raise SessionSharingDenied('continuation target differs from original approval')
        compiler.revalidate(seed.proof)
        check_target()
        metadata = lifecycle.raw_metadata(session_id)
        owner_revision = host.owner_revision(session_id, identity)
        from jiuwenswarm.runtime.continuation_delivery import capture_continuation_delivery
        final_check = capture_continuation_delivery(host, check_identity, request, session_id)
        if (metadata.get('project_id') != request.target_project_id
                or metadata.get('execution_config_fingerprint') != target.execution_fingerprint
                or metadata.get('execution_profile_id') != request.execution_profile_id
                or metadata.get('model') != request.model_name
                or metadata.get('persist_session') is not True):
            raise SessionSharingDenied('continuation target changed')

        def check_delivery():
            captured.run(final_check)
            check_target()
            compiler.revalidate(seed.proof)
            if host.owner_revision(session_id, identity) != owner_revision:
                raise SessionSharingDenied('continuation owner changed before delivery')
        return ContinuedSession(session_id, request.target_project_id, metadata['project_dir'],
            metadata['work_mode'], metadata['mode'], request.execution_profile_id,
            request.model_name, metadata.get('title', ''), request.session_id, request.share_id,
            request.expected_revision, check_delivery)

    existing = _existing(host, request, identity)
    if existing is not None:
        result = result_for(existing)
        await runtime._reconcile_continuation_result(existing)
        result.revalidate()
        return result
    seed = await run_history_io(compiler.compile, request)
    check_target()
    compiler.revalidate(seed.proof)
    prepared = None
    with ContinuationPublication(host, compiler, seed, target.execution_fingerprint,
                                 target_snapshot=target_snapshot) as publication:
        publication._before_runtime_commit = check_target
        try:
            prepared = await runtime.prepare_session_create(target.provision_input)
            check_target()
            compiler.revalidate(seed.proof)
            from jiuwenswarm.server.runtime.session.session_metadata import flush_pending_writes
            if await run_history_io(flush_pending_writes) is not True:
                raise SessionSharingDenied('continuation metadata is not durable')
            check_target()
            runtime.validate_session_provision_for_delivery(prepared)
            publication.write_seed()
            check_target()
            # The original Provisioner commit hook publishes the durable seed
            # after Runtime's commit-lock/identity checks, before any response.
            await runtime.commit_session_provision(
                prepared, timing=SessionProvisionCommitTiming.BEFORE_RESULT_DELIVERY)
            check_target()
            return result_for(prepared.result.session_id)
        except BaseException as primary:
            if prepared is not None and prepared.state is SessionProvisionState.PREPARED:
                try:
                    await runtime.abort_session_provision(prepared)
                except BaseException as cleanup:
                    primary.add_note('continuation preparation compensation incomplete: ' + type(cleanup).__name__)
            raise
