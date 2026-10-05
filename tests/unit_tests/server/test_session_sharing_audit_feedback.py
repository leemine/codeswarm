"""Real store audit statuses and committed changes survive cleanup failure."""
from dataclasses import asdict

import pytest

from tests.unit_tests.server import test_session_sharing_adapter as existing

setup = existing.setup


@pytest.mark.asyncio
async def test_three_rpc_mutations_record_original_request_and_trusted_identity(setup):
    adapter, store, _, _, _ = setup
    created = await adapter.handle(existing.create())
    share = created.payload['share']
    updated = await adapter.handle(existing.request('update', session_id='session', share_id=share['share_id'],
        expected_revision=1, actions=['view'], expires_at=180))
    revoked = await adapter.handle(existing.request('revoke', session_id='session', share_id=share['share_id'],
        expected_revision=2))
    events = store._storage._load()['sharing_audit']['events']
    for response, event, method in zip((created, updated, revoked), events, ('create', 'update', 'revoke')):
        assert response.ok
        assert response.payload['audit'] == {'persisted': True, 'degraded': False, 'reason': 'audit_persisted',
                                             'sequence': event['sequence'], 'event_id': event['event_id']}
        assert event['context']['actor'] == asdict(existing.OWNER)
        assert event['context']['request_id'] == 'req'
        assert event['context']['method'] == 'session.share.' + method
        assert 'actor' not in response.payload and 'mutation' not in response.payload


@pytest.mark.asyncio
@pytest.mark.parametrize('field', ['audit_context', 'audit_result', 'audit', 'mutation'])
async def test_wire_cannot_supply_private_audit_state(setup, field):
    adapter, store, _, _, _ = setup
    response = await adapter.handle(existing.create(**{field: {'persisted': True}}))
    assert not response.ok and response.payload['code'] == 'BAD_REQUEST'
    assert 'sharing_audit' not in store._storage._load()


@pytest.mark.asyncio
@pytest.mark.parametrize('exit_failure', [False, True])
async def test_degraded_revoke_stays_committed_and_exit_failure_keeps_both_states(setup, exit_failure):
    adapter, store, _, _, _ = setup
    created = await adapter.handle(existing.create())
    share = created.payload['share']
    data = store._storage._load()
    data['sharing_audit'] = {'schema_version': 'damaged', 'evidence': 'preserve'}
    store._storage._save(data)
    calls = []

    async def after_mutation():
        calls.append(1)
        if exit_failure:
            raise RuntimeError('DO-NOT-EXPOSE-CREDENTIAL')

    adapter.after_mutation = after_mutation
    response = await adapter.handle(existing.request('revoke', session_id='session', share_id=share['share_id'], expected_revision=1))
    assert response.ok is not exit_failure and calls == [1]
    assert response.payload['audit'] == {'persisted': False, 'degraded': True, 'reason': 'audit_storage_invalid',
                                         'sequence': None, 'event_id': None}
    current = store._storage._load()
    assert current['session_sharing']['shares'][share['share_id']]['revoked'] is True
    assert current['session_sharing']['shares'][share['share_id']]['revision'] == 2
    assert current['sharing_audit'] == data['sharing_audit']
    if exit_failure:
        assert response.payload['code'] == 'EXIT_UNCONFIRMED' and response.payload['exit_confirmed'] is False
        assert response.payload['mutation'] == {'committed': True, 'method': 'session.share.revoke',
            'session_id': 'session', 'share_id': share['share_id'], 'revision': 2}
    assert 'DO-NOT-EXPOSE-CREDENTIAL' not in str(response.payload)


@pytest.mark.asyncio
async def test_failed_update_exit_keeps_persisted_mutation_without_returning_full_share(setup):
    adapter, _, _, _, _ = setup
    share = (await adapter.handle(existing.create())).payload['share']

    async def failed():
        raise RuntimeError('private')

    adapter.after_mutation = failed
    response = await adapter.handle(existing.request('update', session_id='session', share_id=share['share_id'],
        expected_revision=1, actions=['view'], expires_at=180))
    assert not response.ok and response.payload['audit']['persisted'] is True
    assert response.payload['mutation']['revision'] == 2
    assert set(response.payload) == {'code', 'error', 'mutation', 'audit', 'exit_confirmed'}
