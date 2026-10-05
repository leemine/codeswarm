"""Consumer ownership and confirmed transport exit, using owned asyncio tasks."""

import asyncio
from types import SimpleNamespace

import pytest

from jiuwenswarm.governance.contracts import TrustedIdentity
from jiuwenswarm.governance.tool_resources import ResourceExecutionContext
from jiuwenswarm.runtime.harness.execution_session import (
    ExecutionExitState,
    ExecutionExitUnconfirmedError,
    ExecutionSession,
)
from jiuwenswarm.runtime.harness.tool_transport import ManagedProductToolTransport
from jiuwenswarm.server.runtime.agent_adapter.engine_adapter import EngineAgentAdapter


@pytest.mark.asyncio
@pytest.mark.parametrize("during_forced_wait", [False, True])
async def test_cancelled_stop_retains_active_server_until_confirmed_exit(monkeypatch, during_forced_wait):
    from jiuwenswarm.runtime.harness import tool_transport as module

    release, cancel_received = asyncio.Event(), asyncio.Event()

    async def owned_server():
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancel_received.set()
            await release.wait()

    task = asyncio.create_task(owned_server())
    transport = ManagedProductToolTransport(None, host_session_id="synthetic")
    transport._serve_task = task
    server = SimpleNamespace(should_exit=False)
    listener = SimpleNamespace(close=lambda: None)
    transport._uvicorn, transport._socket, transport._port = server, listener, 19001
    transport._accepting_preflight = True
    if during_forced_wait:
        monkeypatch.setattr(module, "_STOP_TIMEOUT_S", .01)
    stopping = asyncio.create_task(transport.stop())
    try:
        if during_forced_wait:
            await asyncio.wait_for(cancel_received.wait(), 1)
        else:
            while not server.should_exit:
                await asyncio.sleep(0)
        stopping.cancel()
        with pytest.raises(asyncio.CancelledError):
            await stopping
        assert transport._serve_task is task
        assert transport._socket is listener
        assert not task.done()
        assert not transport.exit_confirmed
        assert not transport._accepting_preflight
        release.set()
        await asyncio.wait_for(task, 1)
        await transport.stop()
        assert transport.exit_confirmed
        assert transport._port is None
    finally:
        release.set()
        if not stopping.done():
            stopping.cancel()
        if not task.done():
            task.cancel()
        await asyncio.gather(stopping, task, return_exceptions=True)


@pytest.mark.asyncio
async def test_execution_does_not_confirm_exit_when_transport_stop_returns_without_exit():
    async def stop():
        return None

    session = SimpleNamespace(
        io=SimpleNamespace(stop=stop), _tool_transport=SimpleNamespace(stop=stop, exit_confirmed=False),
        _tool_gateway=None, _exit_state=ExecutionExitState.RUNNING,
    )
    with pytest.raises(ExecutionExitUnconfirmedError):
        await ExecutionSession._stop_owned_resources(session, router=None)
    assert session._exit_state is ExecutionExitState.EXIT_UNCONFIRMED


@pytest.mark.parametrize("subject,allowed", [("alice", True), ("bob", False)])
def test_external_consumer_ownership_requires_exact_trusted_binding_subject(subject, allowed):
    binding = SimpleNamespace(
        host_session_id="same-session", provider_id="opencode", workspace="/tmp", subject_id="alice",
    )
    owner = SimpleNamespace(
        _resource_governed=True,
        _session=SimpleNamespace(binding=binding, owns_governed_provider_session=lambda sid: sid == "native-owned"),
    )
    execution = ResourceExecutionContext(
        "project", TrustedIdentity(subject, subject, "fixture"), "same-session", "/tmp", "opencode",
    )
    assert EngineAgentAdapter.owns_external_tool_session(owner, execution, "native-owned") is allowed


@pytest.mark.asyncio
@pytest.mark.parametrize('streaming', [False, True])
async def test_real_loopback_stop_closes_owned_model_before_server_drain(tmp_path, monkeypatch, streaming):
    """Real Uvicorn/HTTPX TCP, controlled upstream; no external Provider/model."""
    import httpx
    from jiuwenswarm.governance.credential_resources import BoundCredentialAuthority, CredentialUse
    from jiuwenswarm.governance.model_credentials import ModelCredentialBinding
    from jiuwenswarm.governance.opencode_model_http import OpenCodeModelOperationAuthority
    from jiuwenswarm.governance.resources import ResourceDefinition
    from jiuwenswarm.server.runtime.session import project_store
    from jiuwenswarm.server.runtime.session.project_access import ProjectAccessStore

    monkeypatch.setattr(project_store, 'get_agent_root_dir', lambda: tmp_path)
    project_store.invalidate_cache()
    project = project_store.create_project('transport close fixture', str(tmp_path))
    store = ProjectAccessStore()
    store.initialize(project.project_id, 'owner')
    identity = TrustedIdentity('owner', 'owner', 'fixture-auth')
    received, release, first_chunk = asyncio.Event(), asyncio.Event(), asyncio.Event()
    upstream_tasks = set()
    requests = []

    async def upstream(reader, writer):
        task = asyncio.current_task()
        upstream_tasks.add(task)
        try:
            header = await reader.readuntil(b'\r\n\r\n')
            length = next(int(line.split(b':', 1)[1]) for line in header.split(b'\r\n')
                          if line.lower().startswith(b'content-length:'))
            await reader.readexactly(length)
            assert b'Authorization: Bearer synthetic-owned-key' in header
            requests.append(True)
            if streaming:
                writer.write(b'HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nTransfer-Encoding: chunked\r\n\r\n9\r\ndata: x\n\n\r\n')
                await writer.drain()
            received.set()
            # Deliberately no model response until cleanup has been tested.
            await release.wait()
        finally:
            writer.close()
            await writer.wait_closed()
            upstream_tasks.discard(task)

    upstream_server = await asyncio.start_server(upstream, '127.0.0.1', 0)
    port = upstream_server.sockets[0].getsockname()[1]
    binding = ModelCredentialBinding('fixture', f'http://127.0.0.1:{port}/v1')
    store.register_resource(project.project_id, ResourceDefinition('credential', 'credential', binding.reference),
                            owner_subject_id='owner', actions=('use',), expected_revision=0)
    use = CredentialUse('credential', binding.reference, 'model', binding.destination)
    authority = BoundCredentialAuthority(
        ResourceExecutionContext(project.project_id, identity, 'parent', str(tmp_path), 'opencode'),
        uses=(use,), authorizer=store,
        resolver=SimpleNamespace(resolve_credential=lambda _: 'synthetic-owned-key'),
        current_identity=lambda: identity, is_current_execution=lambda: True,
    )
    record = OpenCodeModelOperationAuthority(authority, use, lambda: True)
    source = SimpleNamespace(turn_id='turn-one', model='fixture')
    async def capture(*args, **kwargs):
        return source

    transport = ManagedProductToolTransport(None, host_session_id='parent')
    other = ManagedProductToolTransport(None, host_session_id='other')
    request_task = None
    try:
        await transport.start()
        await other.start()
        transport.bind_native_preflight(lambda _: None)
        transport.bind_model_gateway(binding,
            execution_binding=SimpleNamespace(provider_id='opencode', host_session_id='parent',
                                               subject_id='owner', workspace=str(tmp_path)),
            capture_source=capture, is_source_current=lambda item: item is source,
            authority_for_turn=lambda turn: record if turn == 'turn-one' else None)
        consumer, server_task = transport._model_consumer, transport._serve_task
        headers = {
            'Authorization': 'Bearer ' + transport._token,
            'x-openjiuwen-session': 'native-fixture', 'x-openjiuwen-root': 'root',
            'x-openjiuwen-generation': transport._preflight_generation,
            'x-openjiuwen-agent': 'build', 'x-openjiuwen-model': 'fixture',
            'x-openjiuwen-provider': 'openjiuwen',
        }
        async with httpx.AsyncClient(trust_env=False, timeout=5) as client:
            async def consume():
                async with client.stream('POST',
                    f'http://127.0.0.1:{transport._port}/model/v1/chat/completions', headers=headers,
                    json={'model': 'fixture', 'messages': [], 'stream': streaming}) as response:
                    async for chunk in response.aiter_bytes():
                        if streaming:
                            assert chunk == b'data: x\n\n'
                            first_chunk.set()
            request_task = asyncio.create_task(consume())
            await asyncio.wait_for(received.wait(), 2)
            if streaming:
                await asyncio.wait_for(first_chunk.wait(), 2)
            assert not request_task.done()
            assert requests == [True] and not consumer.closed
            # Less than existing inner/outer 10s budgets: shutdown must actively
            # close its owned model HTTP, not wait for upstream/CLI completion.
            await asyncio.wait_for(transport.stop(), 2)
            assert consumer.closed and server_task.done() and transport.exit_confirmed
            assert transport._uvicorn is None and transport._socket is None
            assert other.started and not other.exit_confirmed
            assert not release.is_set()
            await asyncio.wait_for(asyncio.gather(request_task, return_exceptions=True), 2)
            await transport.stop()  # Idempotent confirmed cleanup.
    finally:
        release.set()
        upstream_server.close()
        await upstream_server.wait_closed()
        if request_task is not None:
            await asyncio.gather(request_task, return_exceptions=True)
        await transport.stop()
        await other.stop()
        if upstream_tasks:
            await asyncio.gather(*tuple(upstream_tasks), return_exceptions=True)
        project_store.invalidate_cache()
