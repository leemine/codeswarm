"""Real durable continuation and Coordinator claim; synthetic Native IO only."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.runtime.continuation_control import capture_continuation_control, continuation_control
from tests.unit_tests.runtime import test_continuation_execution as fixtures

setup, transaction = fixtures.setup, fixtures.transaction


async def arranged(tx):
    original, policy = await fixtures.capture(tx)
    context = await policy.make_context()
    sid = original.session_id
    await tx.runtime.observe_detached_native_turn(sid, tx.owned_execution.execution_id,
        {'event_type': 'chat.ask_user_question', 'request_id': 'question-1'})
    bound = SimpleNamespace(binding=SimpleNamespace(host_session_id=sid))
    turn = SimpleNamespace(turn_id='native-turn', abort_requested=False,
        content=SimpleNamespace(metadata={'native.host_request': 'original-token'}))
    from jiuwenswarm.runtime.harness.native_session import NativeExecutionSession, _HostRequest
    from openjiuwen.harness.schema.interaction import SendInputRequest
    native = object.__new__(NativeExecutionSession)
    native.engine = SimpleNamespace(binding=bound.binding)
    native._closing = native._closed = False
    native._native = SimpleNamespace(active_turn=turn)
    native._turn_requests = {turn.turn_id: 'original-token'}
    native._requests = {'original-token': _HostRequest(request=SendInputRequest(
        request_id=original.request_id, inputs={}))}
    child = SimpleNamespace(_native_execution=native, _register_session_agent_task=Mock())
    adapter = SimpleNamespace(_native_session_routes={sid: (None, None, bound, context)},
        _get_cached_session_adapter=Mock(return_value=child), _touch_session_adapter=Mock())
    facade = SimpleNamespace(_adapter=adapter)
    tx.manager.get_agent_for_session_nowait = Mock(return_value=facade)
    request = AgentRequest(request_id='new-answer-id', session_id=sid, channel_id='web',
        req_method=ReqMethod.CHAT_SEND, is_stream=True,
        params={'query': '', 'mode': 'agent.work.normal', 'request_id': 'question-1',
                'source': 'permission_interrupt', 'answers': [{'question': 'Continue?', 'selected_options': ['allow_once']}]})
    coordinator = tx.runtime._session_coordinator
    return SimpleNamespace(**locals())


@pytest.mark.asyncio
async def test_control_uses_original_context_and_existing_child_without_warmup(transaction):
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter
    a = await arranged(transaction)
    async def deliver():
        a.request._continuation_control = capture_continuation_control(a.tx.runtime, a.request, a.facade)
        a.adapter._get_or_create_session_adapter = AsyncMock(side_effect=AssertionError('new seed forbidden'))
        child = await JiuWenSwarmDeepAdapter._get_session_adapter_for_request(
            a.adapter, a.request, reserve_activity=True)
        assert child is a.child
        child._register_session_agent_task.assert_called_once_with(a.sid)
        assert continuation_control(a.request, child=child).child is child
        assert a.context.request_id == a.original.request_id != a.request.request_id
        a.policy.check()
        return []
    await a.coordinator.deliver_control(a.sid, 'question-1', deliver)
    with pytest.raises(Exception, match='control claim|required|parent'):
        continuation_control(a.request)


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['identity', 'revoke', 'parent', 'turn', 'child', 'route', 'facade', 'model', 'copy'])
async def test_late_or_changed_control_cannot_select_replacement(transaction, change):
    a = await arranged(transaction)
    async def deliver():
        a.request._continuation_control = capture_continuation_control(a.tx.runtime, a.request, a.facade)
        if change == 'identity':
            a.tx.setup.identities[0] = fixtures.ALICE
        elif change == 'revoke':
            a.tx.setup.host.store.revoke(a.tx.request.share_id, fixtures.ALICE, expected_revision=1)
        elif change == 'parent':
            a.coordinator._registry.get(a.tx.owned_execution.execution_id).cancellation_requested = True
        elif change == 'turn':
            a.native._native.active_turn = SimpleNamespace(turn_id='new', abort_requested=False)
        elif change == 'child':
            a.adapter._get_cached_session_adapter.return_value = object()
        elif change == 'route':
            a.adapter._native_session_routes[a.sid] = tuple([*a.adapter._native_session_routes[a.sid]])
        elif change == 'facade':
            a.tx.manager.get_agent_for_session_nowait.return_value = object()
        elif change == 'model':
            a.request.params['model_name'] = 'other#0'
        else:
            from copy import copy
            copied = copy(a.request)
            with pytest.raises(PermissionError):
                continuation_control(copied)
            return []
        with pytest.raises((PermissionError, RuntimeError)):
            continuation_control(a.request)
        return []
    try:
        await a.coordinator.deliver_control(a.sid, 'question-1', deliver)
    except RuntimeError:
        assert change == 'parent'


@pytest.mark.asyncio
async def test_other_task_cannot_borrow_claim(transaction):
    a = await arranged(transaction)
    async def deliver():
        async def stranger():
            with pytest.raises(RuntimeError, match='control claim'):
                capture_continuation_control(a.tx.runtime, a.request, a.facade)
        await asyncio.create_task(stranger())
        return []
    await a.coordinator.deliver_control(a.sid, 'question-1', deliver)


@pytest.mark.asyncio
async def test_legacy_control_does_not_gain_continuation_handle(transaction):
    a = await arranged(transaction)
    a.adapter._native_session_routes.clear()
    assert capture_continuation_control(a.tx.runtime, a.request, a.facade) is None
    a.request._continuation_control = {'request_id': a.original.request_id}
    with pytest.raises(PermissionError):
        continuation_control(a.request)


@pytest.mark.asyncio
async def test_real_facade_does_not_reconcile_new_control_id(transaction, monkeypatch):
    from jiuwenswarm.server.runtime.agent_adapter import interface, interface_deep
    a = await arranged(transaction)
    a.facade._ensure_adapter = Mock(return_value=a.adapter)
    a.facade._adapter_mode_for_request = Mock(return_value='normal')
    a.facade._select_execution_before_mcp = interface.JiuWenSwarm._select_execution_before_mcp
    a.facade._session_manager = SimpleNamespace(get_session_id=lambda sid: sid)
    a.facade._build_inputs = Mock(return_value=({}, '', False))
    a.facade.reconcile_session_mcp = AsyncMock(side_effect=AssertionError('control cannot reconcile'))
    monkeypatch.setattr(interface, 'restore_chat_send_equipment_params', Mock())
    delivered = []
    async def consume(request, inputs):
        selected = await interface_deep.JiuWenSwarmDeepAdapter._get_session_adapter_for_request(
            a.adapter, request, reserve_activity=True)
        assert selected is a.child
        delivered.append(request.request_id)
        if False:
            yield
    a.adapter.process_message_stream_impl = consume
    async def operation():
        a.request._continuation_control = capture_continuation_control(a.tx.runtime, a.request, a.facade)
        return [item async for item in interface.JiuWenSwarm.deliver_control_input(a.facade, a.request)]
    await a.coordinator.deliver_control(a.sid, 'question-1', operation)
    assert delivered == ['new-answer-id']
    a.facade.reconcile_session_mcp.assert_not_awaited()


@pytest.mark.asyncio
async def test_stream_claim_keeps_same_parent_capability(transaction):
    a = await arranged(transaction)
    async def operation():
        a.request._continuation_control = capture_continuation_control(a.tx.runtime, a.request, a.facade)
        assert continuation_control(a.request).child is a.child
        yield 'resumed'
    assert [item async for item in a.coordinator.deliver_control_stream(
        a.sid, 'question-1', operation)] == ['resumed']
