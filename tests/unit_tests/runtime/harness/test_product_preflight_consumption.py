"""Real MCP wire consumption with synthetic native persistence and approval."""
from dataclasses import replace
from types import SimpleNamespace, MethodType
from contextlib import asynccontextmanager
import asyncio

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from openjiuwen.harness_protocol import ToolApprovalDecision, ToolApprovalResponse
from openjiuwen.harness_providers.opencode import PRODUCT_TICKET_FIELD
from jiuwenswarm.runtime.harness.execution_session import ExecutionExitState
from jiuwenswarm.runtime.harness.tool_gateway import ProductToolGateway, ProductToolScope
from jiuwenswarm.governance.product_executor import current_product_executor
from test_opencode_preflight_binding import fixture_session


class Echo:
    def __init__(self):
        self.card = SimpleNamespace(name='echo', description='fixture', parallel_safe=False,
                                    input_params={'type':'object', 'properties':{'value':{'type':'string'}},
                                                  'required':['value'], 'additionalProperties':False})
        self.calls = []

    async def invoke(self, inputs, **kwargs):
        self.calls.append((inputs, kwargs))
        return SimpleNamespace(success=True, data=inputs['value'])

    def render_for_llm(self, output):
        return output.data


@asynccontextmanager
async def owned(tmp_path, *, mutation=None):
    tool = Echo()
    gateway = ProductToolGateway([tool], scope=ProductToolScope('owner', 'parent', str(tmp_path)))
    session, harness, context = fixture_session(tmp_path, gateway)
    state = SimpleNamespace(allowed=True, calls=[], messages=[], tool=tool, gateway=gateway)

    async def authority(operation):
        proof = current_product_executor()
        assert proof is not None and proof.operation is operation
        state.calls.append((operation.turn_id, operation.call_id))
        if mutation == 'method':
            async def replacement(self, *_a, **_kw):
                raise AssertionError('replacement must never execute')
            tool.invoke = MethodType(replacement, tool)
        if mutation == 'kwargs':
            gateway._invoke_kwargs['other-session'] = object()
        if mutation == 'secret_error':
            raise ValueError('synthetic-secret-must-not-log')
        return state.allowed

    async def native_request(method, path):
        return state.messages

    async def close():
        pass

    async def approval(request):
        return ToolApprovalResponse(request.request_id, ToolApprovalDecision.ALLOW)

    async def reply(*args):
        assert args[-1] == {'response':'once'}

    context = replace(context, tool_authorizer=authority)
    prepared = await session._prepare_tool_context(context)
    harness._context = prepared
    harness._session_id = 'native-owned'
    harness._active_turn = SimpleNamespace(turn_id='original-turn', abort_requested=False, stop_requested=False)
    harness._transport = SimpleNamespace(request=native_request, close=close)
    harness._await_host_interaction = approval
    harness._reply_native = reply
    harness._preflight.begin(harness.active_turn, 'root')
    session._started = True
    session._exit_state = ExecutionExitState.RUNNING
    endpoint = harness._preflight.endpoint

    async def ticket(call_id, args):
        body = {'version':1, 'generation':endpoint.generation, 'nonce': ('a' if call_id=='call1' else 'b')*32,
                'session_id':'native-owned', 'call_id':call_id, 'tool':'jiuwenswarm_product_tools_echo', 'args':args}
        state.messages = [
            {'info':{'id':'root', 'role':'user', 'sessionID':'native-owned'}, 'parts':[]},
            {'info':{'id':'assistant', 'role':'assistant', 'parentID':'root', 'sessionID':'native-owned'},
             'parts':[{'type':'tool', 'id':'part', 'messageID':'assistant', 'sessionID':'native-owned',
                       'callID':call_id, 'tool':body['tool'], 'state':{'status':'running','input':args}}]},
        ]
        async with httpx.AsyncClient() as client:
            response = await client.post(endpoint.url, json=body,
                                         headers={'Authorization':'Bearer '+endpoint.token})
        assert response.status_code == 200, response.text
        result = response.json()
        assert result['allowed'] is True and 'ticket' in result
        await harness._route_permission(harness.active_turn, SimpleNamespace(mark_denied=lambda _: None),
                                         'permission-'+call_id, call_id,
                                         {'sessionID':'native-owned', 'tool':{'messageID':'assistant'},
                                          'permission':body['tool'], 'metadata':{}})
        return {**args, PRODUCT_TICKET_FIELD:result['ticket']}

    state.ticket = ticket
    config = prepared.mcp_servers[0]
    try:
        async with httpx.AsyncClient(headers=dict(config.headers)) as client:
            async with streamable_http_client(config.url, http_client=client) as (read, write, _):
                async with ClientSession(read, write) as mcp:
                    await mcp.initialize()
                    state.mcp = mcp
                    yield state
    finally:
        harness._active_turn = None
        await session.stop()
        assert session._tool_transport.exit_confirmed


@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['allow', 'missing', 'revoked', 'schema', 'method', 'kwargs', 'secret_error'])
async def test_original_product_call_is_checked_after_ticket_stripping(tmp_path, case, caplog):
    async with owned(tmp_path, mutation=case) as state:
        args = {'value':123 if case=='schema' else 'ordinary'}
        wire = await state.ticket('call1', args)
        if case == 'missing':
            wire.pop(PRODUCT_TICKET_FIELD)
        if case == 'revoked':
            state.allowed = False
        result = await state.mcp.call_tool('echo', wire)
        assert result.isError is (case != 'allow')
        assert bool(state.tool.calls) is (case == 'allow')
        if case == 'allow':
            assert state.tool.calls == [({'value':'ordinary'}, {})]
            assert state.calls == [('original-turn','call1'), ('original-turn','call1')]
            replay = await state.mcp.call_tool('echo', wire)
            assert replay.isError and len(state.tool.calls) == 1
        assert 'synthetic-secret-must-not-log' not in caplog.text
        assert all(PRODUCT_TICKET_FIELD not in repr(call) for call in state.tool.calls)


@pytest.mark.asyncio
async def test_resource_revocation_while_waiting_for_gateway_lock_denies(tmp_path):
    async with owned(tmp_path) as state:
        wire = await state.ticket('call1', {'value':'queued'})
        await state.gateway._unsafe_lock.acquire()
        pending = asyncio.create_task(state.mcp.call_tool('echo', wire))
        try:
            async with asyncio.timeout(2):
                while not state.calls:
                    await asyncio.sleep(0)
            state.allowed = False
            state.gateway._unsafe_lock.release()
            result = await asyncio.wait_for(pending, 3)
            assert result.isError and not state.tool.calls
            assert state.calls == [('original-turn','call1'), ('original-turn','call1')]
        finally:
            if state.gateway._unsafe_lock.locked():
                state.gateway._unsafe_lock.release()
            if not pending.done():
                pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.asyncio
async def test_product_proof_cannot_cross_task_or_execute_twice(tmp_path):
    from openjiuwen.harness_protocol import BeforeToolContext, ToolInvocation
    from jiuwenswarm.governance.product_executor import ProductExecutorProof, product_executor_scope
    owner, tool = object(), Echo()
    gateway = ProductToolGateway([tool], scope=ProductToolScope('owner', 'parent', str(tmp_path)))
    gateway.bind_required_authority(owner)
    invocation = ToolInvocation('native-call', 'echo', {'value':'safe'})
    operation = BeforeToolContext('a','native','original','native-call','jiuwenswarm_product_tools_echo',invocation.arguments)
    async def allowed(_):
        return True
    proof = ProductExecutorProof(owner,gateway,tool,invocation,operation,lambda: True,allowed)
    with product_executor_scope(proof):
        assert (await asyncio.create_task(gateway.invoke(invocation))).is_error
        assert not (await gateway.invoke(invocation)).is_error
        assert (await gateway.invoke(invocation)).is_error
    assert len(tool.calls) == 1 and not proof.active and proof.task is None
