"""Real owner sidecar admission, ordinary Web handler and queued cleanup result."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests.unit_tests.server.test_sharing_host import setup, ALICE, BOB
from jiuwenswarm.common.schema.agent import AgentResponse
from jiuwenswarm.governance import organization_auth, session_boundary
from jiuwenswarm.gateway.channel_manager.base import RobotMessageRouter
from jiuwenswarm.gateway.channel_manager.web.web_connect import WebChannel, WebChannelConfig
from jiuwenswarm.gateway.channel_manager.web.app_web_handlers import WebHandlersBindParams, _register_web_handlers


@pytest.fixture
def boundary(setup, monkeypatch):
    host, access, project, _, _, _, _ = setup
    # Existing owned work can be stopped despite loss of ordinary read authority.
    access.replace_acl(project, 'admin', acl={}, expected_revision=2)
    actor = [ALICE]
    principal = SimpleNamespace(identity=lambda: actor[0])
    monkeypatch.setattr(organization_auth, 'configured_authenticator', lambda: object())
    monkeypatch.setattr(organization_auth, 'connection_principal', lambda ws: principal)
    monkeypatch.setattr(session_boundary, 'organization_sharing_host', lambda: host)
    channel = WebChannel(WebChannelConfig(enabled=True), RobotMessageRouter())
    channel._process_files = AsyncMock(side_effect=AssertionError('cleanup processed files'))
    channel.register_ws = AsyncMock(side_effect=AssertionError('cleanup subscribed history'))
    channel._on_message_cb = AsyncMock(side_effect=AssertionError('cleanup entered generic message queue'))
    ws = SimpleNamespace(_jiuwen_ws_id='browser', closed=False, send=AsyncMock())
    channel._send_queues['browser'] = asyncio.Queue()
    client = SimpleNamespace(server_ready=True, send_request=AsyncMock())
    _register_web_handlers(WebHandlersBindParams(channel=channel, agent_client=client))
    return host, actor, channel, ws, client


def response(**changes):
    result = AgentResponse('cancel-1', 'web', ok=True, payload={
        'event_type': 'chat.interrupt_result', 'session_id': 'session',
        'intent': 'cancel', 'success': True, 'exit_confirmed': True,
        'metadata': {'secret': 'hidden'}, 'history': ['private'], 'todos': ['private'],
    })
    for key, value in changes.items():
        if key in {'request_id', 'channel_id', 'ok'}:
            setattr(result, key, value)
        else:
            result.payload[key] = value
    return result


async def dispatch(channel, ws, *, method='chat.interrupt', **extra):
    await channel._handle_raw_message(ws, json.dumps({
        'type': 'req', 'id': 'cancel-1', 'method': method,
        'params': {'session_id': 'session', 'intent': 'cancel', **extra},
    }), {})


async def drain(channel, ws):
    channel._send_queues['browser'].put_nowait(None)
    await channel._writer_loop(ws, 'browser')
    return [json.loads(call.args[0]) for call in ws.send.await_args_list]


@pytest.mark.parametrize('target_request_id', [None, 'original-turn'])
@pytest.mark.asyncio
async def test_revoked_owner_cancel_keeps_original_identity_and_only_fixed_result(boundary, target_request_id):
    host, _, channel, ws, client = boundary
    assert not host.owner_current('session', ALICE)
    extra = {'target_request_id': target_request_id} if target_request_id else {}
    async def execute(envelope):
        assert envelope.request_id == 'cancel-1'
        assert envelope.session_id == 'session'
        assert envelope.params == {'session_id': 'session', 'intent': 'cancel', **extra}
        assert envelope.method == 'chat.interrupt' and envelope.is_stream is False
        assert organization_auth.current_identity() == ALICE
        return response()
    client.send_request.side_effect = execute
    await dispatch(channel, ws, **extra)
    frames = await drain(channel, ws)
    assert len(frames) == 2
    expected = {'request_id': 'cancel-1', 'session_id': 'session', 'intent': 'cancel',
                'success': True, 'exit_confirmed': True}
    assert frames[0] == {'type': 'res', 'id': 'cancel-1', 'ok': True, 'payload': expected}
    assert frames[1] == {'type': 'event', 'event': 'chat.interrupt_result', 'payload': expected}
    assert not channel._clients_by_key and not channel._ws_sessions
    channel.register_ws.assert_not_awaited()
    channel._process_files.assert_not_awaited()
    channel._on_message_cb.assert_not_awaited()


@pytest.mark.parametrize('patch', [
    {'ok': False}, {'request_id': 'other'}, {'channel_id': 'other'}, {'session_id': 'other'},
    {'intent': 'resume'}, {'event_type': 'history.get'}, {'success': False},
    {'exit_confirmed': False}, {'exit_confirmed': None}, {'success': 'true'},
])
@pytest.mark.asyncio
async def test_unconfirmed_or_mismatched_results_do_not_report_exit(boundary, patch):
    _, _, channel, ws, client = boundary
    client.send_request.return_value = response(**patch)
    await dispatch(channel, ws)
    frames = await drain(channel, ws)
    assert len(frames) == 2 and frames[0]['ok'] is False
    assert all(frame['payload']['success'] is False and frame['payload']['exit_confirmed'] is False for frame in frames)
    assert 'hidden' not in json.dumps(frames) and 'private' not in json.dumps(frames)


@pytest.mark.asyncio
async def test_transport_error_is_fixed_unconfirmed_no_private_details(boundary):
    _, _, channel, ws, client = boundary
    client.send_request.side_effect = RuntimeError('private secret history')
    await dispatch(channel, ws)
    frames = await drain(channel, ws)
    assert frames[0]['ok'] is False and frames[1]['payload']['success'] is False
    assert 'private secret' not in json.dumps(frames)


@pytest.mark.parametrize('change', ['identity', 'stamp'])
@pytest.mark.asyncio
async def test_cleanup_queue_rechecks_identity_and_original_owner_stamp(boundary, change):
    host, actor, channel, ws, client = boundary
    client.send_request.return_value = response()
    await dispatch(channel, ws)
    if change == 'identity':
        actor[0] = BOB
    else:
        host.invalidate_source('session', expected_epoch=host.source_epoch('session'))
    assert await drain(channel, ws) == []


@pytest.mark.asyncio
async def test_await_identity_change_drops_result_and_never_subscribes(boundary):
    _, actor, channel, ws, client = boundary
    async def execute(envelope):
        actor[0] = BOB
        return response()
    client.send_request.side_effect = execute
    await dispatch(channel, ws)
    assert await drain(channel, ws) == []
    assert not channel._clients_by_key


@pytest.mark.parametrize('extra', [{'new_input': 'execute me'}, {'history': []}, {'exit_confirmed': True}])
@pytest.mark.asyncio
async def test_client_fields_cannot_authorize_or_expand_cleanup(boundary, extra):
    _, _, channel, ws, client = boundary
    await dispatch(channel, ws, **extra)
    frames = await drain(channel, ws)
    assert len(frames) == 1 and frames[0]['ok'] is False
    client.send_request.assert_not_awaited()
    channel._process_files.assert_not_awaited()


@pytest.mark.asyncio
async def test_cleanup_permit_never_authorizes_generic_response_or_event(boundary):
    _, _, channel, ws, client = boundary
    client.send_request.return_value = response()
    await dispatch(channel, ws)
    await channel.send_response(ws, 'cancel-1', ok=True, payload={'history': ['private']})
    await channel.send_event(ws, 'chat.interrupt_result',
                             {'request_id': 'cancel-1', 'session_id': 'session', 'success': True}, session_id='session')
    frames = await drain(channel, ws)
    assert len(frames) == 2 and 'private' not in json.dumps(frames)


@pytest.mark.asyncio
async def test_delete_cleanup_delivery_is_not_implicitly_enabled(boundary):
    _, _, channel, ws, client = boundary
    await dispatch(channel, ws, method='session.delete')
    frames = await drain(channel, ws)
    assert len(frames) == 1 and frames[0]['ok'] is False
    client.send_request.assert_not_awaited()


@pytest.mark.asyncio
async def test_legacy_interrupt_keeps_receipt_and_original_dispatch_contract(boundary, monkeypatch):
    _, _, channel, ws, client = boundary
    monkeypatch.setattr(organization_auth, 'configured_authenticator', lambda: None)
    monkeypatch.setattr(organization_auth, 'connection_principal', lambda ws: None)
    await channel._method_handlers['chat.interrupt'](ws, 'legacy', {'intent': 'cancel'}, 'session')
    frames = await drain(channel, ws)
    assert frames == [{'type': 'res', 'id': 'legacy', 'ok': True,
                       'payload': {'accepted': True, 'session_id': 'session', 'intent': 'cancel'}}]
    client.send_request.assert_not_awaited()
