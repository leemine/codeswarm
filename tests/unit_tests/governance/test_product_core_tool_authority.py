"""Product gateway + actual core final Tool boundary; no listener or Provider."""
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace
import asyncio

import pytest

from openjiuwen.core.foundation.tool import Tool, ToolCard, current_tool_invocation
from openjiuwen.core.runner import Runner
from openjiuwen.core.runner.callback.events import ToolCallEvents
from openjiuwen.harness.execution_subject import current_execution_subject, ExecutionSubject, execution_subject_scope
from openjiuwen.harness_protocol import BeforeToolContext, ToolInvocation

from jiuwenswarm.governance.contracts import TrustedIdentity
from jiuwenswarm.governance.opencode_tool_resources import OpenCodeToolResourceResolver
from jiuwenswarm.governance.product_executor import (
    ProductExecutorProof, product_executor_scope, require_product_executor,
)
from jiuwenswarm.governance.resources import ResourceDecision, ResourceRequest
from jiuwenswarm.governance.tool_resources import BoundToolResourceAuthority, ResourceExecutionContext, ToolResourceUse
from jiuwenswarm.runtime.harness.tool_gateway import ProductToolGateway, ProductToolScope


@pytest.fixture
def host(tmp_path):
    class Probe(Tool):
        async def invoke(self, inputs, **kwargs):
            effects.append((dict(inputs), current_execution_subject(), kwargs))
            return 'ok'

        async def stream(self, inputs, **kwargs):
            raise AssertionError('no stream fallback')
            yield inputs

    owner, session = object(), object()
    effects, seen, resource_checks = [], [], []
    state = SimpleNamespace(live=True, allowed=True)
    tool = Probe(ToolCard(id='product-core-probe', name='probe', description='fixture', parallel_safe=False))
    gateway = ProductToolGateway([tool], scope=ProductToolScope('owner', 'host-session', str(tmp_path)),
                                 invoke_kwargs={'session': session})
    gateway.bind_required_authority(owner)
    original = BeforeToolContext('agent', 'native-session', 'source-turn', 'source-call',
                                 'jiuwenswarm_product_tools_probe', {'path': str(tmp_path / 'original')})
    invocation = ToolInvocation(original.call_id, 'probe', original.arguments)
    identity = TrustedIdentity('owner', 'owner', 'synthetic-host')
    execution = ResourceExecutionContext('project', identity, 'host-session', str(tmp_path), 'opencode')

    class Resolver:
        def resources_for_tool(self, actual_execution, operation):
            assert actual_execution is execution
            proof = require_product_executor(operation)
            final = current_tool_invocation()
            assert final.source_operation is original and proof.operation is original
            assert proof.executor is tool and final.executor is tool
            assert operation is final.operation and final.is_current()
            assert current_execution_subject().session_id == 'host-session'
            with pytest.raises(ValueError):
                require_product_executor(replace(operation))
            with pytest.raises(ValueError):
                require_product_executor(original)
            seen.append(operation)
            return (ToolResourceUse(ResourceRequest('workspace', 'read', operation.arguments['path']), str(tmp_path)),)

    def authorize_resource(pid, actor, request):
        resource_checks.append(request)
        allowed = state.allowed and request.path == str(tmp_path / 'final')
        return ResourceDecision(allowed, pid, actor.actor_id, actor.subject_id, request, 1, 1,
                                reference=str(tmp_path), scope=str(tmp_path))

    authority = BoundToolResourceAuthority(execution, authorizer=SimpleNamespace(authorize_resource=authorize_resource),
        resolver=Resolver(), current_identity=lambda: identity, is_current_execution=lambda: state.live)

    async def run(callback=authority, *, after_proof=None):
        proof = ProductExecutorProof(owner, gateway, tool, invocation, original, lambda: state.live, callback)
        if after_proof:
            after_proof(proof)
        with product_executor_scope(proof):
            result = await gateway.invoke(invocation)
        assert not proof.active and proof.task is None
        return result

    return SimpleNamespace(**locals())


@asynccontextmanager
async def transformed(host, value='final'):
    async def transform(*args, **kwargs):
        return (), {**kwargs, 'inputs': {'path': str(host.tmp_path / value)}}
    await Runner.callback_framework.register(ToolCallEvents.TOOL_INVOKE_INPUT, transform, callback_type='transform')
    try:
        yield
    finally:
        await Runner.callback_framework.unregister(ToolCallEvents.TOOL_INVOKE_INPUT, transform)


@pytest.mark.asyncio
async def test_core_product_authorizes_actual_final_args_with_original_ticket_identity(host):
    async with transformed(host):
        result = await host.run()
    assert not result.is_error and result.content == 'ok'
    assert host.effects[0][0] == {'path': str(host.tmp_path / 'final')}
    assert host.effects[0][1].subject_id == 'owner'
    assert host.effects[0][2]['session'] is host.session
    assert len(host.resource_checks) == 1  # No original-argument resource grant is substituted.
    assert len(host.seen) == 2  # Existing resolver recheck follows ResourceGuard.
    assert all(op.turn_id == 'source-turn' and op.call_id == 'source-call' for op in host.seen)
    assert host.original.arguments['path'] == str(host.tmp_path / 'original')


@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['final_path', 'revoked', 'unknown_default_mapper', 'empty_mapping'])
async def test_core_product_resources_are_not_opened_by_ticket_or_tool_name(host, case):
    if case == 'revoked':
        host.state.allowed = False
    callback = host.authority
    if case in ('unknown_default_mapper', 'empty_mapping'):
        resolver = (OpenCodeToolResourceResolver({'resources': []}, owns_session=lambda *_: True)
                    if case == 'unknown_default_mapper' else SimpleNamespace(resources_for_tool=lambda *_: ()))
        callback = BoundToolResourceAuthority(host.execution,
            authorizer=SimpleNamespace(authorize_resource=host.authorize_resource), resolver=resolver,
            current_identity=lambda: host.identity, is_current_execution=lambda: host.state.live)
    async with transformed(host, 'outside' if case == 'final_path' else 'final'):
        assert (await host.run(callback)).is_error
    assert not host.effects


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['source', 'method', 'kwargs', 'scope', 'args'])
async def test_core_product_rechecks_actual_dependencies_after_await(host, change):
    final_inputs = {'path': str(host.tmp_path / 'final')}

    async def transform(*args, **kwargs):
        return (), {**kwargs, 'inputs': final_inputs}

    async def authorize(operation):
        assert await host.authority(operation)
        await asyncio.sleep(0)
        if change == 'source':
            host.state.live = False
        elif change == 'method':
            host.tool.invoke = lambda *_args, **_kwargs: None
        elif change == 'kwargs':
            host.gateway._invoke_kwargs['session'] = object()
        elif change == 'scope':
            host.gateway._scope = replace(host.gateway.scope, subject_id='other')
        else:
            final_inputs['path'] = str(host.tmp_path / 'changed')
        return True

    await Runner.callback_framework.register(ToolCallEvents.TOOL_INVOKE_INPUT, transform, callback_type='transform')
    try:
        assert (await host.run(authorize)).is_error
    finally:
        await Runner.callback_framework.unregister(ToolCallEvents.TOOL_INVOKE_INPUT, transform)
    assert not host.effects


@pytest.mark.asyncio
async def test_core_product_authorizes_after_waiting_for_existing_gateway_lock(host):
    await host.gateway._unsafe_lock.acquire()
    entered = asyncio.Event()

    async def admit(*_):
        entered.set()
        return True

    host.gateway._admit = admit
    async with transformed(host):
        pending = asyncio.create_task(host.run())
        await entered.wait()
        assert not host.resource_checks
        host.state.allowed = False
        host.gateway._unsafe_lock.release()
        assert (await pending).is_error
    assert not host.effects and len(host.resource_checks) == 1


@pytest.mark.asyncio
async def test_core_product_subject_change_in_callback_is_denied(host):
    contexts = []

    async def started(**_kwargs):
        scope = execution_subject_scope(ExecutionSubject('child', 'child', 'subagent', session_id='child-session'))
        scope.__enter__()
        contexts.append(scope)

    await Runner.callback_framework.register(ToolCallEvents.TOOL_CALL_STARTED, started)
    try:
        async with transformed(host):
            assert (await host.run()).is_error
    finally:
        # Restore the callback's temporary subject in its original task.
        for scope in reversed(contexts):
            scope.__exit__(None, None, None)
        await Runner.callback_framework.unregister(ToolCallEvents.TOOL_CALL_STARTED, started)
    assert not host.effects and not host.resource_checks


@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['missing_api', 'unknown_wrapper'])
async def test_core_product_without_provable_final_entry_denies(host, monkeypatch, case):
    import openjiuwen.core.foundation.tool as core_tools
    if case == 'missing_api':
        monkeypatch.delattr(core_tools, 'invoke_tool_with_authority')
    else:
        async def replaced(*_args, **_kwargs):
            raise AssertionError('unregistered wrapper must not execute')
        host.tool.invoke = replaced
    assert (await host.run()).is_error
    assert not host.effects and not host.seen


@pytest.mark.asyncio
async def test_core_product_cancel_and_reentry_preserve_single_use(host):
    entered = asyncio.Event()
    saved = []

    async def authorize(operation):
        proof = require_product_executor(operation)
        saved.append(proof)
        assert (await host.gateway.invoke(host.invocation)).is_error
        assert (await asyncio.create_task(host.gateway.invoke(host.invocation))).is_error
        entered.set()
        await asyncio.Event().wait()

    async with transformed(host):
        pending = asyncio.create_task(host.run(authorize))
        await entered.wait()
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
    assert not host.effects and not saved[0].active and saved[0].task is None


@pytest.mark.asyncio
async def test_real_ticket_consumer_preserves_source_into_core_final_gateway(host):
    """Actual core gate + Session consumer + final Tool; synthetic persistence only."""
    from openjiuwen.harness_protocol import HarnessContext, HostCapability, McpServerConfig, McpTransport
    from openjiuwen.harness_protocol import ToolApprovalDecision, ToolApprovalResponse
    from openjiuwen.harness_providers.opencode import (
        OpenCodeHarness, OpenCodeHarnessConfig, OpenCodeModelConfig, OpenCodePreflightEndpoint, PRODUCT_TICKET_FIELD,
    )
    from jiuwenswarm.runtime.harness.execution_session import ExecutionSession

    harness = OpenCodeHarness(OpenCodeHarnessConfig(model=OpenCodeModelConfig('fixture', 'http://127.0.0.1:1')))
    endpoint = OpenCodePreflightEndpoint('http://127.0.0.1:12345/private', 'synthetic-token-' + 'a'*32,
                                        'generation', product_tool_names=('probe',))
    harness.bind_preflight_endpoint(endpoint)
    seen = []

    async def authority(operation):
        proof = require_product_executor(operation)
        assert current_tool_invocation().source_operation is proof.operation
        assert proof.operation is harness._preflight.records['source-call'].request
        assert operation.turn_id == 'source-turn' and operation.call_id == 'source-call'
        seen.append(operation)
        return operation.arguments['path'] == str(host.tmp_path / 'final')

    context = HarnessContext('agent', 'agent', 'host-session', '', cwd=str(host.tmp_path),
        host_capabilities=frozenset({HostCapability.TOOL_APPROVAL, HostCapability.MCP_SERVERS}),
        tool_authorizer=authority, interactions=SimpleNamespace(),
        mcp_servers=(McpServerConfig('jiuwenswarm_product_tools', McpTransport.HTTP,
            url='http://127.0.0.1:12345/mcp', headers={'Authorization': 'Bearer '+endpoint.token}),))
    harness._context = context
    harness._session_id = 'native-session'
    harness._active_turn = SimpleNamespace(turn_id='source-turn', abort_requested=False, stop_requested=False)
    args = dict(host.original.arguments)
    body = {'version': 1, 'generation': 'generation', 'nonce': 'a'*32, 'session_id': 'native-session',
            'call_id': 'source-call', 'tool': host.original.tool_name, 'args': args}

    async def native_request(*_):
        return [
            {'info': {'id':'root', 'role':'user', 'sessionID':'native-session'}, 'parts':[]},
            {'info': {'id':'assistant', 'role':'assistant', 'sessionID':'native-session', 'parentID':'root'},
             'parts': [{'type':'tool', 'tool':host.original.tool_name, 'callID':'source-call',
                        'messageID':'assistant', 'sessionID':'native-session',
                        'state': {'status':'running', 'input':args}}]},
        ]

    async def approve(request):
        return ToolApprovalResponse(request.request_id, ToolApprovalDecision.ALLOW)

    async def reply(*values):
        assert values[-1] == {'response':'once'}

    harness._transport = SimpleNamespace(request=native_request)
    harness._await_host_interaction = approve
    harness._reply_native = reply
    harness._preflight.begin(harness.active_turn, 'root')
    answer = await harness.authorize_preflight(body)
    assert answer['allowed'] is True
    await harness._route_permission(harness.active_turn, SimpleNamespace(mark_denied=lambda _: None),
        'permission', 'source-call', {'sessionID':'native-session', 'permission':host.original.tool_name,
                                     'tool':{'messageID':'assistant'}, 'metadata':{}})
    owner = SimpleNamespace(engine=SimpleNamespace(harness=harness), _tool_gateway=host.gateway,
        owns_governed_provider_session=lambda sid: sid == 'native-session')
    host.gateway._required_authority_owner = owner
    consumer = ExecutionSession._product_consumer(owner, harness, host.gateway, context, await host.gateway.definitions())
    wire = {**args, PRODUCT_TICKET_FIELD: answer['ticket']}
    async with transformed(host):
        assert not (await consumer('probe', wire)).is_error
        assert (await consumer('probe', wire)).is_error
    assert len(seen) == len(host.effects) == 1
    assert PRODUCT_TICKET_FIELD not in host.effects[0][0]
