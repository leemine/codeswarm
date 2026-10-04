"""Owner-sidecar continuation publication; no independent ACL or scheduler.

Owner checks never read seed/history or save. Seed IO is bounded and outside
sidecar locks. Runtime owns rollback, binding, resource authority and execution.
"""
from __future__ import annotations

import json
import os
import stat
from contextvars import ContextVar
from dataclasses import asdict

from jiuwenswarm.governance.continuation import (
    ContinuationInput, ContinuationMessage, ContinuationProof, ContinuationSeed,
)
from jiuwenswarm.governance.continuation_publication import current_scope
from jiuwenswarm.governance.contracts import TrustedIdentity
from jiuwenswarm.governance.resources import valid_expiry
from jiuwenswarm.governance.session_sharing import SessionHistoryRange, SessionSharingDenied, SessionSharingConflict
from . import lifecycle
from .continuation import ContinuationCompiler, MAX_SEED_BYTES, MAX_SEED_MESSAGES
from .sharing_audit import (
    SharingAuditBounds, SharingAuditFacts, SharingAuditContext, AuditResultCallback,
    append_sharing_audit, audit_context as checked_audit_context, notify_audit_result, source_project,
)

MAX_SEED_FILE_BYTES = 8 * 1024 * 1024
MAX_SOURCE_DEPTH = 8
_CHAIN = ContextVar('continuation_source_chain', default=())
_FIELDS = {'schema_version', 'state', 'publication_id', 'proof', 'seed_digest',
           'config_fingerprint', 'execution_profile_id', 'target_snapshot'}


def _exact(value, keys):
    if type(value) is not dict or set(value) != set(keys):
        raise SessionSharingDenied('invalid persisted continuation fields')
    return value


def _positive(value):
    if type(value) is not int or not 1 <= value < 2 ** 256:
        raise SessionSharingDenied('invalid continuation revision')
    return value


def _hex(value):
    if not isinstance(value, str) or len(value) != 64 or any(c not in '0123456789abcdef' for c in value):
        raise SessionSharingDenied('invalid continuation digest or nonce')
    return value


def _identity(value):
    _exact(value, {'actor_id', 'subject_id', 'authority'})
    if any(not isinstance(v, str) or len(v) > 1024 for v in value.values()):
        raise SessionSharingDenied('invalid persisted continuation identity')
    return TrustedIdentity(**value)


def parse_proof(value):
    """Strict host record parser, never a request authorization API."""
    try:
        return _parse_proof(value)
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise SessionSharingDenied('malformed persisted continuation proof') from exc


def _parse_proof(value):
    _exact(value, ContinuationProof.__dataclass_fields__)
    _exact(value['request'], ContinuationInput.__dataclass_fields__)
    request = ContinuationInput.from_wire(value['request'])
    _exact(value['history'], SessionHistoryRange.__dataclass_fields__)
    history = SessionHistoryRange(**value['history'])
    if history.session_id != request.session_id or value['share_revision'] != request.expected_revision:
        raise SessionSharingDenied('continuation source mismatch')
    for field in ('owner_revision', 'source_revision', 'share_revision', 'target_revision'):
        _positive(value[field])
    for field in ('expires_at', 'source_expires_at'):
        if not valid_expiry(value[field]):
            raise SessionSharingDenied('invalid continuation expiry')
    parent = value['parent_share_id']
    if parent is None:
        if value['parent_revision'] is not None:
            raise SessionSharingDenied('invalid continuation parent')
    elif not isinstance(parent, str) or not parent or len(parent) > 200 or parent.strip() != parent:
        raise SessionSharingDenied('invalid continuation parent')
    else:
        _positive(value['parent_revision'])
    return ContinuationProof(request, _identity(value['identity']), _identity(value['source_owner']),
        value['owner_revision'], value['source_revision'], value['share_revision'], history,
        value['expires_at'], value['source_expires_at'], _identity(value['grantor']),
        parent, value['parent_revision'], value['target_revision'])


def validate_target_snapshot(value):
    """Strict nonsecret facts approved before allocation; never rebuilt from defaults."""
    _exact(value, {'model_binding_fingerprint', 'model_entry_fingerprint',
                   'project_dir', 'work_mode', 'mode'})
    for key in ('model_binding_fingerprint', 'model_entry_fingerprint'):
        _hex(value[key])
    from pathlib import Path
    root = value['project_dir']
    if (not isinstance(root, str) or not root or not Path(root).is_absolute()
            or '${' in root or '\x00' in root or len(root) > 32768
            or value['work_mode'] not in ('work', 'code')
            or value['mode'] != 'agent.' + value['work_mode'] + '.normal'):
        raise SessionSharingDenied('invalid continuation target snapshot')
    return dict(value)


def _publication(value):
    _exact(value, _FIELDS)
    if type(value['schema_version']) is not int or value['schema_version'] != 1 or value['state'] not in ('pending', 'committed'):
        raise SessionSharingDenied('unsupported continuation publication')
    for field in ('publication_id', 'seed_digest', 'config_fingerprint'):
        _hex(value[field])
    validate_target_snapshot(value['target_snapshot'])
    proof = parse_proof(value['proof'])
    if value['execution_profile_id'] != proof.request.execution_profile_id:
        raise SessionSharingDenied('continuation profile mismatch')
    return proof


def _scope_record(scope):
    return {'schema_version': 1, 'state': 'pending', 'publication_id': scope.publication_id,
            'proof': asdict(scope.seed.proof), 'seed_digest': scope.seed.digest,
            'config_fingerprint': scope.config_fingerprint, 'target_snapshot': dict(scope.target_snapshot),
            'execution_profile_id': scope.seed.proof.request.execution_profile_id}


def prepare_registration(host, session_id, owner, project_id, *, previous, epoch, owners):
    scope = current_scope()
    if scope is None:
        return None
    _safe_platform()
    scope._require()
    if (scope.host is not host or scope.seed.proof.identity != owner
            or scope.seed.proof.request.target_project_id != project_id
            or previous != 0 or epoch != 1 or session_id == scope.seed.proof.request.session_id
            or (scope.session_id is not None and scope.session_id != session_id)):
        raise SessionSharingDenied('continuation requires one fresh owned target')
    # The caller holds the original sidecar lock and passes its current data
    # snapshot. A token tombstone survives both commit and owner retirement.
    for registered in owners.values():
        if not isinstance(registered, dict) or 'continuation' not in registered:
            continue
        previous_proof = _publication(registered['continuation'])
        if (previous_proof.identity == owner
                and previous_proof.request.create_token == scope.seed.proof.request.create_token):
            raise SessionSharingConflict('continuation create token already reserved')
    record = _scope_record(scope)
    _publication(record)
    scope.session_id = session_id
    return record


def _metadata(session_id, publication, proof):
    from jiuwenswarm.common.mode_matrix import deprecate_mode, is_single_agent_mode
    from jiuwenswarm.runtime.request import resolve_agent_request_mode

    metadata = lifecycle.raw_metadata(session_id)
    target = publication['target_snapshot']
    # Original history may persist the equivalent Web mode alias. Compare the
    # existing execution semantics without relaxing the fixed Work/Code profile.
    same_mode = (type(metadata.get('mode')) is str and bool(metadata['mode'])
                 and is_single_agent_mode(deprecate_mode(metadata['mode']))
                 and resolve_agent_request_mode(metadata['mode'], work_mode=metadata.get('work_mode'))[:2]
                 == resolve_agent_request_mode(target['mode'], work_mode=target['work_mode'])[:2])
    if (metadata.get('project_id') != proof.request.target_project_id
            or metadata.get('execution_profile_id') != publication['execution_profile_id']
            or metadata.get('execution_config_fingerprint') != publication['config_fingerprint']
            or metadata.get('model', '') != proof.request.model_name
            or any(metadata.get(key) != publication['target_snapshot'][key]
                   for key in ('project_dir', 'work_mode')) or not same_mode):
        raise SessionSharingDenied('continuation target configuration changed')


def guard(host, session_id, record, owner, source):
    if 'continuation' not in record:
        return
    key = (str(host._storage.path), session_id)
    chain = _CHAIN.get()
    if key in chain or len(chain) >= MAX_SOURCE_DEPTH:
        raise SessionSharingDenied('continuation source cycle or depth exceeded')
    token = _CHAIN.set((*chain, key))
    try:
        publication = record['continuation']
        proof = _publication(publication)
        if proof.identity != owner or proof.request.target_project_id != source['project_id']:
            raise SessionSharingDenied('continuation target owner changed')
        _metadata(session_id, publication, proof)
        if publication['state'] == 'pending':
            scope = current_scope()
            if (scope is None or scope.host is not host or scope.session_id != session_id
                    or record['revision'] != 1 or source['epoch'] != 1
                    or publication != _scope_record(scope)):
                raise SessionSharingDenied('continuation publication incomplete')
            scope._require()
        else:
            # Scope gives no privilege after commit, including in its original Task.
            ContinuationCompiler(host, identity_resolver=lambda: owner,
                                 project_authorizer=host._storage).revalidate(proof)
    finally:
        _CHAIN.reset(token)


def _owned_publication(host, session_id, identity, *, require_committed):
    with host._storage._locked():
        record, owner, source, _ = host._current(host._storage._load(), session_id)
        if owner != identity or 'continuation' not in record:
            raise SessionSharingDenied('owned continuation required')
        publication = record['continuation']
        if require_committed and publication['state'] != 'committed':
            raise SessionSharingDenied('committed continuation required')
        # JSON values are detached from the temporary sidecar snapshot.
        return json.loads(json.dumps(publication))



def read_approval(host, session_id, identity):
    """Read current original facts, or None for an ordinary owned Session."""
    with host._storage._locked():
        record, owner, _, _ = host._current(host._storage._load(), session_id)
        if owner != identity:
            raise SessionSharingDenied('owned Session required')
        publication = record.get('continuation')
        if publication is None:
            return None
        if publication['state'] != 'committed':
            raise SessionSharingDenied('committed continuation required')
        return json.loads(json.dumps(publication))


def read_target_snapshot(host, session_id, identity):
    """Return original committed configuration facts after current owner checks."""
    return _owned_publication(host, session_id, identity, require_committed=True)['target_snapshot']


def _safe_platform():
    if os.name != 'posix' or any(not hasattr(os, flag) for flag in ('O_NOFOLLOW', 'O_DIRECTORY', 'O_NONBLOCK')):
        raise SessionSharingDenied('safe continuation persistence is unavailable on this platform')

def _path(session_id):
    _safe_platform()
    directory = lifecycle.resolve_session(session_id)
    root = lifecycle.get_agent_sessions_dir().resolve()
    resolved = directory.resolve()
    if directory.is_symlink() or not resolved.is_relative_to(root):
        raise SessionSharingDenied('continuation directory is not owned')
    return directory / 'continuation.json'


def _decode_seed(value):
    _exact(value, {'version', 'proof', 'messages', 'digest'})
    if type(value['version']) is not int or value['version'] != 1:
        raise SessionSharingDenied('unsupported continuation seed version')
    proof = parse_proof(value['proof'])
    messages = value['messages']
    if type(messages) is not list or len(messages) > MAX_SEED_MESSAGES:
        raise SessionSharingDenied('continuation seed message limit exceeded')
    total = 0
    parsed = []
    for message in messages:
        _exact(message, {'role', 'content'})
        item = ContinuationMessage(**message)
        total += len(item.content.encode('utf-8'))
        if total > MAX_SEED_BYTES:
            raise SessionSharingDenied('continuation seed byte limit exceeded')
        parsed.append(item)
    seed = ContinuationSeed(proof, tuple(parsed))
    if _hex(value['digest']) != seed.digest:
        raise SessionSharingDenied('continuation seed digest mismatch')
    return seed


def _read_file(session_id):
    path = _path(session_id)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_SEED_FILE_BYTES:
            raise SessionSharingDenied('continuation seed file unavailable or oversized')
        raw = stream.read(MAX_SEED_FILE_BYTES + 1)
        after = os.fstat(stream.fileno())
    current = path.lstat()
    if (len(raw) > MAX_SEED_FILE_BYTES or len(raw) != before.st_size
            or (before.st_dev, before.st_ino, before.st_mtime_ns, before.st_size)
            != (after.st_dev, after.st_ino, after.st_mtime_ns, after.st_size)
            or not stat.S_ISREG(current.st_mode)
            or (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino)):
        raise SessionSharingDenied('continuation seed file changed')
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise SessionSharingDenied('duplicate continuation fields')
            result[key] = value
        return result
    try:
        return _decode_seed(json.loads(raw, object_pairs_hook=pairs))
    except (KeyError, TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise SessionSharingDenied('malformed persisted continuation seed') from exc


def _matches(seed, publication):
    if seed.digest != publication['seed_digest'] or asdict(seed.proof) != publication['proof']:
        raise SessionSharingDenied('continuation seed differs from publication')



def _without_sidecar_lock(host):
    from .project_access import _HELD_LOCKS
    if str(host._storage.path) in getattr(_HELD_LOCKS, 'paths', set()):
        raise SessionSharingDenied('seed IO cannot hold the owner sidecar lock')

def write_seed(scope):
    _without_sidecar_lock(scope.host)
    scope._require()
    publication = _owned_publication(scope.host, scope.session_id, scope.seed.proof.identity, require_committed=False)
    if publication != _scope_record(scope):
        raise SessionSharingDenied('only pending publication may write its seed')
    path = _path(scope.session_id)
    if path.exists() or path.is_symlink():
        seed = _read_file(scope.session_id)
        _matches(seed, publication)
    else:
        value = {'version': 1, 'proof': asdict(scope.seed.proof),
                 'messages': [asdict(item) for item in scope.seed.messages], 'digest': scope.seed.digest}
        _decode_seed(value)
        if len(json.dumps(value, ensure_ascii=False).encode()) > MAX_SEED_FILE_BYTES:
            raise SessionSharingDenied('continuation seed file too large')
        lifecycle.atomic_json(path, value)
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    scope._require()
    if _owned_publication(scope.host, scope.session_id, scope.seed.proof.identity, require_committed=False) != publication:
        raise SessionSharingConflict('continuation reservation changed during write')


def commit(scope, *, audit_context: SharingAuditContext | None = None,
           audit_result: AuditResultCallback | None = None):
    _without_sidecar_lock(scope.host)
    scope._require()
    publication = _owned_publication(scope.host, scope.session_id, scope.seed.proof.identity, require_committed=False)
    expected = _scope_record(scope)
    if publication['state'] == 'committed':
        expected['state'] = 'committed'
    if publication != expected:
        raise SessionSharingConflict('continuation reservation changed')
    seed = _read_file(scope.session_id)
    _matches(seed, publication)
    _metadata(scope.session_id, publication, seed.proof)
    scope._require()
    host = scope.host
    with host._storage._locked():
        data = host._storage._load()
        record, owner, source, _ = host._current(data, scope.session_id)
        if (owner != scope.seed.proof.identity or record['revision'] != 1 or source['epoch'] != 1
                or record.get('continuation') != publication):
            raise SessionSharingConflict('continuation reservation changed before commit')
        scope._require()
        if publication['state'] == 'committed':
            return
        record['continuation']['state'] = 'committed'
        proof = scope.seed.proof
        result = append_sharing_audit(data, checked_audit_context(proof.identity, audit_context),
            SharingAuditFacts('continue', proof.request.session_id, proof.request.share_id, proof.identity,
                proof.share_revision, proof.owner_revision, proof.source_revision,
                decision_owner_revision=proof.owner_revision, decision_source_revision=proof.source_revision,
                source_project_id=source_project(data['session_sharing'], proof.request.session_id),
                target_session_id=scope.session_id, target_project_id=proof.request.target_project_id,
                target_revision=proof.target_revision, parent_share_id=proof.parent_share_id,
                parent_revision=proof.parent_revision,
                bounds_after=SharingAuditBounds(('execute', 'view'), proof.history, proof.expires_at),
                publication_id=scope.publication_id, seed_digest=scope.seed.digest, phase='publication'))
        host._storage._save(data)
    notify_audit_result(audit_result, result)


def confirms_committed(scope):
    """Reconcile only this owned transaction's disk outcome, never grant access.

    Unlike an owner permission check, this receipt probe does not require a still
    live source share. It proves the original publication write occurred; it may
    not authorize result delivery, execution, or mutation of another record.
    """
    _without_sidecar_lock(scope.host)
    if scope.session_id is None or not scope._used:
        return False
    expected = _scope_record(scope)
    expected['state'] = 'committed'
    host = scope.host
    def same_record():
        with host._storage._locked():
            record, owner, source = host._record(host._storage._load(), scope.session_id)
            return (owner == scope.seed.proof.identity and record['revision'] == 1
                    and source['epoch'] == 1
                    and source['project_id'] == scope.seed.proof.request.target_project_id
                    and record.get('continuation') == expected)
    if not same_record():
        return False
    seed = _read_file(scope.session_id)
    _matches(seed, expected)
    _metadata(scope.session_id, expected, seed.proof)
    return same_record()


def read_seed(host, session_id, identity):
    _without_sidecar_lock(host)
    publication = _owned_publication(host, session_id, identity, require_committed=True)
    seed = _read_file(session_id)
    _matches(seed, publication)
    if _owned_publication(host, session_id, identity, require_committed=True) != publication:
        raise SessionSharingDenied('continuation authority changed during read')
    return seed
