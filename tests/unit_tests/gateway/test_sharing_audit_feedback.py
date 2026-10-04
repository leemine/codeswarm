"""Actual registered handler + E2A proxy retains only exact mutation receipts."""
import copy
from types import SimpleNamespace

import pytest

from jiuwenswarm.gateway.channel_manager.web.app_web_handlers import WebHandlersBindParams, _register_web_handlers
from tests.unit_tests.gateway.test_app_web_handlers import FakeWebChannel
from tests.unit_tests.server import test_session_sharing_adapter as existing

setup = existing.setup


@pytest.mark.asyncio
async def test_real_store_adapter_to_actual_handler_proxy_retains_degraded_commit(setup):
    adapter, store, _, _, _ = setup
    share = (await adapter.handle(existing.create())).payload['share']
    data = store._storage._load()
    data['sharing_audit'] = None
    store._storage._save(data)

    async def failed():
        raise RuntimeError('private')

    adapter.after_mutation = failed
    requests = []

    class Client:
        async def send_request(self, envelope):
            requests.append(envelope)
            return await adapter.handle(SimpleNamespace(req_method=envelope.method, params=envelope.params,
                request_id=envelope.request_id, channel_id='web', metadata={}))

    channel = FakeWebChannel()
    _register_web_handlers(WebHandlersBindParams(channel=channel, agent_client=Client()))
    await channel.methods['session.share.revoke'](object(), 'exact-request',
        {'session_id': 'session', 'share_id': share['share_id'], 'expected_revision': 1}, 'session')
    assert len(requests) == 1 and requests[0].request_id == 'exact-request'
    result = channel.responses[0]
    assert result['id'] == 'exact-request' and result['ok'] is False
    assert result['code'] == 'EXIT_UNCONFIRMED'
    assert result['payload']['mutation']['revision'] == 2
    assert result['payload']['audit']['degraded'] is True
    assert set(result['payload']) == {'code', 'mutation', 'audit', 'exit_confirmed'}
    assert 'private' not in str(result)


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['extra', 'sid', 'share', 'revision', 'method', 'audit', 'code'])
async def test_actual_proxy_does_not_forward_uncorrelated_or_untyped_error_payload(change):
    value = {'code': 'EXIT_UNCONFIRMED', 'error': 'DO-NOT-EXPOSE', 'exit_confirmed': False,
        'mutation': {'committed': True, 'method': 'session.share.revoke', 'session_id': 'session', 'share_id': 'share', 'revision': 2},
        'audit': {'persisted': True, 'degraded': False, 'reason': 'audit_persisted', 'sequence': 1, 'event_id': 'a' * 32}}
    if change == 'extra':
        value['private_body'] = 'DO-NOT-EXPOSE'
    elif change == 'audit':
        value['audit']['private_body'] = 'DO-NOT-EXPOSE'
    elif change == 'code':
        value['code'] = 'FORBIDDEN'
    else:
        key = {'sid': 'session_id', 'share': 'share_id', 'revision': 'revision', 'method': 'method'}[change]
        value['mutation'][key] = 3 if key == 'revision' else 'wrong'

    class Client:
        async def send_request(self, envelope):
            return SimpleNamespace(ok=False, payload=copy.deepcopy(value))

    channel = FakeWebChannel()
    _register_web_handlers(WebHandlersBindParams(channel=channel, agent_client=Client()))
    await channel.methods['session.share.revoke'](object(), 'req',
        {'session_id': 'session', 'share_id': 'share', 'expected_revision': 1}, 'session')
    result = channel.responses[0]
    assert not result['ok'] and 'DO-NOT-EXPOSE' not in str(result)
    if change == 'extra':
        assert set(result['payload']) == {'code', 'exit_confirmed', 'mutation', 'audit'}
    else:
        assert result['payload'] is None


@pytest.mark.asyncio
async def test_success_projection_and_other_project_error_contract_are_unchanged():
    class Client:
        async def send_request(self, envelope):
            return SimpleNamespace(ok=envelope.method == 'session.share.create',
                payload={'share': {'share_id': 'new'}, 'code': 'CONFLICT', 'error': 'original'})

    channel = FakeWebChannel()
    _register_web_handlers(WebHandlersBindParams(channel=channel, agent_client=Client()))
    await channel.methods['session.share.create'](object(), 'create', {'session_id': 'session'}, 'session')
    assert channel.responses[-1]['ok'] is True and channel.responses[-1]['payload']['share'] == {'share_id': 'new'}
    await channel.methods['project.content.update'](object(), 'project', {}, 'session')
    assert channel.responses[-1]['payload'] is None and channel.responses[-1]['error'] == 'original'
