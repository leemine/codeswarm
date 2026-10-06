"""Resource mutations preserve exact committed facts across the real Web proxy."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from jiuwenswarm.gateway.channel_manager.web.app_web_handlers import (
    WebHandlersBindParams,
    _register_web_handlers,
)
from tests.unit_tests.gateway.test_app_web_handlers import FakeWebChannel


def receipt():
    return {
        'code': 'EXIT_UNCONFIRMED', 'error': 'PRIVATE-ERROR-MUST-NOT-LEAK',
        'exit_confirmed': False,
        'mutation': {'committed': True, 'project_id': 'project',
                     'resource_id': 'workspace', 'target_actor': 'bob', 'resource_revision': 4},
    }


async def invoke(payload, *, method='project.resources.revoke', ok=False):
    seen = []

    class Client:
        async def send_request(self, envelope):
            seen.append(envelope)
            return SimpleNamespace(ok=ok, payload=deepcopy(payload))

    channel = FakeWebChannel()
    _register_web_handlers(WebHandlersBindParams(channel=channel, agent_client=Client()))
    params = {'project_id': 'project', 'resource_id': 'workspace', 'target_actor': 'bob',
              'expected_acl_revision': 2, 'expected_resource_revision': 3}
    if method == 'project.resources.grant':
        params.update(actions=['read'], expires_at=None)
    await channel.methods[method](object(), 'resource-request', params, None)
    assert len(seen) == 1 and seen[0].request_id == 'resource-request'
    assert seen[0].method == method and seen[0].params == params
    return channel.responses[0]


@pytest.mark.asyncio
@pytest.mark.parametrize('method', ['project.resources.grant', 'project.resources.revoke'])
async def test_committed_but_exit_unconfirmed_is_visible_without_internal_error(method):
    response = await invoke(receipt(), method=method)
    assert response['ok'] is False and response['code'] == 'EXIT_UNCONFIRMED'
    assert response['payload'] == {
        'code': 'EXIT_UNCONFIRMED', 'exit_confirmed': False, 'mutation': receipt()['mutation'],
    }
    assert 'PRIVATE-ERROR' not in str(response)
    assert 'do not repeat' in response['error']


@pytest.mark.asyncio
@pytest.mark.parametrize('change', [
    'project', 'resource', 'target', 'revision', 'boolean_revision', 'extra_mutation',
    'code', 'exit', 'committed',
])
async def test_uncorrelated_commit_or_untyped_receipt_is_not_forwarded(change):
    payload = receipt()
    mutation = payload['mutation']
    if change in {'project', 'resource', 'target'}:
        mutation[{'project': 'project_id', 'resource': 'resource_id', 'target': 'target_actor'}[change]] = 'other'
    elif change == 'revision':
        mutation['resource_revision'] = 5
    elif change == 'boolean_revision':
        mutation['resource_revision'] = True
    elif change == 'extra_mutation':
        mutation['credential'] = 'PRIVATE-ERROR-MUST-NOT-LEAK'
    elif change == 'code':
        payload['code'] = 'FORBIDDEN'
    elif change == 'exit':
        payload['exit_confirmed'] = True
    else:
        mutation['committed'] = False
    response = await invoke(payload)
    assert response['ok'] is False and response['payload'] is None
    assert 'PRIVATE-ERROR' not in str(response)


@pytest.mark.asyncio
async def test_success_passes_original_safe_receipt_and_error_discards_unrelated_fields():
    mutation = receipt()['mutation']
    response = await invoke({'mutation': mutation}, ok=True)
    assert response['ok'] is True and response['payload'] == {'mutation': mutation}
    payload = receipt()
    payload['private_configuration'] = 'PRIVATE-ERROR-MUST-NOT-LEAK'
    response = await invoke(payload)
    assert set(response['payload']) == {'code', 'exit_confirmed', 'mutation'}
    assert 'private_configuration' not in str(response)


@pytest.mark.asyncio
@pytest.mark.parametrize('method', ['project.resources.grant', 'project.resources.revoke'])
async def test_unknown_outcome_remains_unknown_without_claiming_a_commit_or_denial(method):
    response = await invoke({
        'code': 'MUTATION_OUTCOME_UNKNOWN',
        'error': 'PRIVATE-ERROR-MUST-NOT-LEAK',
        'private_configuration': 'PRIVATE-ERROR-MUST-NOT-LEAK',
    }, method=method)
    assert response['ok'] is False
    assert response['code'] == 'MUTATION_OUTCOME_UNKNOWN'
    assert response['payload'] is None
    assert 'outcome unavailable' in response['error']
    assert 'refresh' in response['error']
    assert 'PRIVATE-ERROR' not in str(response)
