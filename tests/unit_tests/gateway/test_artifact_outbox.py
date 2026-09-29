"""Recovery must not need a new Turn or select the current session audience."""
import asyncio

import pytest

from jiuwenswarm.common.e2a.constants import E2A_ARTIFACT_ORIGIN_KEY
from jiuwenswarm.gateway.routing.artifact_delivery import freeze_origin
from jiuwenswarm.gateway.routing.keys import AgentRef, RoutingKey
from jiuwenswarm.server.gateway_push.artifact_outbox import ArtifactOutbox


@pytest.fixture
def isolated_history(tmp_path, monkeypatch):
    from jiuwenswarm.server.runtime.session import lifecycle, session_history, session_metadata
    sessions = tmp_path / 'sessions'
    sessions.mkdir()
    for module in (lifecycle, session_history, session_metadata):
        monkeypatch.setattr(module, 'get_agent_sessions_dir', lambda: sessions)
    yield session_history
    assert session_history.flush_pending_writes()


async def seed(history, session='original', *, origin=True):
    key = RoutingKey('alice', 'web', 'original-app', AgentRef.default(), session)
    receipt = history.append_history_record_durable(
        session_id=session, request_id='original-request', channel_id='web',
        role='assistant', content='', event_type='chat.file', timestamp=1,
        delivery_id='browser-artifact:file', extra={
            'files': [{'name': 'a.txt'}],
            'artifact_route_metadata': {E2A_ARTIFACT_ORIGIN_KEY: freeze_origin(key)} if origin else {},
        })
    await history.wait_for_history_receipt(receipt)


async def eventually(predicate):
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(.01)


@pytest.mark.asyncio
async def test_cold_server_replays_history_without_turn_and_ack_only_after_gateway(isolated_history):
    history = isolated_history
    await seed(history)
    attempts = []
    async def offline(message):
        attempts.append(message)
        return False
    first = ArtifactOutbox(offline, retry_seconds=.01)
    first.start()
    try:
        await eventually(lambda: attempts)
        assert not any(r.get('acceptance') for r in history.load_history_records('original'))
    finally:
        await first.close()
    received = []
    async def online(message):
        received.append(message)
        return True
    second = ArtifactOutbox(online, retry_seconds=.01)
    second.start()
    try:
        await eventually(lambda: any(r.get('acceptance') for r in history.load_history_records('original')))
        assert len(received) == 1
        assert received[0]['request_id'] == 'original-request'
        assert received[0]['metadata'][E2A_ARTIFACT_ORIGIN_KEY]['app_id'] == 'original-app'
    finally:
        await second.close()
    assert first._task is second._task is None


@pytest.mark.asyncio
async def test_legacy_unknown_recipient_and_lifecycle_block_do_not_auto_send(isolated_history):
    from jiuwenswarm.server.runtime.session import lifecycle
    history = isolated_history
    await seed(history, 'legacy', origin=False)
    await seed(history, 'blocked')
    with lifecycle.resource_lock('session', 'blocked'):
        lifecycle.save_locked('session', 'blocked', {'blocked': True, 'write_blocked': True})
    sent = []
    async def push(message):
        sent.append(message)
        return True
    worker = ArtifactOutbox(push, retry_seconds=.01)
    worker.start()
    try:
        await eventually(lambda: worker._discovered and not worker._pending)
        assert sent == []
    finally:
        await worker.close()


@pytest.mark.asyncio
async def test_new_file_is_queued_after_history_commit_and_bound_to_own_host(isolated_history, tmp_path):
    from jiuwenswarm.agents.harness.common.tools.send_file_to_user import SendFileToolkit
    from jiuwenswarm.runtime import host_services
    history = isolated_history
    entered, release = asyncio.Event(), asyncio.Event()
    sent = []
    async def own_push(message):
        assert any(row.get('event_type') == 'chat.file' for row in history.load_history_records('new'))
        entered.set()
        await release.wait()
        sent.append(message)
        return True
    worker = ArtifactOutbox(own_push, retry_seconds=.01)
    worker.start()
    async def wrong_push(message):
        raise AssertionError('outbox must not use a later global push owner')
    previous = host_services.install_runtime_push_handler(wrong_push)
    try:
        key = RoutingKey('alice', 'web', 'default', AgentRef.default(), 'new')
        toolkit = SendFileToolkit(request_id='r', session_id='new', channel_id='web',
            metadata={E2A_ARTIFACT_ORIGIN_KEY: freeze_origin(key)})
        path = tmp_path / 'a.txt'
        path.write_text('queued')
        result = await toolkit.deliver_projected_artifact(path, {'artifactId': 'new', 'metadata': {}})
        assert '等待投递' in result
        await asyncio.wait_for(entered.wait(), 5)
        assert not any(row.get('acceptance') for row in history.load_history_records('new'))
        release.set()
        await eventually(lambda: any(row.get('acceptance') for row in history.load_history_records('new')))
        assert len(sent) == 1
    finally:
        host_services.restore_runtime_push_handler(wrong_push, previous)
        await worker.close()


@pytest.mark.asyncio
async def test_server_stop_keeps_retry_owner_until_runtime_drain_finishes(isolated_history):
    from unittest.mock import AsyncMock
    from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer
    from jiuwenswarm.runtime.host_services import enqueue_artifact_retry
    worker = ArtifactOutbox(AsyncMock(return_value=False))
    worker._discovered = True
    worker.start()
    server = object.__new__(AgentWebSocketServer)
    server._artifact_outbox = worker
    async def drain():
        assert enqueue_artifact_retry('last-file')
    server._stop_main_services = drain
    server._stop_personal_context_best_effort = AsyncMock()
    await server.stop()
    assert server._artifact_outbox is None
    assert worker._task is None
    assert not enqueue_artifact_retry('after-stop')
    server._stop_personal_context_best_effort.assert_awaited_once()
