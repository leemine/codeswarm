"""Opt-in real localhost sockets for Browser Artifact durable Gateway delivery.

No external model or account is used. This validates production wire parsing,
acceptance replies, history, inbox recovery and Web/TUI writers, not UI display.
"""
from __future__ import annotations

import asyncio
import json
import os

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.system, pytest.mark.skipif(
    os.environ.get('RUN_BROWSER_ARTIFACT_GATEWAY') != '1', reason='local Artifact Gateway sockets are opt-in',
)]


@pytest.mark.asyncio
async def test_real_socket_acceptance_and_gateway_restart(tmp_path, monkeypatch):
    import websockets
    from jiuwenswarm.agents.harness.common.tools import send_file_to_user as sfu
    from jiuwenswarm.gateway.channel_manager.base import RobotMessageRouter
    from jiuwenswarm.gateway.channel_manager.channel_manager import ChannelManager
    from jiuwenswarm.gateway.channel_manager.web.web_connect import WebChannel, WebChannelConfig
    from jiuwenswarm.gateway.channel_manager.tui.tui_channel import TuiChannel
    from jiuwenswarm.gateway.message_handler.message_handler import MessageHandler
    from jiuwenswarm.gateway.routing.agent_client import WebSocketAgentServerClient
    from jiuwenswarm.gateway.routing.artifact_delivery import ArtifactDeliveryQueue, ArtifactInbox
    from jiuwenswarm.gateway.routing.keys import AgentRef, RoutingKey, make_delivery_target
    from jiuwenswarm.gateway.routing.session_sharing import SubRole, SessionSharingRegistry
    from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer
    from jiuwenswarm.server.runtime.session import lifecycle, session_history

    sessions = tmp_path / 'sessions'
    sessions.mkdir()
    monkeypatch.setattr(session_history, 'get_agent_sessions_dir', lambda: sessions)
    monkeypatch.setattr(lifecycle, 'get_agent_sessions_dir', lambda: sessions)
    server = object.__new__(AgentWebSocketServer)
    server._current_ws = None
    server._current_send_lock = asyncio.Lock()
    async def agent_connection(ws):
        server._current_ws = ws
        await ws.send(json.dumps({'type': 'event', 'event': 'connection.ack'}))
        async for raw in ws:
            await server._handle_message(ws, raw, server._current_send_lock)

    client = WebSocketAgentServerClient()
    handler = object.__new__(MessageHandler)
    MessageHandler.__init__(handler, client)
    handler._register_agent_server_push_handler()
    manager = ChannelManager(handler)
    path = tmp_path / 'gateway.sqlite3'
    manager._artifact_queue = ArtifactDeliveryQueue(ArtifactInbox(path))
    web = WebChannel(WebChannelConfig(enabled=True), RobotMessageRouter())
    tui = TuiChannel()
    ready = {kind: asyncio.Event() for kind in ('web', 'tui')}
    channel_servers = []
    async def channel_connection(ws, kind, channel):
        key = RoutingKey('alice', kind, 'default', AgentRef.default(), 'session')
        await channel.register_ws(ws, key)
        await handler.get_session_sharing_registry().register('session', SubRole.GODVIEW, key,
            make_delivery_target(kind, ws_id=ws._jiuwen_ws_id))
        ready[kind].set()
        try:
            await ws.wait_closed()
        finally:
            await channel.unregister_ws(ws)

    monkeypatch.setattr(sfu, 'send_runtime_push', server.send_push)
    async with asyncio.timeout(60):
        async with websockets.serve(agent_connection, '127.0.0.1', 0) as agent_listener:
            try:
                await client.connect('ws://127.0.0.1:' + str(agent_listener.sockets[0].getsockname()[1]))
                for kind, channel in [('web', web), ('tui', tui)]:
                    async def accept(ws, kind=kind, channel=channel):
                        await channel_connection(ws, kind, channel)
                    listener = await websockets.serve(accept, '127.0.0.1', 0)
                    channel_servers.append(listener)
                    manager.register_external_channel(kind, channel)
                web_uri, tui_uri = ['ws://127.0.0.1:' + str(item.sockets[0].getsockname()[1]) for item in channel_servers]
                async with websockets.connect(web_uri) as web_ws, websockets.connect(tui_uri) as tui_ws:
                    await asyncio.gather(*(event.wait() for event in ready.values()))
                    artifact = tmp_path / 'artifact.txt'
                    artifact.write_text('R1-10D_DURABLE_GATEWAY')
                    toolkit = sfu.SendFileToolkit(request_id='original', session_id='session', channel_id='web')
                    await toolkit.deliver_projected_artifact(artifact, {'artifactId': 'live', 'metadata': {}})
                    rows = session_history.load_history_records('session')
                    assert any(row.get('acceptance') == 'gateway_durable_v1' for row in rows)
                    # Acceptance precedes any output send; lose Gateway volatile state here.
                    assert len(manager._artifact_queue.inbox.pending()) == 2
                    recovered = ChannelManager(handler)
                    recovered._artifact_queue = ArtifactDeliveryQueue(ArtifactInbox(path))
                    recovered.register_external_channel('web', web)
                    recovered.register_external_channel('tui', tui)
                    handler._session_sharing = SessionSharingRegistry()
                    await recovered._artifact_queue.drain(recovered._send_artifact_target)
                    web_file, tui_file = await asyncio.gather(web_ws.recv(), tui_ws.recv())
                    frames = [json.loads(web_file), json.loads(tui_file)]
                    assert all(frame['event'] == 'chat.file' for frame in frames)
                    assert len({frame['payload']['delivery_id'] for frame in frames}) == 1
                    assert recovered._artifact_queue.inbox.pending() == []
                    await toolkit.replay_projected_artifacts()
                    assert len([row for row in session_history.load_history_records('session')
                                if row.get('event_type') == 'chat.file']) == 1
            finally:
                await client.disconnect()
                for listener in channel_servers:
                    listener.close()
                await asyncio.gather(*(listener.wait_closed() for listener in channel_servers))
                assert session_history.flush_pending_writes()


@pytest.mark.asyncio
async def test_offline_history_auto_recovery_and_original_recipient_real_sockets(tmp_path, monkeypatch):
    import websockets
    from jiuwenswarm.agents.harness.common.tools import send_file_to_user as sfu
    from jiuwenswarm.common.e2a.constants import E2A_ARTIFACT_ORIGIN_KEY
    from jiuwenswarm.gateway.channel_manager.channel_manager import ChannelManager
    from jiuwenswarm.gateway.channel_manager.tui.tui_channel import TuiChannel
    from jiuwenswarm.gateway.message_handler.message_handler import MessageHandler
    from jiuwenswarm.gateway.routing.agent_client import WebSocketAgentServerClient
    from jiuwenswarm.gateway.routing.artifact_delivery import ArtifactDeliveryQueue, ArtifactInbox, freeze_origin
    from jiuwenswarm.gateway.routing.keys import AgentRef, RoutingKey
    from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer
    from jiuwenswarm.server.gateway_push.artifact_outbox import ArtifactOutbox
    from jiuwenswarm.server.runtime.session import lifecycle, session_history, session_metadata

    sessions = tmp_path / 'sessions'
    sessions.mkdir()
    for module in (lifecycle, session_history, session_metadata):
        monkeypatch.setattr(module, 'get_agent_sessions_dir', lambda: sessions)
    server = object.__new__(AgentWebSocketServer)
    server._current_ws = None
    server._current_send_lock = asyncio.Lock()
    monkeypatch.setattr(sfu, 'send_runtime_push', server.send_push)
    key = RoutingKey('alice', 'tui', 'default', AgentRef.default(), 'offline-session')
    toolkit = sfu.SendFileToolkit(request_id='original-offline', session_id=key.session_id,
        channel_id='tui', metadata={E2A_ARTIFACT_ORIGIN_KEY: freeze_origin(key)})
    artifact = tmp_path / 'offline.txt'
    artifact.write_text('R1-10D_OFFLINE_RECOVERY')
    with pytest.raises(RuntimeError, match='not accepted'):
        await toolkit.deliver_projected_artifact(artifact, {'artifactId': 'offline', 'metadata': {}})

    async def agent_connection(ws):
        server._current_ws = ws
        await ws.send(json.dumps({'type': 'event', 'event': 'connection.ack'}))
        try:
            async for raw in ws:
                await server._handle_message(ws, raw, server._current_send_lock)
        finally:
            server._release_current_connection(ws)

    client = WebSocketAgentServerClient()
    handler = object.__new__(MessageHandler)
    MessageHandler.__init__(handler, client)
    handler._register_agent_server_push_handler()
    manager = ChannelManager(handler)
    path = tmp_path / 'gateway.sqlite3'
    manager._artifact_queue = ArtifactDeliveryQueue(ArtifactInbox(path))
    worker = ArtifactOutbox(server.send_push, retry_seconds=.02)
    ready = asyncio.Queue()
    channel = TuiChannel()
    async def recipient_connection(ws):
        user = await ws.recv()
        recipient_key = RoutingKey(user, 'tui', 'default', AgentRef.default(), key.session_id)
        await channel.register_ws(ws, recipient_key)
        await ready.put(user)
        try:
            await ws.wait_closed()
        finally:
            await channel.unregister_ws(ws)

    async with asyncio.timeout(60):
        async with websockets.serve(agent_connection, '127.0.0.1', 0) as listener:
            try:
                await client.connect('ws://127.0.0.1:' + str(listener.sockets[0].getsockname()[1]))
                # Recreated resident service discovers committed history; no Turn starts.
                worker.start()
                while not any(row.get('acceptance') == 'gateway_durable_v1'
                              for row in session_history.load_history_records(key.session_id)):
                    await asyncio.sleep(.02)
                await worker.close()
                assert len(manager._artifact_queue.inbox.pending()) == 1
                recovered = ChannelManager(handler)
                recovered._artifact_queue = ArtifactDeliveryQueue(ArtifactInbox(path))
                recovered.register_external_channel('tui', channel)
                async with websockets.serve(recipient_connection, '127.0.0.1', 0) as recipients:
                    uri = 'ws://127.0.0.1:' + str(recipients.sockets[0].getsockname()[1])
                    async with websockets.connect(uri) as bob:
                        await bob.send('bob')
                        assert await ready.get() == 'bob'
                        await recovered._artifact_queue.drain(recovered._send_artifact_target)
                        assert len(recovered._artifact_queue.inbox.pending()) == 1
                        async with websockets.connect(uri) as alice:
                            await alice.send('alice')
                            assert await ready.get() == 'alice'
                            await recovered._artifact_queue.drain(recovered._send_artifact_target)
                            frame = json.loads(await asyncio.wait_for(alice.recv(), 3))
                            assert frame['event'] == 'chat.file'
                            assert frame['payload']['files'][0]['name'] == 'offline.txt'
                            assert recovered._artifact_queue.inbox.pending() == []
                        with pytest.raises(TimeoutError):
                            await asyncio.wait_for(bob.recv(), .1)
                assert len([row for row in session_history.load_history_records(key.session_id)
                            if row.get('event_type') == 'chat.file']) == 1
            finally:
                await worker.close()
                await client.disconnect()
                assert session_history.flush_pending_writes()
