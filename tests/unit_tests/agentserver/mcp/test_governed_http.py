"""Real installed MCP SDK through a private HTTPX MockTransport; no network."""
import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from mcp import types

from jiuwenswarm.governance.contracts import TrustedIdentity
from jiuwenswarm.governance.credential_resources import BoundCredentialAuthority, CredentialUse
from jiuwenswarm.governance.resources import ResourceDecision
from jiuwenswarm.governance.tool_resources import ResourceExecutionContext
from jiuwenswarm.server.runtime.mcp import governed_http as consumer


@pytest.fixture
def host(monkeypatch):
    state = SimpleNamespace(allowed=True, current=True, revision=1, sent=[], resolved=0,
                            closed=0, targets=[], alter=None, resolve=None, streams=[],
                            secret='synthetic-only-bearer')
    identity = TrustedIdentity('bob', 'bob', 'organization')
    use = CredentialUse('bob-mcp', 'mcp:bob', 'mcp', 'https://mcp.invalid/rpc')

    def authorize(project, actor, request):
        return ResourceDecision(state.allowed, project, actor.actor_id, actor.subject_id,
                                request, 1, state.revision, reference=use.reference)

    async def resolve(_):
        state.resolved += 1
        if state.resolve:
            await state.resolve()
        return state.secret

    authority = BoundCredentialAuthority(
        ResourceExecutionContext('p', identity, 'bob-session', '/workspace', 'native'),
        uses=(use,), authorizer=SimpleNamespace(authorize_resource=authorize),
        resolver=SimpleNamespace(resolve_credential=resolve), current_identity=lambda: identity,
        is_current_execution=lambda: state.current,
    )

    async def handler(request):
        message = json.loads(request.content)
        state.sent.append((message, dict(request.headers)))
        assert request.headers['authorization'] == 'Bearer ' + state.secret
        method = message['method']
        if state.alter:
            response = await state.alter(request, message)
            if response:
                return response
        if method == 'notifications/initialized':
            return httpx.Response(202)
        if method == 'initialize':
            result = {'protocolVersion': types.LATEST_PROTOCOL_VERSION, 'capabilities': {},
                      'serverInfo': {'name': 'test-only', 'version': '1'}}
        elif method == 'tools/list':
            result = {'tools': [{'name': 'echo', 'inputSchema': {'type': 'object'}}]}
        else:
            result = {'content': [{'type': 'text', 'text': 'ordinary-success'}], 'isError': False}
        return httpx.Response(200, json={'jsonrpc': '2.0', 'id': message['id'], 'result': result})

    class Transport(httpx.MockTransport):
        async def aclose(self):
            state.closed += 1
            await super().aclose()

    def build(**kwargs):
        assert kwargs == {'retries': 0, 'trust_env': False}
        return Transport(handler)
    monkeypatch.setattr(consumer.httpx, 'AsyncHTTPTransport', build)

    def admit(target):
        state.targets.append(target)
        return state.allowed

    state.binding = consumer.McpHttpBinding('host-connection', use.destination, 'catalog-1', use)
    state.kwargs = dict(tool_name='echo', arguments={'text': 'hello'}, credential_authority=authority,
                        admit_actual_request=admit, is_current=lambda: state.current, timeout=1)
    return state


@pytest.mark.asyncio
async def test_real_sdk_each_post_authorized_and_private_client_closed(host):
    result = await consumer.invoke_mcp_tool(host.binding, **host.kwargs)
    assert result.result.content[0].text == 'ordinary-success'
    assert [m['method'] for m, _ in host.sent] == [
        'initialize', 'notifications/initialized', 'tools/call', 'tools/list']
    assert host.resolved == result.receipt.request_count == 4
    assert result.receipt.closed and host.closed == 1
    assert host.secret not in repr(result)
    assert 'authorization' not in repr(result.receipt).lower()
    assert all(t.arguments_json == '{"text":"hello"}' for t in host.targets)


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['deny', 'revision', 'generation'])
async def test_changed_authority_during_resolution_never_sends(host, change):
    async def alter():
        if change == 'deny':
            host.allowed = False
        elif change == 'revision':
            host.revision += 1
        else:
            host.current = False
    host.resolve = alter
    with pytest.raises(consumer.McpConsumptionDenied):
        await consumer.invoke_mcp_tool(host.binding, **host.kwargs)
    assert host.sent == []
    assert host.closed == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('shape', ['redirect', 'sse', 'session', 'oversize', 'wrong_status'])
async def test_unsupported_response_fails_closed_without_followup(host, shape):
    async def alter(request, message):
        if shape == 'redirect':
            return httpx.Response(307, headers={'location': 'https://other.invalid'})
        if shape == 'sse':
            return httpx.Response(200, headers={'content-type': 'text/event-stream'}, content=b'data: x')
        if shape == 'session':
            return httpx.Response(200, headers={'mcp-session-id': 'unowned'}, json={})
        if shape == 'wrong_status':
            return httpx.Response(202)
        return httpx.Response(200, json={'oversize': 'x' * 1024})
    host.alter = alter
    if shape == 'oversize':
        host.kwargs['max_response_bytes'] = 128
    with pytest.raises(consumer.McpConsumptionDenied):
        await consumer.invoke_mcp_tool(host.binding, **host.kwargs)
    assert len(host.sent) == 1
    assert host.closed == 1


@pytest.mark.asyncio
async def test_request_snapshot_ignores_later_mutation_of_callers_dict(host):
    async def alter():
        host.kwargs['arguments']['text'] = 'changed-after-entry'
    host.resolve = alter
    await consumer.invoke_mcp_tool(host.binding, **host.kwargs)
    call = next(m for m, _ in host.sent if m['method'] == 'tools/call')
    assert call['params']['arguments'] == {'text': 'hello'}


@pytest.mark.asyncio
async def test_revoke_during_actual_body_read_blocks_delivery(host):
    class RevokingStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'{"jsonrpc":"2.0","id":0,"result":{}}'
            host.allowed = False
        async def aclose(self):
            host.streams.append('closed')
    async def alter(request, message):
        return httpx.Response(200, headers={'content-type': 'application/json'}, stream=RevokingStream())
    host.alter = alter
    with pytest.raises(consumer.McpConsumptionDenied):
        await consumer.invoke_mcp_tool(host.binding, **host.kwargs)
    assert host.streams and host.closed == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['resolver', 'transport', 'cancel'])
async def test_error_and_cancellation_have_no_sensitive_exception_context(host, kind, caplog):
    async def fail():
        if kind == 'cancel':
            raise asyncio.CancelledError('synthetic-sensitive-cancel')
        raise RuntimeError('synthetic-sensitive-diagnostic')
    if kind == 'transport':
        async def alter(request, message):
            await fail()
        host.alter = alter
    else:
        host.resolve = fail
    expected = asyncio.CancelledError if kind == 'cancel' else consumer.McpConsumptionDenied
    with pytest.raises(expected) as error:
        await consumer.invoke_mcp_tool(host.binding, **host.kwargs)
    assert 'synthetic-sensitive' not in str(error.value)
    assert error.value.__context__ is None
    assert error.value.__cause__ is None
    assert host.closed == 1
    assert len(host.sent) <= 1  # No reconnect/replay.
    assert "synthetic-sensitive" not in caplog.text


@pytest.mark.asyncio
async def test_result_revoke_on_last_sdk_request_cannot_return_receipt(host):
    async def alter(request, message):
        if message['method'] == 'tools/list':
            host.revision += 1
    host.alter = alter
    with pytest.raises(consumer.McpConsumptionDenied):
        await consumer.invoke_mcp_tool(host.binding, **host.kwargs)
    assert host.closed == 1


@pytest.mark.asyncio
async def test_two_operations_do_not_share_client_or_cached_credential(host):
    await consumer.invoke_mcp_tool(host.binding, **host.kwargs)
    host.secret = 'synthetic-second-credential'
    await consumer.invoke_mcp_tool(host.binding, **host.kwargs)
    assert host.closed == 2 and host.resolved == 8
    assert {headers['authorization'] for _, headers in host.sent[:4]} == {'Bearer synthetic-only-bearer'}
    assert {headers['authorization'] for _, headers in host.sent[4:]} == {'Bearer synthetic-second-credential'}


@pytest.mark.asyncio
@pytest.mark.parametrize('value', [None, {1: 'bad'}, {'bad': float('nan')}, {'bad': ('tuple',)}])
async def test_unknown_input_shape_is_rejected_before_network(host, value):
    host.kwargs['arguments'] = value
    with pytest.raises(consumer.McpConsumptionDenied):
        await consumer.invoke_mcp_tool(host.binding, **host.kwargs)
    assert not host.sent


@pytest.mark.asyncio
async def test_unknown_tool_grant_denies_before_credential_resolution(host):
    host.kwargs['admit_actual_request'] = lambda _: False
    with pytest.raises(consumer.McpConsumptionDenied):
        await consumer.invoke_mcp_tool(host.binding, **host.kwargs)
    assert not host.sent and not host.resolved and host.closed == 1


@pytest.mark.asyncio
async def test_late_first_call_cannot_borrow_new_execution(host):
    pending = consumer.invoke_mcp_tool(host.binding, **host.kwargs)
    host.current = False
    with pytest.raises(consumer.McpConsumptionDenied):
        await pending
    assert not host.sent and not host.resolved


@pytest.mark.asyncio
async def test_actual_request_change_during_credential_wait_is_rejected(host, monkeypatch):
    original = consumer._OperationTransport.target
    held = []
    def target(self, request):
        held.append(request)
        return original(self, request)
    monkeypatch.setattr(consumer._OperationTransport, 'target', target)
    async def alter():
        held[0].url = httpx.URL('https://another.invalid/rpc')
    host.resolve = alter
    with pytest.raises(consumer.McpConsumptionDenied):
        await consumer.invoke_mcp_tool(host.binding, **host.kwargs)
    assert not host.sent and host.closed == 1


@pytest.mark.asyncio
async def test_client_request_auth_removed_before_error_escapes(host):
    held = []
    async def alter(request, message):
        held.append(request)
        raise RuntimeError('synthetic-private-transport-error')
    host.alter = alter
    with pytest.raises(consumer.McpConsumptionDenied):
        await consumer.invoke_mcp_tool(host.binding, **host.kwargs)
    assert held and 'authorization' not in held[0].headers


@pytest.mark.asyncio
async def test_tool_revocation_during_client_cleanup_blocks_result(host, monkeypatch):
    original = consumer._OperationTransport.aclose
    async def close(self):
        await original(self)
        host.allowed = False
    monkeypatch.setattr(consumer._OperationTransport, 'aclose', close)
    with pytest.raises(consumer.McpConsumptionDenied):
        await consumer.invoke_mcp_tool(host.binding, **host.kwargs)
    assert host.closed == 1


@pytest.mark.asyncio
async def test_caller_cancel_closes_actual_sdk_session_and_private_transport(host):
    started = asyncio.Event()
    async def pause():
        started.set()
        await asyncio.Event().wait()
    host.resolve = pause
    task = asyncio.create_task(consumer.invoke_mcp_tool(host.binding, **host.kwargs))
    await asyncio.wait_for(started.wait(), 1)
    task.cancel('synthetic-caller-cancel-detail')
    with pytest.raises(asyncio.CancelledError) as error:
        await task
    assert str(error.value) == '' and error.value.__context__ is None
    assert not host.sent and host.closed == 1


@pytest.mark.asyncio
async def test_failed_owned_cleanup_cannot_produce_success_receipt(host, monkeypatch):
    async def fail(self):
        self.active = False
        await self.inner.aclose()
        raise RuntimeError('synthetic-sensitive-cleanup')
    monkeypatch.setattr(consumer._OperationTransport, 'aclose', fail)
    with pytest.raises(consumer.McpConsumptionDenied) as error:
        await consumer.invoke_mcp_tool(host.binding, **host.kwargs)
    assert error.value.__context__ is None and 'synthetic-sensitive' not in str(error.value)


@pytest.mark.asyncio
async def test_unknown_rpc_fields_or_method_never_reach_sink(host, monkeypatch):
    original = consumer._OperationTransport.handle_async_request
    async def changed(self, request):
        message = json.loads(request.content)
        message['params']['unapproved'] = 'not-a-host-grant'
        other = httpx.Request('POST', request.url, json=message)
        return await original(self, other)
    monkeypatch.setattr(consumer._OperationTransport, 'handle_async_request', changed)
    with pytest.raises(consumer.McpConsumptionDenied):
        await consumer.invoke_mcp_tool(host.binding, **host.kwargs)
    assert not host.sent and not host.resolved


@pytest.mark.asyncio
async def test_final_json_types_cannot_be_substituted_by_python_equality(host, monkeypatch):
    host.kwargs['arguments'] = {'value': True}
    original = consumer._OperationTransport.handle_async_request
    async def changed(self, request):
        message = json.loads(request.content)
        if message['method'] == 'tools/call':
            message['params']['arguments']['value'] = 1
            request = httpx.Request('POST', request.url, json=message)
        return await original(self, request)
    monkeypatch.setattr(consumer._OperationTransport, 'handle_async_request', changed)
    with pytest.raises(consumer.McpConsumptionDenied):
        await consumer.invoke_mcp_tool(host.binding, **host.kwargs)
    assert all(message['method'] != 'tools/call' for message, _ in host.sent)
