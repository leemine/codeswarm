"""Cleanup-only host authority with real sidecar/continuation, no Provider."""
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from jiuwenswarm.governance import organization_auth
from jiuwenswarm.governance.continuation_publication import ContinuationPublication
from jiuwenswarm.governance.project_boundary import ProjectAccessDenied, authorize_resource_request
from jiuwenswarm.governance.session_boundary import admit_session_request, is_cleanup_request
from jiuwenswarm.governance.session_sharing import SessionSharingDenied
from jiuwenswarm.server.runtime.session import lifecycle
from tests.unit_tests.runtime import test_continuation_transaction as cases

setup = cases.setup
transaction = cases.transaction
CLEANUP = ('chat.cancel', 'chat.interrupt', 'session.stop', 'session.delete')


def permit(tx, sid, method='chat.cancel', params=None):
    return admit_session_request(method, params or {'session_id': sid},
        identity_resolver=lambda: tx.setup.identities[0], host=tx.setup.host, envelope_session=sid)


def resource(tx, sid, method, params, value=None, identity=None):
    request = SimpleNamespace(req_method=SimpleNamespace(value=method), session_id=sid, params=params)
    return authorize_resource_request(request, identity or tx.setup.identities[0],
        access_store=tx.setup.access, session_permit=value)


@pytest.mark.asyncio
@pytest.mark.parametrize('method', CLEANUP)
async def test_source_and_target_revoked_cleanup_only_remains_exact(transaction, monkeypatch, method):
    tx = transaction
    result = await cases.create(tx)
    params = {'session_id': result.session_id, 'intent': 'cancel'}
    original = permit(tx, result.session_id, method, params)
    tx.setup.host.store.revoke(tx.request.share_id, cases.ALICE, expected_revision=1)
    tx.setup.access.replace_acl(tx.setup.target.project_id, 'admin', acl={}, expected_revision=2)
    assert not tx.setup.host.owner_current(result.session_id, cases.BOB)
    assert original.revalidate()
    fresh = permit(tx, result.session_id, method, params)
    assert fresh.cleanup == original.cleanup and fresh.owners == ()
    assert fresh.allows_cleanup(method, params, cases.BOB, result.session_id)
    monkeypatch.setattr(organization_auth, 'configured_authenticator', lambda: object())
    assert resource(tx, result.session_id, method, params, fresh) is None
    with pytest.raises(ProjectAccessDenied):
        resource(tx, result.session_id, method, params)
    for denied in ('history.get', 'session.get_metadata', 'chat.send', 'session.switch'):
        with pytest.raises(SessionSharingDenied):
            permit(tx, result.session_id, denied)
    with pytest.raises(SessionSharingDenied):
        tx.setup.host.store.list_for_session(result.session_id, cases.BOB)


@pytest.mark.asyncio
@pytest.mark.parametrize('mutate', ['owner_revision', 'source_epoch', 'retire', 'owner_identity'])
async def test_queued_cleanup_denies_changed_owner_or_epoch(transaction, mutate):
    tx = transaction
    result = await cases.create(tx)
    original = permit(tx, result.session_id)
    if mutate == 'source_epoch':
        tx.setup.host.invalidate_source(result.session_id, expected_epoch=1)
    else:
        with tx.setup.access._locked():
            data = tx.setup.access._load()
            record = data['session_sharing']['owners'][result.session_id]
            if mutate == 'owner_revision':
                record['revision'] += 1
            elif mutate == 'retire':
                record['retired'] = True
            else:
                record['identity']['subject_id'] = 'different-subject'
            tx.setup.access._save(data)
    assert not original.revalidate()
    assert not original.allows_cleanup('chat.cancel', {'session_id': result.session_id}, cases.BOB, result.session_id)
    if mutate in {'retire', 'owner_identity'}:
        with pytest.raises(SessionSharingDenied):
            permit(tx, result.session_id)
    else:
        assert permit(tx, result.session_id).cleanup != original.cleanup


@pytest.mark.asyncio
@pytest.mark.parametrize('wrong', [cases.ALICE, cases.CAROL,
    replace(cases.BOB, subject_id='other'), replace(cases.BOB, authority='other')])
async def test_cleanup_requires_complete_current_identity(transaction, wrong):
    tx = transaction
    result = await cases.create(tx)
    original = permit(tx, result.session_id)
    tx.setup.identities[0] = wrong
    assert not original.revalidate()
    with pytest.raises(SessionSharingDenied):
        permit(tx, result.session_id)


@pytest.mark.asyncio
async def test_pending_publication_is_not_cleanup_authority(transaction, monkeypatch):
    tx = transaction
    commit = ContinuationPublication.commit
    checked = []
    def check_pending(scope):
        with pytest.raises(SessionSharingDenied, match='committed cleanup'):
            permit(tx, scope.session_id)
        checked.append(scope.session_id)
        return commit(scope)
    monkeypatch.setattr(ContinuationPublication, 'commit', check_pending)
    result = await cases.create(tx)
    assert checked == [result.session_id]
    assert permit(tx, result.session_id).revalidate()


@pytest.mark.asyncio
async def test_lifecycle_block_does_not_restore_execution_and_invalidates_old_cleanup(transaction):
    tx = transaction
    result = await cases.create(tx)
    original = permit(tx, result.session_id)
    lifecycle.begin('session', result.session_id, 'delete')
    assert not original.revalidate()
    fresh = permit(tx, result.session_id)
    assert fresh.revalidate()
    assert lifecycle.state('session', result.session_id)['blocked'] is True
    assert not tx.setup.host.owner_current(result.session_id, cases.BOB)
    with pytest.raises(lifecycle.LifecycleError, match='blocks this operation'):
        permit(tx, result.session_id, 'chat.send')
    lifecycle.complete('session', result.session_id, deleted=True)
    assert not fresh.revalidate()
    with pytest.raises(SessionSharingDenied):
        permit(tx, result.session_id)


@pytest.mark.asyncio
async def test_target_binding_mismatch_unknown_owner_and_dead_directory_are_denied(transaction, monkeypatch):
    tx = transaction
    result = await cases.create(tx)
    original = permit(tx, result.session_id)
    path = tx.root / result.session_id / 'metadata.json'
    metadata = json.loads(path.read_text())
    metadata['project_id'] = tx.setup.source.project_id
    path.write_text(json.dumps(metadata))
    assert not original.revalidate()
    with pytest.raises(SessionSharingDenied):
        permit(tx, result.session_id)
    with pytest.raises(SessionSharingDenied):
        permit(tx, 'unknown-session')
    metadata['project_id'] = tx.setup.target.project_id
    path.write_text(json.dumps(metadata))
    monkeypatch.setattr(tx.setup.host, '_known_actor', lambda _: False)
    assert not original.revalidate()


@pytest.mark.parametrize('method', CLEANUP)
@pytest.mark.parametrize('extra', ['new_input', 'messages', 'query', 'share_id', 'model_name', 'target_session_id'])
async def test_cancel_rejects_execution_parameter_smuggling(method, extra):
    with pytest.raises(SessionSharingDenied):
        is_cleanup_request(method, {'session_id': 'session', extra: ''})


@pytest.mark.parametrize('intent', ['pause', 'resume', 'supplement'])
@pytest.mark.parametrize('method', ['chat.cancel', 'chat.interrupt'])
async def test_non_cancel_remains_normal_execution_path(transaction, intent, method):
    tx = transaction
    result = await cases.create(tx)
    params = {'session_id': result.session_id, 'intent': intent, 'new_input': 'new prompt'}
    assert not is_cleanup_request(method, params)
    assert permit(tx, result.session_id, method, params).cleanup is None
    tx.setup.host.store.revoke(tx.request.share_id, cases.ALICE, expected_revision=1)
    with pytest.raises(SessionSharingDenied):
        permit(tx, result.session_id, method, params)


@pytest.mark.asyncio
async def test_project_exemption_needs_exact_original_params_identity_target_and_method(transaction, monkeypatch):
    tx = transaction
    result = await cases.create(tx)
    params = {'session_id': result.session_id, 'intent': 'cancel', 'target_request_id': 'old-turn',
              'mode': 'agent.work.normal', 'work_mode': 'work', 'project_dir': str(tx.setup.tmp_path/'target'),
              'project_id': tx.setup.target.project_id, 'team': False, 'trusted_dirs': []}
    original = permit(tx, result.session_id, params=params)
    monkeypatch.setattr(organization_auth, 'configured_authenticator', lambda: object())
    assert resource(tx, result.session_id, 'chat.cancel', dict(params), original) is None
    for changed in ({**params, 'target_request_id': 'new-turn'}, {**params, 'mode': 'team'},
                    {**params, 'new_input': 'smuggled'}, {**params, 'session_id': 'other-session'}):
        with pytest.raises(ProjectAccessDenied):
            resource(tx, result.session_id, 'chat.cancel', changed, original)
    with pytest.raises(ProjectAccessDenied):
        resource(tx, result.session_id, 'session.stop', params, original)
    with pytest.raises(ProjectAccessDenied):
        resource(tx, result.session_id, 'chat.cancel', params, original, cases.ALICE)
    tx.setup.host.invalidate_source(result.session_id, expected_epoch=1)
    with pytest.raises(ProjectAccessDenied):
        resource(tx, result.session_id, 'chat.cancel', params, original)


@pytest.mark.parametrize('intent', [None, True, 1, {}, [], 'unknown', ' cancel '])
def test_malformed_intent_is_denied(intent):
    with pytest.raises(SessionSharingDenied):
        is_cleanup_request('chat.cancel', {'session_id': 'session', 'intent': intent})


@pytest.mark.asyncio
async def test_expired_source_does_not_revoke_cleanup_or_restore_view(transaction):
    tx = transaction
    result = await cases.create(tx)
    original = permit(tx, result.session_id)
    tx.setup.clock[0] = 201
    assert original.revalidate()
    assert not tx.setup.host.owner_current(result.session_id, cases.BOB)
    with pytest.raises(SessionSharingDenied):
        permit(tx, result.session_id, 'history.get')
