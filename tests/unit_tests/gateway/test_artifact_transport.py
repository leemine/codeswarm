"""Artifact receipts require durable Gateway acceptance and actual channel writes."""
import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.common.e2a.constants import E2A_ARTIFACT_ACCEPTANCE_KEY, E2A_ARTIFACT_ACCEPTED_EVENT
from jiuwenswarm.common.schema.message import Message, EventType
from jiuwenswarm.gateway.channel_manager.base import RobotMessageRouter
from jiuwenswarm.gateway.channel_manager.tui.tui_channel import TuiChannel
from jiuwenswarm.gateway.channel_manager.web.web_connect import WebChannel, WebChannelConfig
from jiuwenswarm.gateway.routing.agent_client import WebSocketAgentServerClient
from jiuwenswarm.gateway.routing.artifact_delivery import freeze_message, restore_message, freeze_target, restore_target
from jiuwenswarm.gateway.routing.keys import AgentRef, RoutingKey, make_delivery_target
from jiuwenswarm.gateway.routing.session_sharing import RoutingTarget
from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer


class Socket:
    closed = False
    def __init__(self):
        self.sent = []
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()
        self.fail = False

    async def send(self, wire):
        self.entered.set()
        await self.release.wait()
        if self.fail:
            raise ConnectionError('disconnected')
        self.sent.append(json.loads(wire))


def artifact_message(channel='web'):
    return Message(id='request', type='event', channel_id=channel, session_id='session',
                   params={}, timestamp=1, ok=True, event_type=EventType.CHAT_FILE,
                   agent_ref=AgentRef.default(), metadata={},
                   payload={'event_type': 'chat.file', 'delivery_id': 'browser-artifact:a',
                            'files': [{'name': 'file.txt', 'url': '/file.txt'}]})


def test_persistent_message_and_target_roundtrip():
    msg = artifact_message()
    msg.metadata = {'app_id': 'default', 'access_token': 'private-token',
                    E2A_ARTIFACT_ACCEPTANCE_KEY: 'private-nonce'}
    frozen = freeze_message(msg)
    assert 'private-token' not in json.dumps(frozen)
    assert 'private-nonce' not in json.dumps(frozen)
    restored = restore_message(frozen)
    assert restored.payload == msg.payload
    assert restored.mode == msg.mode
    assert restored.agent_ref == msg.agent_ref
    key = RoutingKey('user', 'web', 'default', AgentRef.default(), 'session')
    target = RoutingTarget('godview', routing_keys=[key], delivery=make_delivery_target('web', ws_id='old'))
    restored_target = restore_target(freeze_target('web', 'default', target))
    assert restored_target.routing_keys == [key]
    assert restored_target.delivery.ws_id == ''


@pytest.mark.asyncio
async def test_push_requires_receipt_from_original_connection_and_nonce():
    server = AgentWebSocketServer.__new__(AgentWebSocketServer)
    ws = server._current_ws = Socket()
    server._current_send_lock = asyncio.Lock()
    task = asyncio.create_task(server.send_push({'request_id': 'r', 'channel_id': 'web',
        'session_id': 's', 'payload': artifact_message().payload}))
    await asyncio.wait_for(ws.entered.wait(), 1)
    assert not task.done()
    nonce = ws.sent[0]['metadata'][E2A_ARTIFACT_ACCEPTANCE_KEY]
    async def acknowledge(socket, value):
        await server._handle_message(socket, json.dumps({'type': 'event', 'event': E2A_ARTIFACT_ACCEPTED_EVENT,
            'payload': {'nonce': value}}), asyncio.Lock())
    await acknowledge(Socket(), nonce)
    await acknowledge(ws, 'old-nonce')
    assert not task.done()
    await acknowledge(ws, nonce)
    assert await asyncio.wait_for(task, 1) is True
    assert server._artifact_acceptance_receipts._pending == {}
    await acknowledge(ws, nonce)  # Duplicate/stale receipt is harmless.


@pytest.mark.asyncio
async def test_cancelled_push_discards_connection_receipt():
    server = AgentWebSocketServer.__new__(AgentWebSocketServer)
    server._current_ws = Socket()
    server._current_send_lock = asyncio.Lock()
    task = asyncio.create_task(server.send_push({'channel_id': 'web', 'payload': artifact_message().payload}))
    await asyncio.wait_for(server._current_ws.entered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert server._artifact_acceptance_receipts._pending == {}


@pytest.mark.asyncio
@pytest.mark.parametrize('accepted', [True, False, None, OSError('disk failure')])
async def test_gateway_receipt_only_after_durable_acceptance(accepted):
    client = WebSocketAgentServerClient.__new__(WebSocketAgentServerClient)
    client._on_server_push = AsyncMock(**({'side_effect': accepted} if isinstance(accepted, Exception)
                                         else {'return_value': accepted}))
    ws = Socket()
    client._ws = Socket()  # A replacement connection must not receive old acknowledgements.
    await client._dispatch_server_push({'metadata': {E2A_ARTIFACT_ACCEPTANCE_KEY: 'nonce'}}, ws)
    assert len(ws.sent) == int(accepted is True)
    assert client._ws.sent == []


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['web', 'tui'])
@pytest.mark.parametrize('fail', [False, True])
async def test_confirmed_channel_waits_for_writer_and_propagates_failure(kind, fail):
    channel = WebChannel(WebChannelConfig(enabled=True), RobotMessageRouter()) if kind == 'web' else TuiChannel()
    ws = Socket()
    ws.release.clear()
    ws.fail = fail
    key = RoutingKey('user', kind, 'default', AgentRef.default(), 'session')
    await channel.register_ws(ws, key)
    try:
        routing = RoutingTarget('godview', routing_keys=[key], delivery=make_delivery_target(kind))
        task = asyncio.create_task(channel.send_confirmed(artifact_message(kind), routing_target=routing))
        await asyncio.wait_for(ws.entered.wait(), 1)
        assert not task.done()
        ws.release.set()
        if fail:
            with pytest.raises(ConnectionError):
                await asyncio.wait_for(task, 1)
        else:
            await asyncio.wait_for(task, 1)
            assert ws.sent[0]['payload']['delivery_id'] == 'browser-artifact:a'
    finally:
        ws.release.set()
        await channel.unregister_ws(ws)


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['web', 'tui'])
async def test_missing_frozen_recipient_never_falls_back_to_other_session_viewer(kind):
    channel = WebChannel(WebChannelConfig(enabled=True), RobotMessageRouter()) if kind == 'web' else TuiChannel()
    ws = Socket()
    viewer = RoutingKey('other-user', kind, 'default', AgentRef.default(), 'session')
    intended = RoutingKey('original-user', kind, 'default', AgentRef.default(), 'session')
    await channel.register_ws(ws, viewer)
    try:
        routing = RoutingTarget('godview', routing_keys=[intended], delivery=make_delivery_target(kind))
        with pytest.raises(ConnectionError):
            await channel.send_confirmed(artifact_message(kind), routing_target=routing)
        assert ws.sent == []
    finally:
        await channel.unregister_ws(ws)


@pytest.mark.asyncio
async def test_gateway_pipeline_acceptance_restart_and_exact_target_retry(tmp_path):
    from jiuwenswarm.gateway.channel_manager.channel_manager import ChannelManager
    from jiuwenswarm.gateway.message_handler.message_handler import MessageHandler
    from jiuwenswarm.gateway.routing.artifact_delivery import ArtifactInbox, ArtifactDeliveryQueue
    from jiuwenswarm.gateway.routing.session_sharing import SessionSharingRegistry, SubRole
    from jiuwenswarm.server.gateway_push.wire import build_server_push_wire

    # Use the production push parser, routing policy, durable queue and both writers.
    handler = object.__new__(MessageHandler)
    MessageHandler.__init__(handler, WebSocketAgentServerClient())
    handler._session_sharing = SessionSharingRegistry()
    manager = ChannelManager(handler)
    path = tmp_path / 'gateway.sqlite3'
    manager._artifact_queue = ArtifactDeliveryQueue(ArtifactInbox(path))
    web = WebChannel(WebChannelConfig(enabled=True), RobotMessageRouter())
    tui = TuiChannel()
    sockets = []
    for kind, channel in [('web', web), ('tui', tui)]:
        ws = Socket()
        key = RoutingKey('alice', kind, 'default', AgentRef.default(), 'session')
        await channel.register_ws(ws, key)
        await handler._session_sharing.register('session', SubRole.GODVIEW, key,
            make_delivery_target(kind, ws_id=ws._jiuwen_ws_id))
        manager.register_external_channel(kind, channel)
        sockets.append((channel, ws, key))
    wire = build_server_push_wire({'request_id': 'request', 'channel_id': 'web', 'session_id': 'session',
        'payload': artifact_message().payload})
    wire['metadata'][E2A_ARTIFACT_ACCEPTANCE_KEY] = 'private-nonce'
    try:
        assert await handler._handle_agent_server_push(wire) is True
        pending = manager._artifact_queue.inbox.pending()
        assert len(pending) == 2
        assert 'private-nonce' not in json.dumps(pending)
        sockets[1][1].fail = True
        await manager._artifact_queue.drain(manager._send_artifact_target)
        assert len(sockets[0][1].sent) == 1
        assert len(sockets[1][1].sent) == 0
        await tui.unregister_ws(sockets[1][1])
        replacement = Socket()
        await tui.register_ws(replacement, sockets[1][2])
        sockets[1] = (tui, replacement, sockets[1][2])
        # Lose all volatile Gateway subscriptions and reconstruction metadata.
        handler._session_sharing = SessionSharingRegistry()
        recovered = ChannelManager(handler)
        recovered._artifact_queue = ArtifactDeliveryQueue(ArtifactInbox(path))
        recovered.register_external_channel('web', web)
        recovered.register_external_channel('tui', tui)
        assert await handler._handle_agent_server_push(wire) is True
        await recovered._artifact_queue.drain(recovered._send_artifact_target)
        assert len(sockets[0][1].sent) == 1
        assert len(replacement.sent) == 1
        assert replacement.sent[0]['payload']['delivery_id'] == 'browser-artifact:a'
        assert recovered._artifact_queue.inbox.pending() == []
    finally:
        for channel, ws, _ in sockets:
            await channel.unregister_ws(ws)
