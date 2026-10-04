"""Actual Native Goal capability at the existing facade; synthetic model/IO."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.runtime.harness.native_goal_control import capture_native_goal_control
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter
from tests.unit_tests.governance._managed_native_fixture import ownership
from tests.unit_tests.runtime.harness import test_native_goal_source as source_fixtures

goal_case = source_fixtures.goal_case


@pytest.fixture
async def control_case(goal_case):
    c = goal_case
    entered, release = asyncio.Event(), asyncio.Event()
    async def gate(_ctx):
        entered.set()
        await release.wait()
    c.outer_before = gate
    child = object.__new__(JiuWenSwarmDeepAdapter)
    child._is_session_scoped_adapter = True
    child._parent_session_id = 'goal-session'
    child._instance = c.outer
    child._native_execution = c.native
    child._record_goal_set_history_if_needed = AsyncMock()
    owners = ownership(child, 'goal-session')
    facade = owners.facade
    facade._ensure_adapter = Mock(side_effect=AssertionError('control cannot allocate'))
    facade._select_execution_before_mcp = Mock(side_effect=AssertionError('control cannot select route'))
    running = asyncio.create_task(c.submit())
    await asyncio.wait_for(entered.wait(), 3)
    request = AgentRequest('goal-control', session_id='goal-session', channel_id='web',
                           req_method=ReqMethod.COMMAND_GOAL, params={'action': 'get'})
    cap = capture_native_goal_control(c.native, source=c.admission.bound[0].source,
        request_id=request.request_id, check_current=c.admission.check)
    request._native_goal_control = cap
    c.__dict__.update(child=child, facade=facade, owners=owners, control=request,
                      cap=cap, release=release, running=running)
    try:
        yield c
    finally:
        release.set()
        if not running.done():
            await asyncio.wait_for(running, 10)
        else:
            running.result()


async def _call(c, stream):
    if stream:
        chunks = [chunk async for chunk in c.facade.process_message_stream(c.control)]
        assert len(chunks) == 1 and chunks[0].is_complete
        return chunks[0].payload
    response = await c.facade.execute_message(c.control)
    return {**response.payload, '_ok': response.ok}


@pytest.mark.asyncio
@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.parametrize('action', ['get', 'pause', 'resume', 'set', 'clear'])
async def test_live_goal_control_uses_same_facade_and_returns_one_ack(control_case, stream, action):
    c = control_case
    c.control.params = {'action': action}
    if action == 'set':
        c.control.params.update(objective='replacement goal', overwrite_confirmed=True)
    before = c.native._native.active_turn
    result = await _call(c, stream)
    assert result.get('event_type') != 'chat.error' and result.get('_ok', True)
    assert result.get('action') == action
    assert result.get('event_type', 'goal.snapshot') == 'goal.snapshot'
    c.facade._ensure_adapter.assert_not_called()
    c.facade._select_execution_before_mcp.assert_not_called()
    assert len(c.admission.bound) == 1
    assert not c.tools and not c.http  # existing output reader still owns held attempt
    if action != 'clear':
        assert c.native._native.active_turn is before
    assert c.child._record_goal_set_history_if_needed.await_count == int(action == 'set')


@pytest.mark.asyncio
@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.parametrize('change', ['sid', 'rid', 'cap_type', 'child', 'native', 'missing_cap'])
async def test_control_request_cannot_select_a_new_owner(control_case, stream, change):
    c = control_case
    c.control.params = {'action': 'pause'}
    if change == 'sid':
        c.control.session_id = 'different'
    elif change == 'rid':
        c.control.request_id = 'different'
    elif change == 'cap_type':
        c.control._native_goal_control = SimpleNamespace(native=c.native)
    elif change == 'child':
        c.owners.root._session_adapters['goal-session'] = SimpleNamespace(_native_execution=c.native)
    elif change == 'native':
        c.child._native_execution = object()
    else:
        del c.control._native_goal_control
    result = await _call(c, stream)
    assert result.get('event_type') == 'chat.error' if stream else result['_ok'] is False
    c.facade._ensure_adapter.assert_not_called()
    c.facade._select_execution_before_mcp.assert_not_called()
    c.child._record_goal_set_history_if_needed.assert_not_awaited()
    assert c.outer.goal_manager.peek().status.value == 'active'


@pytest.mark.asyncio
@pytest.mark.parametrize('stream', [False, True])
async def test_active_set_confirmation_keeps_original_goal_and_reader(control_case, stream):
    c = control_case
    c.control.params = {'action': 'set', 'objective': 'replacement goal'}
    result = await _call(c, stream)
    if stream:
        assert result['event_type'] == 'goal.confirm_required'
    else:
        assert result['_ok'] is False and result['code'] == 'already_exists'
    assert result['existing_goal']['objective'] == 'goal objective'
    c.child._record_goal_set_history_if_needed.assert_not_awaited()
    assert len(c.admission.bound) == 1


def _initial_adapter(c):
    child = object.__new__(JiuWenSwarmDeepAdapter)
    child._is_session_scoped_adapter = True
    child._parent_session_id = 'goal-session'
    child._instance = c.outer
    child._native_execution = c.native
    return child


@pytest.mark.asyncio
async def test_actual_pending_initial_dispatcher_reaches_manager_and_consumers(goal_case):
    c = goal_case
    child = _initial_adapter(c)
    async def dispatch(**kwargs):
        return await child._dispatch_goal_control(session_id='goal-session', **kwargs)
    c.native._goal_dispatcher = dispatch
    await c.submit()
    assert c.error is None and len(c.http) == 2 and c.side_effects == ['goal']
    assert c.outer.goal_manager.peek().status.value == 'completed'


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['objective', 'budget_bool', 'sid', 'result_done', 'child_task'])
async def test_initial_dispatch_cannot_change_frozen_operation_or_task(goal_case, change):
    from jiuwenswarm.governance.tool_context import tool_authority_scope
    c = goal_case
    child = _initial_adapter(c)
    denied = []
    async def dispatch(**kwargs):
        kwargs['session_id'] = 'goal-session'
        if change == 'objective':
            kwargs['objective'] = 'different objective'
        elif change == 'budget_bool':
            kwargs['max_attempts'] = True
        elif change == 'sid':
            kwargs['session_id'] = 'different-session'
        elif change == 'result_done':
            c.admission.bound[0]._entry.result.set_result({'result_type': 'goal_control'})
        try:
            if change == 'child_task':
                return await asyncio.create_task(child._dispatch_goal_control(**kwargs))
            return await child._dispatch_goal_control(**kwargs)
        except PermissionError:
            denied.append(True)
            raise
    c.native._goal_dispatcher = dispatch
    with tool_authority_scope(None, provider_authorizers=c.bundle):
        _, result = await c.native.submit_goal('set', request=c.request,
                                               objective='goal objective', max_attempts=1)
    if change == 'result_done':
        await result
    else:
        with pytest.raises(asyncio.CancelledError):
            await result
    await asyncio.wait_for(c.admission.bound[0]._entry.terminal_event.wait(), 10)
    assert denied == [True] and not c.models and not c.tools
    assert c.outer.goal_manager.peek() is None


@pytest.mark.asyncio
@pytest.mark.parametrize('action', ['set', 'pause', 'resume', 'clear'])
async def test_direct_managed_adapter_mutation_cannot_bypass_facade_cap(control_case, action):
    c = control_case
    with pytest.raises(PermissionError):
        await c.child._dispatch_goal_control(action=action, session_id='goal-session', objective='other')
    assert c.outer.goal_manager.peek().objective == 'goal objective'
    assert len(c.admission.bound) == 1 and not c.tools


@pytest.mark.asyncio
@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.parametrize('change', ['native', 'params', 'identity'])
async def test_control_history_wait_cannot_publish_stale_result(control_case, stream, change):
    c = control_case
    c.control.params = {'action': 'set', 'objective': 'new goal', 'overwrite_confirmed': True}
    native, params = c.child._native_execution, dict(c.control.params)
    async def record(*_args, **_kwargs):
        await asyncio.sleep(0)
        if change == 'native':
            c.child._native_execution = object()
        elif change == 'params':
            c.control.params['objective'] = 'different late objective'
        else:
            c.admission.live = False
    c.child._record_goal_set_history_if_needed.side_effect = record
    try:
        result = await _call(c, stream)
        assert result.get('event_type') == 'chat.error' if stream else result['_ok'] is False
        assert 'goal' not in result and 'existing_goal' not in result
        c.child._record_goal_set_history_if_needed.assert_awaited_once()
        assert len(c.admission.bound) == 1
    finally:
        c.child._native_execution = native
        c.control.params = params
        c.admission.live = True


@pytest.mark.asyncio
@pytest.mark.parametrize('action', ['set', 'resume'])
async def test_active_stream_without_cap_cannot_attach_another_reader(control_case, action):
    c = control_case
    del c.control._native_goal_control
    c.control.params = {'action': action, 'objective': 'replacement goal'}
    result = await _call(c, True)
    assert result['event_type'] == 'chat.error'
    c.facade._ensure_adapter.assert_not_called()
    c.facade._select_execution_before_mcp.assert_not_called()
    assert len(c.admission.bound) == 1
    assert c.outer.goal_manager.peek().objective == 'goal objective'


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['source', 'snapshot_after_check'])
async def test_initial_dispatch_rechecks_original_source_and_operation(goal_case, change):
    from dataclasses import replace
    from jiuwenswarm.governance.tool_context import tool_authority_scope
    from jiuwenswarm.runtime.harness.native_session import _initial_goal_operation
    from openjiuwen.core.controller.schema.execution_origin import ExecutionOrigin
    c = goal_case
    child = _initial_adapter(c)
    denied = []
    async def dispatch(**kwargs):
        entry = c.admission.bound[0]._entry
        lifecycle = entry.lifecycle
        if change == 'source':
            entry.lifecycle = replace(lifecycle, source=ExecutionOrigin(object(), lambda: None))
        else:
            def change_snapshot():
                entry.goal_operation = _initial_goal_operation('set', session_id='goal-session',
                    objective='different', max_attempts=1)
            c.admission.check_hook = change_snapshot
        try:
            return await child._dispatch_goal_control(session_id='goal-session', **kwargs)
        except PermissionError:
            denied.append(True)
            raise
        finally:
            entry.lifecycle = lifecycle
            c.admission.check_hook = None
    c.native._goal_dispatcher = dispatch
    with tool_authority_scope(None, provider_authorizers=c.bundle):
        _, result = await c.native.submit_goal('set', request=c.request,
                                               objective='goal objective', max_attempts=1)
    with pytest.raises(asyncio.CancelledError):
        await result
    await asyncio.wait_for(c.admission.bound[0]._entry.terminal_event.wait(), 10)
    assert denied == [True] and not c.models and not c.tools
    assert c.outer.goal_manager.peek() is None


@pytest.mark.asyncio
async def test_unscoped_managed_goal_root_does_not_allocate_or_mutate(control_case):
    c = control_case
    root = object.__new__(JiuWenSwarmDeepAdapter)
    root._is_session_scoped_adapter = False
    root._session_adapters = {'goal-session': c.child}
    root._instance = None
    root._get_or_create_session_adapter = AsyncMock(side_effect=AssertionError('must not allocate'))
    with pytest.raises(PermissionError):
        await root._dispatch_goal_control(action='set', objective='new', session_id='goal-session')
    root._get_or_create_session_adapter.assert_not_called()
    assert c.outer.goal_manager.peek().objective == 'goal objective'


@pytest.mark.asyncio
async def test_initial_goal_copies_caller_arguments_before_lifecycle_callback(goal_case):
    from dataclasses import replace
    from jiuwenswarm.governance.tool_context import tool_authority_scope
    c = goal_case
    child = _initial_adapter(c)
    kwargs = {'objective': 'original objective', 'max_attempts': 1}
    actual = []
    async def dispatch(**operation):
        actual.append(dict(operation))
        return await child._dispatch_goal_control(session_id='goal-session', **operation)
    c.native._goal_dispatcher = dispatch
    factory = c.bundle.native_lifecycle_factory
    def capture(native, request):
        kwargs.update(objective='late caller mutation', max_attempts=9)
        return factory(native, request)
    bundle = replace(c.bundle, native_lifecycle_factory=capture)
    with tool_authority_scope(None, provider_authorizers=bundle):
        _, result = await c.native.submit_goal('set', request=c.request, **kwargs)
    await asyncio.wait_for(result, 3)
    await asyncio.wait_for(c.admission.bound[0]._entry.terminal_event.wait(), 4)
    assert actual == [{'action': 'set', 'objective': 'original objective', 'max_attempts': 1}]
    assert kwargs['objective'] == 'late caller mutation'
    assert c.outer.goal_manager.peek().objective == 'original objective'
    assert c.error is None and c.side_effects == ['goal']
