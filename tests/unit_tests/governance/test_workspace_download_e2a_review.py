"""Real E2A/client/adapter delivery; synthetic socket bridge, no network service."""
import asyncio
import json
from contextvars import copy_context
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from jiuwenswarm.common.e2a.agent_compat import e2a_to_agent_request
from jiuwenswarm.common.e2a.models import E2AEnvelope
from jiuwenswarm.governance.organization_auth import authenticated_scope, current_identity
from jiuwenswarm.governance.workspace_download import (
    WorkspaceDownloadDenied, check_workspace_send, workspace_send_scope,
)
from jiuwenswarm.governance.project_boundary import authorize_resource_request
from jiuwenswarm.governance.session_boundary import admit_session_request, delivery_scope, set_delivery_permit
from jiuwenswarm.gateway.routing.agent_client import WebSocketAgentServerClient
from jiuwenswarm.gateway.channel_manager.web.container_file_http import attach_container_file_routes
from jiuwenswarm.gateway.channel_manager.web.organization_auth_http import register_organization_auth
from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer
from jiuwenswarm.server.runtime.gateway_adapter import AdapterRegistry, WorkspaceFileAdapter
from jiuwenswarm.agents.harness.common.tools.web_file_download import WebFileDownloadManager
from tests.unit_tests.governance import test_workspace_download as sources

credentials, setup = sources.credentials, sources.setup


@pytest.fixture
def wire_download(setup, monkeypatch):
    s = setup
    monkeypatch.setattr(WebFileDownloadManager, '_instance', s.manager)
    monkeypatch.setattr('jiuwenswarm.gateway.channel_manager.web.workspace_download_http.organization_sharing_host', lambda: s.host)
    client = WebSocketAgentServerClient()
    client._uri = 'ws://127.0.0.1:18081'
    client._server_ready = True
    state = SimpleNamespace(requests=[], responses=[], before_delivery=None, before_receive=None)
    server = AgentWebSocketServer.__new__(AgentWebSocketServer)
    server._organization_session_host = s.host
    server._trusted_identity_resolver = lambda _: current_identity()
    server._adapter_registry = AdapterRegistry()
    server._adapter_registry.register(WorkspaceFileAdapter(sharing_host=s.host, identity_resolver=server._resolve_trusted_identity))

    class DeliveryLock:
        async def __aenter__(self):
            await asyncio.sleep(0)
            if state.before_delivery:
                state.before_delivery()
        async def __aexit__(self, *_):
            return False

    class ServerSocket:
        async def send(self, serialized):
            data = json.loads(serialized)
            state.responses.append(data)
            if state.before_receive:
                state.before_receive()
            await client._message_queues[data['request_id']].put(data)

    class ClientSocket:
        remote_address = ('127.0.0.1', 18081)
        async def send(self, serialized):
            wire = json.loads(serialized)
            principal = s.auth.verify(wire)
            request = e2a_to_agent_request(E2AEnvelope.from_dict(wire))
            state.requests.append(request)
            with authenticated_scope(principal), delivery_scope():
                permit = admit_session_request(request.req_method.value, request.params,
                    identity_resolver=current_identity, host=s.host, envelope_session=request.session_id)
                set_delivery_permit(permit)
                authorize_resource_request(request, current_identity(), access_store=s.access, session_permit=permit)
                assert await server._dispatch_gateway_adapter_request(ServerSocket(), request, DeliveryLock())

    client._ws = ClientSocket()
    channel = SimpleNamespace(channel_id='web', agent_client=client)
    app = FastAPI()
    attach_container_file_routes(app, channel)
    register_organization_auth(app)
    return SimpleNamespace(**locals())


@pytest.mark.asyncio
async def test_actual_e2a_client_codec_and_agentserver_deliver_bounded_owner_chunks(wire_download):
    f = wire_download
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=f.app), base_url='http://local') as web:
        response = await web.get('/file-api/download', params={'token': f.s.token(), 'session_id': 'alice-session'},
            headers={'Authorization': 'Bearer ' + f.s.tokens['alice']})
    assert response.status_code == 200 and response.content == f.s.file.read_bytes()
    assert len(f.state.requests) == 3
    assert all(r.req_method.value == 'file.download_workspace_chunk' and r.session_id == 'alice-session'
        and r.channel_id == 'web' and r.user_id == 'alice' and r.params['limit'] <= 65536 for r in f.state.requests)
    assert len({r.request_id for r in f.state.requests}) == 3
    assert not f.client._message_queues


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['send_lock_revoke', 'received_file_change'])
async def test_actual_e2a_buffer_is_rejected_after_authority_or_file_changes(wire_download, change):
    f = wire_download
    token = f.s.token()
    if change == 'send_lock_revoke':
        f.state.before_delivery = lambda: f.s.auth.revoke(f.s.alice)
    else:
        f.state.before_receive = lambda: f.s.file.write_bytes(b'replaced after server read')
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=f.app, raise_app_exceptions=False), base_url='http://local') as web:
        response = await web.get('/file-api/download', params={'token': token, 'session_id': 'alice-session'},
            headers={'Authorization': 'Bearer ' + f.s.tokens['alice']})
    assert response.status_code in {401, 403}
    assert b'fixture content' not in response.content
    assert len(f.state.requests) == 1 and not f.client._message_queues
    if change == 'send_lock_revoke':
        from jiuwenswarm.gateway.routing.agent_client import parse_agent_server_wire_unary
        result = parse_agent_server_wire_unary(f.state.responses[0])
        assert result.ok is False and 'data' not in result.payload


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['remote_uri', 'remote_peer', 'unknown_client', 'replace_after_receive'])
async def test_http_owner_token_cannot_select_remote_or_replaced_route(wire_download, change):
    f = wire_download
    if change == 'remote_uri':
        f.client._uri = 'ws://192.0.2.1:18081'
    elif change == 'remote_peer':
        f.client._ws.remote_address = ('192.0.2.1', 18081)
    elif change == 'unknown_client':
        f.channel.agent_client = object()
    else:
        f.state.before_receive = lambda: setattr(f.client, '_ws', SimpleNamespace(remote_address=('127.0.0.1', 18081)))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=f.app, raise_app_exceptions=False), base_url='http://local') as web:
        response = await web.get('/file-api/download', params={'token': f.s.token(), 'session_id': 'alice-session'},
            headers={'Authorization': 'Bearer ' + f.s.tokens['alice']})
    assert response.status_code == 403
    assert b'fixture content' not in response.content
    assert len(f.state.requests) == (1 if change == 'replace_after_receive' else 0)
    assert not f.client._message_queues


@pytest.mark.asyncio
async def test_client_lock_wait_cannot_send_original_token_to_replacement_peer(wire_download):
    f = wire_download
    reached = []
    class ReplacementSocket:
        remote_address = ('192.0.2.1', 18081)
        async def send(self, serialized):
            wire = json.loads(serialized)
            reached.append(wire['method'])  # Never retain token/credential assertion in evidence.
            await f.client._message_queues[wire['request_id']].put({
                'request_id': wire['request_id'], 'channel_id': 'web', 'ok': False,
                'payload': {'code': 'FORBIDDEN', 'error': 'synthetic replacement'},
            })

    await f.client._lock.acquire()
    task = None
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=f.app, raise_app_exceptions=False), base_url='http://local') as web:
            task = asyncio.create_task(web.get('/file-api/download',
                params={'token': f.s.token(), 'session_id': 'alice-session'},
                headers={'Authorization': 'Bearer ' + f.s.tokens['alice']}))
            async with asyncio.timeout(3):
                while not f.client._message_queues:
                    await asyncio.sleep(0)
            f.client._ws = ReplacementSocket()
            f.client._lock.release()
            response = await asyncio.wait_for(task, 3)
            assert response.status_code == 403
            assert reached == [], 'Original download request reached a changed socket after client lock wait'
    finally:
        if f.client._lock.locked():
            f.client._lock.release()
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_scope_allows_timeout_child_and_same_request_checks_only_while_active():
    calls = []
    client = object()
    wire = {'method': 'file.download_workspace_chunk', 'request_id': 'original'}

    async def send_checks():
        check_workspace_send(client, wire)
        await asyncio.sleep(0)
        check_workspace_send(client, wire)

    with workspace_send_scope(lambda actual, payload: calls.append((actual, payload['request_id']))):
        await asyncio.wait_for(send_checks(), 1)
    assert calls == [(client, 'original'), (client, 'original')]
    with pytest.raises(WorkspaceDownloadDenied):
        check_workspace_send(client, wire)
    # Lexical reset must also retain the legacy no-scope behavior.
    check_workspace_send(client, {'method': 'session.list', 'request_id': 'legacy'})


@pytest.mark.parametrize('ending', ['normal', 'exception', 'cancellation'])
def test_copied_context_cannot_replay_after_scope_exit(ending):
    calls = []
    client = object()
    wire = {'method': 'file.download_workspace_chunk', 'request_id': 'original'}
    try:
        with workspace_send_scope(lambda *_: calls.append('checked')):
            check_workspace_send(client, wire)
            inherited = copy_context()
            if ending == 'exception':
                raise RuntimeError('synthetic scope failure')
            if ending == 'cancellation':
                raise asyncio.CancelledError()
    except (RuntimeError, asyncio.CancelledError):
        pass
    with pytest.raises(WorkspaceDownloadDenied):
        inherited.run(check_workspace_send, client, wire)
    assert calls == ['checked']
    check_workspace_send(client, {'method': 'session.list'})


@pytest.mark.asyncio
async def test_waiting_child_cannot_replay_after_lexical_exit():
    calls = []
    ready, release = asyncio.Event(), asyncio.Event()
    wire = {'method': 'file.download_workspace_chunk', 'request_id': 'original'}

    async def inherited_child():
        ready.set()
        await release.wait()
        with pytest.raises(WorkspaceDownloadDenied):
            check_workspace_send(None, wire)

    with workspace_send_scope(lambda *_: calls.append('checked')):
        child = asyncio.create_task(inherited_child())
        await asyncio.wait_for(ready.wait(), 1)
    release.set()
    await asyncio.wait_for(child, 1)
    assert calls == []


@pytest.mark.parametrize('replacement', ['different', '', None, 123])
def test_scope_pins_first_request_id_before_final_send(replacement):
    calls = []
    wire = {'method': 'file.download_workspace_chunk', 'request_id': 'original'}
    with workspace_send_scope(lambda _, payload: calls.append(payload['request_id'])):
        check_workspace_send(None, wire)
        with pytest.raises(WorkspaceDownloadDenied):
            check_workspace_send(None, {**wire, 'request_id': replacement})
        check_workspace_send(None, wire)
    assert calls == ['original', 'original']


@pytest.mark.asyncio
async def test_actual_client_final_wire_cannot_replace_bound_request_id(wire_download, monkeypatch):
    from jiuwenswarm.gateway.routing import agent_client

    f = wire_download
    original = agent_client._e2a_to_wire

    def replace_request_id(envelope):
        return {**original(envelope), 'request_id': 'different-final-request'}

    monkeypatch.setattr(agent_client, '_e2a_to_wire', replace_request_id)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=f.app, raise_app_exceptions=False), base_url='http://local') as web:
        response = await asyncio.wait_for(web.get('/file-api/download',
            params={'token': f.s.token(), 'session_id': 'alice-session'},
            headers={'Authorization': 'Bearer ' + f.s.tokens['alice']}), 3)
    assert response.status_code == 403
    assert f.state.requests == [] and f.state.responses == []
    assert not f.client._message_queues
