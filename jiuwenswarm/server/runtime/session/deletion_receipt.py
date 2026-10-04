"""Private delete-only receipts in the existing owner sidecar.

Callers own the established project/session execution-owner locks. These ports
never acquire those locks, await, or allocate a Session. Lifecycle.operation is
still the sole progress record; this receipt only pins the original authority.
"""
from __future__ import annotations

import copy
import json
import uuid
from contextvars import copy_context
from dataclasses import dataclass, field

from jiuwenswarm.governance.contracts import TrustedIdentity
from jiuwenswarm.governance.session_sharing import SessionSharingDenied
from . import lifecycle

_MARKER = object()
_BINDING = ('project_id', 'project_dir', 'mode', 'work_mode', 'team_name',
            'channel_id', 'execution_profile_id', 'execution_config_fingerprint')
_FIELDS = {'schema', 'deletion_id', 'session_id', 'identity', 'owner_revision',
           'source_epoch', 'source_session_generation', 'source_project_generation',
           'project_generation', 'operation_id', 'generation', 'initial_generation', 'binding',
           'original_stamp', 'request_params'}


def _deny(message='original deletion authority changed'):
    raise SessionSharingDenied(message)


def _number(value, minimum=0):
    if type(value) is not int or not minimum <= value < 2 ** 64:
        _deny('invalid deletion receipt number')
    return value


def _binding(metadata):
    if not isinstance(metadata, dict) or not metadata:
        _deny('deletion target metadata required')
    value = {key: metadata.get(key) for key in _BINDING}
    if any(item is not None and (not isinstance(item, str) or len(item) > 32768)
           for item in value.values()):
        _deny('invalid deletion target binding')
    from jiuwenswarm.common.mode_matrix import is_team_mode
    if value['team_name'] or is_team_mode(value['mode']):
        _deny('only Single deletion is supported')
    if not isinstance(value['project_id'], str) or not value['project_id']:
        _deny('managed deletion project required')
    return value


def _parse(value):
    if type(value) is not dict or set(value) != _FIELDS or type(value['schema']) is not int or value['schema'] != 1:
        _deny('invalid deletion receipt schema')
    for key in ('deletion_id', 'session_id', 'operation_id'):
        if not isinstance(value[key], str) or not value[key] or len(value[key]) > 256:
            _deny('invalid deletion receipt identifier')
    for key in ('owner_revision', 'source_epoch', 'generation', 'initial_generation'):
        _number(value[key], 1)
    for key in ('source_session_generation', 'source_project_generation', 'project_generation'):
        _number(value[key])
    if type(value['identity']) is not dict or set(value['identity']) != {'actor_id', 'subject_id', 'authority'}:
        _deny('invalid deletion identity')
    try:
        TrustedIdentity(**value['identity'])
    except (TypeError, ValueError):
        _deny('invalid deletion identity')
    if type(value['binding']) is not dict or set(value['binding']) != set(_BINDING):
        _deny('invalid deletion binding')
    _binding(value['binding'])
    stamp = value['original_stamp']
    if type(stamp) is not list or len(stamp) != 9 or type(stamp[7]) is not bool or type(stamp[8]) is not bool:
        _deny('invalid original cleanup stamp')
    for index in (0, 1, 3, 4, 5, 6):
        _number(stamp[index], int(index in (0, 1)))
    if (stamp[0] != value['owner_revision'] or stamp[1]+1 != value['source_epoch']
            or stamp[2] != value['binding']['project_id']
            or stamp[3:5] != [value['source_session_generation'], value['source_project_generation']]
            or stamp[6] != value['project_generation']
            or value['initial_generation'] not in {stamp[5], stamp[5]+1}
            or value['generation'] < value['initial_generation']):
        _deny('inconsistent original deletion stamp')
    from jiuwenswarm.governance.session_boundary import is_cleanup_request
    try:
        params = json.loads(value['request_params'])
        canonical = json.dumps(params, sort_keys=True, separators=(',', ':'))
        valid = is_cleanup_request('session.delete', params) and params.get('session_id', value['session_id']) == value['session_id']
    except (TypeError, ValueError):
        _deny('invalid deletion request parameters')
    if not valid or canonical != value['request_params']:
        _deny('invalid deletion request parameters')
    return copy.deepcopy(value)


def _operation(sid, supplied=None):
    state = lifecycle.state('session', sid)
    op = state.get('operation')
    if (type(op) is not dict or op.get('kind') != 'delete' or op.get('resource_id') != sid
            or op.get('resource_type') != 'session' or op.get('generation') != state.get('generation')
            or op.get('status') not in {'running', 'failed', 'completed'}):
        _deny('original delete operation required')
    _number(state.get('generation'), 1)
    _number(op.get('generation'), 1)
    if supplied is not None and (type(supplied) is not dict or any(
            supplied.get(key) != op.get(key) for key in ('operation_id', 'generation', 'kind', 'resource_id', 'resource_type'))):
        _deny('delete operation changed')
    return op


@dataclass(frozen=True, repr=False)
class DeletionCapture:
    session_id: str
    _host: object = field(repr=False)
    _identity: TrustedIdentity = field(repr=False)
    _resolver: object = field(repr=False)
    _context: object = field(repr=False)
    _stamp: tuple = field(repr=False)
    _binding_json: str = field(repr=False)
    _params: str = field(repr=False)
    _operation_id: str | None = field(repr=False)
    _nonce: str = field(repr=False)
    _marker: object = field(repr=False, default=None)

    @property
    def descriptor(self):
        return json.loads(self._binding_json)


@dataclass(frozen=True, repr=False)
class DeletionReceipt:
    session_id: str
    _host: object = field(repr=False)
    _identity: TrustedIdentity = field(repr=False)
    _resolver: object = field(repr=False)
    _context: object = field(repr=False)
    _record_json: str = field(repr=False)
    _observed_generation: int = field(repr=False)
    _admission_only: bool = field(repr=False)
    _marker: object = field(repr=False, default=None)

    @property
    def descriptor(self):
        return json.loads(self._record_json)['binding']

    @property
    def operation_id(self):
        return json.loads(self._record_json)['operation_id']

    @property
    def generation(self):
        return self._observed_generation


def _live(host, handle):
    if (type(handle) not in {DeletionCapture, DeletionReceipt} or handle._marker is not _MARKER or handle._host is not host
            or handle._context.copy().run(handle._resolver) != handle._identity
            or host._known_actor(handle._identity) is not True):
        _deny('live original deletion identity required')


def _handle(host, value, identity, resolver, context, *, observed=None, admission_only=False):
    return DeletionReceipt(value['session_id'], host, identity, resolver, context,
                           json.dumps(value, sort_keys=True), value['generation'] if observed is None else observed,
                           admission_only, _MARKER)


def capture(host, sid, identity, permit):
    from jiuwenswarm.governance.session_boundary import SessionRequestPermit
    if (type(permit) is not SessionRequestPermit or permit.host is not host
            or permit.identity != identity or permit.method != 'session.delete'
            or permit.cleanup is None or permit.cleanup[0] != sid or not permit.revalidate()):
        _deny('original Single delete permit required')
    try:
        params = json.loads(permit.cleanup_params)
        allowed = permit.allows_cleanup('session.delete', params, identity, sid)
    except (TypeError, ValueError):
        allowed = False
    if not allowed:
        _deny('original exact delete parameters required')
    state = lifecycle.state('session', sid)
    op = state.get('operation') or {}
    if op and op.get('status') != 'completed' and op.get('kind') != 'delete':
        _deny('another lifecycle operation is pending')
    metadata = _binding(lifecycle.raw_metadata(sid))
    with host._storage._locked():
        record, owner, source = host._record(host._storage._load(), sid, active=False)
        if record.get('deletion') is not None:
            _deny('resume original deletion receipt')
        if owner != identity or source['project_id'] != metadata['project_id']:
            _deny()
    if not permit.revalidate():
        _deny()
    return DeletionCapture(sid, host, identity, permit.identity_resolver, copy_context(),
        permit.cleanup[1], json.dumps(metadata, sort_keys=True), permit.cleanup_params,
        op.get('operation_id') if op.get('status') != 'completed' else None, uuid.uuid4().hex, _MARKER)


def begin(host, original, operation):
    from dataclasses import asdict
    if type(original) is not DeletionCapture:
        _deny('original deletion capture required')
    _live(host, original)
    op = _operation(original.session_id, operation)
    if ((original._operation_id is not None and op['operation_id'] != original._operation_id)
            or op['generation'] not in {original._stamp[5], original._stamp[5] + 1}
            or original._operation_id is None and op['generation'] != original._stamp[5] + 1):
        _deny('unexpected deletion lifecycle transition')
    binding = json.loads(original._binding_json)
    value = _parse(dict(schema=1, deletion_id=original._nonce, session_id=original.session_id,
        identity=asdict(original._identity), owner_revision=original._stamp[0], source_epoch=original._stamp[1]+1,
        source_session_generation=original._stamp[3], source_project_generation=original._stamp[4],
        project_generation=original._stamp[6], operation_id=op['operation_id'], generation=op['generation'], initial_generation=op['generation'], binding=binding,
        original_stamp=list(original._stamp), request_params=original._params))
    with host._storage._locked():
        data = host._storage._load()
        record, owner, source = host._record(data, original.session_id, active=False)
        prior = record.get('deletion')
        if prior is not None:
            if _parse(prior) != value:
                _deny('another deletion owns this Session')
            result = _handle(host, value, original._identity, original._resolver, original._context)
            _check_record(host, data, result)
            return result
        if (owner != original._identity or record['revision'] != original._stamp[0]
                or (source['epoch'], source['project_id'], source['session_generation'], source['project_generation'])
                != original._stamp[1:5]
                or lifecycle.state('project', source['project_id']).get('generation', 0) != original._stamp[6]
                or _binding(lifecycle.raw_metadata(original.session_id)) != binding):
            _deny()
        _operation(original.session_id, operation)
        _live(host, original)
        source.update(epoch=value['source_epoch'], active=False, history=None)
        record['deletion'] = value
        host._storage._save(data)
    return _handle(host, value, original._identity, original._resolver, original._context)


def _check_record(host, data, receipt, *, retired=False, operation=True):
    if type(receipt) is not DeletionReceipt or receipt._marker is not _MARKER or receipt._host is not host:
        _deny('original deletion receipt required')
    expected = _parse(json.loads(receipt._record_json))
    record = host.store._section(data)['owners'].get(receipt.session_id)
    if type(record) is not dict or _parse(record.get('deletion')) != expected:
        _deny()
    source = record.get('source', {})
    _number(record.get('revision'), 1)
    for key in ('epoch', 'session_generation', 'project_generation'):
        _number(source.get(key), int(key == 'epoch'))
    if (record.get('identity') != expected['identity'] or record.get('retired') is not retired
            or record.get('revision') != expected['owner_revision'] + int(retired)
            or type(source.get('schema_version')) is not int or source.get('schema_version') != 1 or source.get('active') is not False
            or (source.get('epoch'), source.get('project_id'), source.get('session_generation'), source.get('project_generation'))
            != (expected['source_epoch'], expected['binding']['project_id'], expected['source_session_generation'], expected['source_project_generation'])):
        _deny()
    if operation:
        op = _operation(receipt.session_id)
        if (op['operation_id'], op['generation']) != (expected['operation_id'], receipt._observed_generation):
            _deny('original deletion operation changed')
    if lifecycle.state('project', expected['binding']['project_id']).get('generation', 0) != expected['project_generation']:
        _deny('deletion project generation changed')
    active, archived = lifecycle.session_paths(receipt.session_id)
    metadata = lifecycle.raw_metadata(receipt.session_id)
    if metadata:
        if retired or _binding(metadata) != expected['binding']:
            _deny('deletion metadata changed')
    elif active.exists() or archived.exists():
        # A partial deletion may have removed metadata; only the existing
        # receipt and exact operation authorize finishing the same directory.
        if retired or _operation(receipt.session_id).get('phase') not in {'delete_directory', 'cleanup'}:
            _deny('deletion metadata unavailable before destructive phase')
    return record, expected


def resume(host, sid, identity, *, identity_resolver):
    if not isinstance(identity, TrustedIdentity):
        _deny('trusted deletion identity required')
    with host._storage._locked():
        data = host._storage._load()
        record = host.store._section(data)['owners'].get(sid)
        if type(record) is not dict:
            _deny()
        value = _parse(record.get('deletion'))
        if TrustedIdentity(**value['identity']) != identity or value['session_id'] != sid:
            _deny()
        op = _operation(sid)
        if op['operation_id'] != value['operation_id'] or op['generation'] < value['generation']:
            _deny('original deletion operation required')
        result = _handle(host, value, identity, identity_resolver, copy_context(),
                         observed=op['generation'], admission_only=True)
        _live(host, result)
        _check_record(host, data, result, retired=record.get('retired') is True)
        return result


def check(host, receipt, *, for_admission=False):
    _live(host, receipt)
    with host._storage._locked():
        data = host._storage._load()
        retired = host.store._section(data)['owners'].get(receipt.session_id, {}).get('retired') is True
        if not for_admission and (receipt._admission_only or retired):
            _deny('deletion recovery must be adopted before destructive cleanup')
        _check_record(host, data, receipt, retired=retired)
    _live(host, receipt)


def commit(host, receipt):
    _live(host, receipt)
    with host._storage._locked():
        data = host._storage._load()
        existing = host.store._section(data)['owners'].get(receipt.session_id, {})
        record, value = _check_record(host, data, receipt, retired=existing.get('retired') is True)
        if any(path.exists() for path in lifecycle.session_paths(receipt.session_id)):
            _deny('Session files must be removed before retirement')
        _live(host, receipt)
        if record.get('retired') is True:
            return
        if receipt._admission_only or receipt.generation != value['generation']:
            _deny('deletion recovery must be adopted before commit')
        record.update(retired=True, revision=value['owner_revision']+1)
        host._storage._save(data)


def confirms(host, receipt):
    """Transaction fact only; never a substitute for live request authority."""
    try:
        with host._storage._locked():
            _check_record(host, host._storage._load(), receipt, retired=True)
            return not any(path.exists() for path in lifecycle.session_paths(receipt.session_id))
    except Exception:
        return False


def adopt(host, receipt, operation):
    """Explicitly adopt the original operation after its owner-lock takeover.

    The caller must hold the original execution-owner lock. A generation claim
    can advance by one; it never changes the deletion nonce, owner or source.
    """
    _live(host, receipt)
    op = _operation(receipt.session_id, operation)
    with host._storage._locked():
        data = host._storage._load()
        existing = host.store._section(data)['owners'].get(receipt.session_id, {})
        record, value = _check_record(host, data, receipt,
            retired=existing.get('retired') is True, operation=False)
        if (op['operation_id'] != value['operation_id']
                or op['generation'] not in {receipt.generation, receipt.generation+1}):
            _deny('another operation cannot adopt deletion')
        if op['generation'] != value['generation']:
            value['generation'] = op['generation']
            record['deletion'] = value
            _live(host, receipt)
            host._storage._save(data)
    return _handle(host, value, receipt._identity, receipt._resolver, receipt._context)


def confirm_for_permit(host, permit):
    """Content-free ACK proof for the exact incoming delete request only."""
    from jiuwenswarm.governance.session_boundary import SessionRequestPermit, is_cleanup_request
    try:
        if (type(permit) is not SessionRequestPermit or permit.host is not host
                or permit.method != 'session.delete' or permit.cleanup is None
                or permit.identity_resolver() != permit.identity
                or host._known_actor(permit.identity) is not True):
            return False
        sid, stamp = permit.cleanup
        params = json.loads(permit.cleanup_params)
        if not is_cleanup_request('session.delete', params) or params.get('session_id', sid) != sid:
            return False
        with host._storage._locked():
            data = host._storage._load()
            record = host.store._section(data)['owners'].get(sid)
            value = _parse(record.get('deletion'))
            if (TrustedIdentity(**value['identity']) != permit.identity or value['session_id'] != sid
                    or value['request_params'] != permit.cleanup_params):
                return False
            original = getattr(permit, 'deletion_receipt', None)
            if original is not None:
                if (type(original) is not DeletionReceipt or original._host is not host
                        or original._identity != permit.identity or original.session_id != sid):
                    return False
                _live(host, original)
                _check_record(host, data, original, retired=True)
            else:
                if (type(stamp) is not tuple or list(stamp) != value['original_stamp']
                        or value['generation'] != value['initial_generation']):
                    return False
                original = _handle(host, value, permit.identity, permit.identity_resolver, copy_context())
                _check_record(host, data, original, retired=True)
            state = lifecycle.state('session', sid)
            op = _operation(sid)
            if state.get('deleted') is not True or op['status'] != 'completed':
                return False
            if any(path.exists() for path in lifecycle.session_paths(sid)):
                return False
        return permit.identity_resolver() == permit.identity
    except Exception:
        return False
