"""Gateway-only proof: real admission, synthetic host completion/transport.

The stub completion checker is not evidence of Runtime/sidecar deletion closure.
"""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.common.schema.agent import AgentResponse
from jiuwenswarm.governance import organization_auth
from tests.unit_tests.server.test_organization_cleanup_delivery import boundary, drain
from tests.unit_tests.server.test_sharing_host import setup, ALICE, BOB


@pytest.fixture
def deletion(boundary, monkeypatch):
    host, actor, channel, ws, client = boundary
    completed = [False]
    checks = []
    def confirm(permit):
        checks.append(permit)
        return completed[0]
    monkeypatch.setattr(host, 'confirm_deletion_for_permit', confirm, raising=False)
    return host, actor, channel, ws, client, completed, checks


def response(**changes):
    result = AgentResponse('delete-1', 'web', ok=True, payload={
        'session_id': 'session', 'deleted': True, 'exit_confirmed': True,
        'project_id': 'private-project', 'metadata': {'secret': 'hidden'},
        'history': ['private'], 'todos': ['private'],
    })
    for key, value in changes.items():
        if key in {'request_id', 'channel_id', 'ok'}:
            setattr(result, key, value)
        else:
            result.payload[key] = value
    return result


async def dispatch(channel, ws, **extra):
    await channel._handle_raw_message(ws, json.dumps({
        'type': 'req', 'id': 'delete-1', 'method': 'session.delete',
        'params': {'session_id': 'session', **extra},
    }), {})


@pytest.mark.asyncio
async def test_delete_uses_original_request_and_only_exact_host_confirmed_result(deletion):
    host, _, channel, ws, client, completed, checks = deletion
    async def execute(envelope):
        assert envelope.request_id == 'delete-1' and envelope.channel == 'web'
        assert envelope.session_id == 'session' and envelope.method == 'session.delete'
        assert envelope.params == {'session_id': 'session'} and envelope.is_stream is False
        assert organization_auth.current_identity() == ALICE
        # Simulate retirement making ordinary owner permit stale. Real receipt
        # verification belongs to SharingHost tests, not this synthetic callback.
        host.invalidate_source('session', expected_epoch=host.source_epoch('session'))
        completed[0] = True
        return response()
    client.send_request.side_effect = execute
    await dispatch(channel, ws)
    permit = ws._jiuwen_session_permits['delete-1']
    assert not permit.revalidate()
    frames = await drain(channel, ws)
    payload = {'session_id': 'session', 'deleted': True, 'exit_confirmed': True}
    assert frames == [
        {'type': 'res', 'id': 'delete-1', 'ok': True, 'payload': payload},
        {'type': 'event', 'event': 'session.deleted', 'payload': payload},
    ]
    assert len(checks) >= 5 and all(item is permit for item in checks)
    assert not channel._clients_by_key and not channel._ws_sessions
    channel._process_files.assert_not_awaited()
    channel.register_ws.assert_not_awaited()
    channel._on_message_cb.assert_not_awaited()


@pytest.mark.parametrize('patch', [
    {'request_id': 'other'}, {'channel_id': 'other'}, {'ok': False},
    {'session_id': 'other'}, {'deleted': False}, {'deleted': 'true'},
    {'exit_confirmed': False}, {'exit_confirmed': None},
])
@pytest.mark.asyncio
async def test_wrong_or_unconfirmed_backend_result_never_emits_deleted(deletion, patch):
    _, _, channel, ws, client, completed, _ = deletion
    completed[0] = True
    client.send_request.return_value = response(**patch)
    await dispatch(channel, ws)
    frames = await drain(channel, ws)
    assert frames == [{'type': 'res', 'id': 'delete-1', 'ok': False,
        'payload': {'session_id': 'session', 'deleted': False, 'exit_confirmed': False},
        'error': 'Deletion failed or remains unconfirmed.', 'code': 'DELETE_UNCONFIRMED'}]
    assert 'private' not in json.dumps(frames) and 'hidden' not in json.dumps(frames)


@pytest.mark.parametrize('result', [False, None, 1, 'true'])
@pytest.mark.asyncio
async def test_host_confirmation_must_be_literal_true(deletion, result):
    _, _, channel, ws, client, completed, _ = deletion
    completed[0] = result
    client.send_request.return_value = response()
    await dispatch(channel, ws)
    frames = await drain(channel, ws)
    assert len(frames) == 1 and frames[0]['ok'] is False


@pytest.mark.parametrize('error_site', ['transport', 'host'])
@pytest.mark.asyncio
async def test_errors_are_safe_and_do_not_expose_backend_payload(deletion, monkeypatch, error_site):
    host, _, channel, ws, client, _, _ = deletion
    def fail(*args):
        raise RuntimeError('private secret history')
    if error_site == 'host':
        monkeypatch.setattr(host, 'confirm_deletion_for_permit', fail)
        client.send_request.return_value = response()
    else:
        client.send_request.side_effect = fail
    await dispatch(channel, ws)
    frames = await drain(channel, ws)
    assert len(frames) == 1 and frames[0]['ok'] is False
    assert 'private secret' not in json.dumps(frames)


@pytest.mark.parametrize('change', ['identity', 'receipt', 'permit'])
@pytest.mark.asyncio
async def test_queued_success_rechecks_live_connection_and_exact_receipt(deletion, change):
    _, actor, channel, ws, client, completed, _ = deletion
    completed[0] = True
    client.send_request.return_value = response()
    await dispatch(channel, ws)
    if change == 'identity':
        actor[0] = BOB
    elif change == 'receipt':
        completed[0] = False
    else:
        ws._jiuwen_session_permits['delete-1'] = SimpleNamespace()
    assert await drain(channel, ws) == []


@pytest.mark.asyncio
async def test_identity_changes_during_network_wait_cannot_receive_failure_or_success(deletion):
    _, actor, channel, ws, client, completed, _ = deletion
    async def execute(envelope):
        actor[0] = BOB
        completed[0] = True
        return response()
    client.send_request.side_effect = execute
    await dispatch(channel, ws)
    assert await drain(channel, ws) == []


@pytest.mark.parametrize('extra', [{'files': []}, {'history': []}, {'deleted': True}, {'exit_confirmed': True}])
@pytest.mark.asyncio
async def test_client_fields_do_not_create_receipt_or_side_effects(deletion, extra):
    _, _, channel, ws, client, _, _ = deletion
    await dispatch(channel, ws, **extra)
    frames = await drain(channel, ws)
    assert len(frames) == 1 and frames[0]['ok'] is False
    client.send_request.assert_not_awaited()
    channel._process_files.assert_not_awaited()


@pytest.mark.asyncio
async def test_generic_response_cannot_borrow_delete_permit(deletion):
    _, _, channel, ws, client, completed, _ = deletion
    completed[0] = True
    client.send_request.return_value = response()
    await dispatch(channel, ws)
    await channel.send_response(ws, 'delete-1', ok=True, payload={'metadata': 'private'})
    await channel.send_event(ws, 'session.deleted', {'session_id': 'session', 'history': 'private'}, session_id='session')
    frames = await drain(channel, ws)
    assert len(frames) == 2 and 'private' not in json.dumps(frames)


@pytest.mark.asyncio
async def test_legacy_delete_keeps_existing_unary_contract(deletion, monkeypatch):
    _, _, channel, ws, client, _, checks = deletion
    monkeypatch.setattr(organization_auth, 'configured_authenticator', lambda: None)
    monkeypatch.setattr(organization_auth, 'connection_principal', lambda ws: None)
    client.send_request.return_value = AgentResponse('legacy-fetch', 'web', ok=True, payload={'session_id': 'session'})
    await channel._method_handlers['session.delete'](ws, 'legacy-wire', {'session_id': 'session'}, 'session')
    frames = await drain(channel, ws)
    assert client.send_request.call_args.args[0].request_id.startswith('fetch-')
    assert frames[0] == {'type': 'res', 'id': 'legacy-wire', 'ok': True, 'payload': {'session_id': 'session'}}
    assert not checks


@pytest.mark.asyncio
async def test_confirmation_change_before_enqueue_drops_success(deletion, monkeypatch):
    host, _, channel, ws, client, _, _ = deletion
    values = iter([True, False, False])
    monkeypatch.setattr(host, 'confirm_deletion_for_permit', lambda permit: next(values))
    client.send_request.return_value = response()
    await dispatch(channel, ws)
    assert await drain(channel, ws) == []


@pytest.mark.asyncio
async def test_writer_confirmation_exception_drops_without_logging_private_error(deletion, monkeypatch, caplog):
    host, _, channel, ws, client, completed, _ = deletion
    completed[0] = True
    client.send_request.return_value = response()
    await dispatch(channel, ws)
    def fail(permit):
        raise RuntimeError('private completion authority error')
    monkeypatch.setattr(host, 'confirm_deletion_for_permit', fail)
    assert await drain(channel, ws) == []
    assert 'private completion authority error' not in caplog.text
