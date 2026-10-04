"""Current owner and fixed share authorization at request and final send."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests.unit_tests.server import test_sharing_host as host_tests
from tests.unit_tests.server.test_sharing_host import grant, ALICE, BOB

from jiuwenswarm.governance import organization_auth, session_boundary
from jiuwenswarm.governance.session_boundary import admit_session_request
from jiuwenswarm.server.runtime.gateway_adapter.shared_history_adapter import SharedHistoryAdapter


setup = host_tests.setup

def request(params):
    return SimpleNamespace(params=params, request_id='read-1', channel_id='web', metadata={})


def test_share_read_fixed_range_and_cursor_identity(setup):
    host, _, _, directory, _, _, _ = setup
    with (directory/'history.jsonl').open('a') as stream:
        for index in range(4):
            stream.write(json.dumps({'id': str(index), 'role': 'assistant', 'content': f'text {index}',
                                     'files': {'private_path': '/secret'}, 'media_items': ['https://private']})+'\n')
    scope = host.prepare_source('session', ALICE)
    share = grant(host, scope)
    actor = [BOB]
    adapter = SharedHistoryAdapter(host.store, identity_resolver=lambda _: actor[0])
    params = {'session_id': 'session', 'share_id': share['share_id'], 'limit': 2}
    page = adapter._read(request(params))
    assert [m['content'] for m in page.payload['messages']] == ['text 3', 'text 2']
    assert all(set(m) <= {'id', 'role', 'content'} for m in page.payload['messages'])
    cursor = page.payload['next_cursor']
    assert cursor and '/secret' not in json.dumps(page.payload)
    with (directory/'history.jsonl').open('a') as stream:
        stream.write('{"role":"assistant","content":"future private"}\n')
    next_page = adapter._read(request({**params, 'cursor': cursor}))
    assert [m['content'] for m in next_page.payload['messages']] == ['text 1', 'text 0']
    actor[0] = ALICE
    with pytest.raises(PermissionError):
        adapter._read(request({**params, 'cursor': cursor}))
    actor[0] = BOB
    host.store.revoke(share['share_id'], ALICE, expected_revision=1)
    with pytest.raises(PermissionError):
        page._delivery_guard()
    with pytest.raises(PermissionError):
        adapter._read(request({**params, 'cursor': cursor}))


@pytest.mark.parametrize('method', ['history.get', 'session.switch', 'chat.send', 'session.restore_files',
                                   'session.fork', 'team.history.get', 'unknown.method'])
def test_view_recipient_cannot_enter_owner_or_unknown_methods(setup, method):
    host, _, _, _, _, _, scope = setup
    share = grant(host, scope)
    with pytest.raises(PermissionError):
        admit_session_request(method, {'session_id': 'session', 'share_id': share['share_id']},
                              identity_resolver=lambda: BOB, host=host)


def test_owner_permit_rechecks_source_epoch_and_credential(setup):
    host, _, _, _, _, _, _ = setup
    actor = [ALICE]
    permit = admit_session_request('history.get', {'session_id': 'session'}, identity_resolver=lambda: actor[0], host=host)
    assert permit.revalidate()
    actor[0] = BOB
    assert not permit.revalidate()
    actor[0] = ALICE
    host.invalidate_source('session', expected_epoch=host.source_epoch('session'))
    assert not permit.revalidate()


@pytest.mark.asyncio
async def test_server_final_send_replaces_private_buffer_after_revoke(setup, monkeypatch):
    from jiuwenswarm.server.ws_send import send_wire_payload
    host, _, _, _, _, _, scope = setup
    share = grant(host, scope)
    monkeypatch.setattr(organization_auth, 'configured_authenticator', lambda: object())
    permit = admit_session_request('session.share.history.get', {'session_id': 'session', 'share_id': share['share_id']},
                                  identity_resolver=lambda: BOB, host=host)
    ws = SimpleNamespace(send=AsyncMock())
    with session_boundary.delivery_scope():
        session_boundary.set_delivery_permit(permit)
        host.store.revoke(share['share_id'], ALICE, expected_revision=1)
        await send_wire_payload(ws, {'request_id': 'one', 'channel': 'web', 'payload': {'secret': 'private body'}})
    sent = ws.send.call_args.args[0]
    assert 'private body' not in sent and 'FORBIDDEN' in sent


@pytest.mark.asyncio
async def test_gateway_writer_rechecks_queued_share_and_no_subscription(setup, monkeypatch):
    from jiuwenswarm.gateway.channel_manager.base import RobotMessageRouter
    from jiuwenswarm.gateway.channel_manager.web.web_connect import WebChannel, WebChannelConfig
    host, _, _, _, _, _, scope = setup
    share = grant(host, scope)
    monkeypatch.setattr(organization_auth, 'configured_authenticator', lambda: object())
    monkeypatch.setattr(organization_auth, 'connection_principal', lambda ws: SimpleNamespace(identity=lambda: BOB))
    monkeypatch.setattr(session_boundary, 'organization_sharing_host', lambda: host)
    permit = admit_session_request('session.share.history.get', {'session_id': 'session', 'share_id': share['share_id']},
                                  identity_resolver=lambda: BOB, host=host)
    ws = SimpleNamespace(_jiuwen_ws_id='browser', closed=False, send=AsyncMock(),
                         _jiuwen_session_permits={'one': permit})
    channel = WebChannel(WebChannelConfig(enabled=True), RobotMessageRouter())
    queue = asyncio.Queue()
    channel._send_queues['browser'] = queue
    channel._enqueue_send(ws, {'type': 'res', 'id': 'one', 'ok': True, 'payload': {'secret': 'private body'}})
    assert queue.qsize() == 1
    host.store.revoke(share['share_id'], ALICE, expected_revision=1)
    queue.put_nowait(None)
    await channel._writer_loop(ws, 'browser')
    ws.send.assert_not_awaited()
    assert not channel._clients_by_key


@pytest.mark.asyncio
async def test_unknown_ws_request_never_processes_files_or_registers_session(setup, monkeypatch):
    from jiuwenswarm.gateway.channel_manager.base import RobotMessageRouter
    from jiuwenswarm.gateway.channel_manager.web.web_connect import WebChannel, WebChannelConfig
    host, _, _, _, _, _, _ = setup
    monkeypatch.setattr(organization_auth, 'configured_authenticator', lambda: object())
    monkeypatch.setattr(organization_auth, 'connection_principal', lambda ws: SimpleNamespace(identity=lambda: BOB))
    monkeypatch.setattr(session_boundary, 'organization_sharing_host', lambda: host)
    channel = WebChannel(WebChannelConfig(enabled=True), RobotMessageRouter())
    channel.send_response = AsyncMock()
    channel._process_files = AsyncMock(side_effect=AssertionError('file side effect'))
    channel.register_ws = AsyncMock(side_effect=AssertionError('subscription side effect'))
    ws = SimpleNamespace()
    await channel._handle_authenticated_raw_message(ws, json.dumps({
        'type': 'req', 'id': 'bad', 'method': 'unknown.method', 'params': {'session_id': 'session'}}), {})
    assert channel.send_response.call_args.kwargs['code'] == 'FORBIDDEN'
    channel._process_files.assert_not_awaited()
    channel.register_ws.assert_not_awaited()


def test_owner_permit_never_revives_after_acl_restore(setup):
    host, access, project_id, _, _, _, _ = setup
    permit = admit_session_request('history.get', {'session_id': 'session'}, identity_resolver=lambda: ALICE, host=host)
    access.replace_acl(project_id, 'admin', acl={}, expected_revision=2)
    assert not permit.revalidate()
    access.replace_acl(project_id, 'admin', acl={'alice': ['read', 'admin']}, expected_revision=3)
    assert host.owner_current('session', ALICE)
    assert not permit.revalidate()


@pytest.mark.parametrize('method', ['session.list', 'project.list', 'project.get_sessions', 'session.share.list'])
def test_inventory_buffer_invalidated_by_authority_change(setup, method):
    host, access, project_id, _, _, _, _ = setup
    permit = admit_session_request(method, {}, identity_resolver=lambda: ALICE, host=host)
    assert permit.revalidate()
    access.replace_acl(project_id, 'admin', acl={}, expected_revision=2)
    assert not permit.revalidate()
    access.replace_acl(project_id, 'admin', acl={'alice': ['read', 'admin']}, expected_revision=3)
    assert not permit.revalidate()


@pytest.mark.asyncio
async def test_service_ready_control_is_fixed_without_user_permit(monkeypatch):
    from jiuwenswarm.server.ws_send import send_service_ready
    monkeypatch.setattr(organization_auth, 'configured_authenticator', lambda: object())
    ws = SimpleNamespace(send=AsyncMock())
    await send_service_ready(ws, heartbeat_protocol=1, heartbeat_ready=True)
    wire = json.loads(ws.send.call_args.args[0])
    assert wire == {'type': 'event', 'event': 'connection.ack', 'payload': {
        'status': 'ready', 'heartbeat_job_owner': 'agentserver', 'heartbeat_job_protocol': 1,
        'heartbeat_job_ready': True}}
    with pytest.raises(TypeError):
        await send_service_ready(ws, heartbeat_protocol=1, heartbeat_ready=True, payload={'secret': 'x'})


def test_publication_uses_scoped_identity_and_cas_receipt(setup):
    from jiuwenswarm.governance.session_publication import SessionOwnerPublication
    host, access, project_id, _, _, _, _ = setup
    access.replace_acl(project_id, 'admin', acl={'alice': ['read', 'admin', 'execute']}, expected_revision=2)
    lifecycle = SessionOwnerPublication(host)
    with pytest.raises(PermissionError):
        lifecycle.before_publish('new-session', project_id, True)
    with lifecycle.scope(lambda: BOB), pytest.raises(PermissionError):
        lifecycle.before_publish('new-session', project_id, True)
    with lifecycle.scope(lambda: ALICE):
        rollback = lifecycle.before_publish('new-session', project_id, True)
    assert host.store.registered_owner('new-session')[0] == ALICE
    rollback()
    assert host.store.registered_owner('new-session') is None
    with lifecycle.scope(lambda: ALICE), pytest.raises(ValueError):
        lifecycle.before_publish('new-session', project_id, True)


def test_publication_rollback_cannot_erase_new_source(setup):
    from jiuwenswarm.governance.session_publication import SessionOwnerPublication
    host, access, project_id, _, _, _, _ = setup
    access.replace_acl(project_id, 'admin', acl={'alice': ['read', 'admin', 'execute']}, expected_revision=2)
    lifecycle = SessionOwnerPublication(host)
    with lifecycle.scope(lambda: ALICE):
        rollback = lifecycle.before_publish('new-session', project_id, True)
    host.invalidate_source('new-session', expected_epoch=1)
    with pytest.raises(PermissionError):
        rollback()
    assert host.store.registered_owner('new-session')[0] == ALICE


@pytest.mark.asyncio
async def test_service_handshake_has_only_fixed_host_metadata(setup, monkeypatch):
    from jiuwenswarm.server.ws_send import send_service_ready
    monkeypatch.setattr(organization_auth, 'configured_authenticator', lambda: object())
    ws = SimpleNamespace(send=AsyncMock())
    with session_boundary.delivery_scope():
        await send_service_ready(ws, heartbeat_protocol=1, heartbeat_ready=True)
    assert json.loads(ws.send.call_args.args[0]) == {
        'type': 'event', 'event': 'connection.ack', 'payload': {
            'status': 'ready', 'heartbeat_job_owner': 'agentserver',
            'heartbeat_job_protocol': 1, 'heartbeat_job_ready': True,
        },
    }
    with pytest.raises(ValueError):
        await send_service_ready(ws, heartbeat_protocol='/private/path', heartbeat_ready=True)


@pytest.mark.asyncio
async def test_created_session_response_captures_owner_before_queue(setup, monkeypatch):
    from jiuwenswarm.gateway.channel_manager.base import RobotMessageRouter
    from jiuwenswarm.gateway.channel_manager.web.web_connect import WebChannel, WebChannelConfig
    from jiuwenswarm.gateway.routing.base_ws_channel import _AuthorizedFrame
    host, _, _, _, _, _, _ = setup
    monkeypatch.setattr(organization_auth, 'configured_authenticator', lambda: object())
    monkeypatch.setattr(organization_auth, 'connection_principal', lambda ws: SimpleNamespace(identity=lambda: ALICE))
    permit = admit_session_request('session.create', {}, identity_resolver=lambda: ALICE, host=host)
    ws = SimpleNamespace(_jiuwen_ws_id='creator', closed=False,
                         _jiuwen_session_permits={'create': permit})
    channel = WebChannel(WebChannelConfig(enabled=True), RobotMessageRouter())
    queue = asyncio.Queue()
    channel._send_queues['creator'] = queue
    channel._enqueue_send(ws, {'type': 'res', 'id': 'create', 'ok': True,
                              'payload': {'session_id': 'session', 'projectDir': '/private'}})
    frame = queue.get_nowait()
    assert isinstance(frame, _AuthorizedFrame)
    assert frame.guard()
    host.invalidate_source('session', expected_epoch=host.source_epoch('session'))
    assert not frame.guard()
    assert permit.revalidate()  # A create request alone has no new Session owner.
