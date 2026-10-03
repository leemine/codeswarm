# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Team facade routing without a Native agent or Single execution owner."""
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponseChunk
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.runtime.agent_adapter.agent_adapters import create_adapter
from jiuwenswarm.server.runtime.agent_adapter.team_engine_adapter import ExternalTeamAgentAdapter
from tests.unit_tests.runtime.harness.test_team_execution import host as host


def request_for(host, *, mode=True):
    request = AgentRequest(request_id='request', channel_id='web', session_id='session', user_id='alice',
                           req_method=ReqMethod.CHAT_SEND,
                           params={'mode': 'team.code.normal'} if mode else {})
    request._execution_route = host.route
    request._bound_execution = host.route.bound
    return request


def test_factory_selects_team_without_native_or_single_construction(host, monkeypatch):
    monkeypatch.setattr('jiuwenswarm.server.runtime.agent_adapter.engine_adapter.EngineAgentAdapter',
                        Mock(side_effect=AssertionError('Single execution')))
    adapter = create_adapter(execution_route=host.route)
    assert isinstance(adapter, ExternalTeamAgentAdapter)
    assert host.engines == []
    manifest = adapter.ui_capability_manifest.record()
    assert manifest['provider_id'] == host.provider
    assert manifest['state'] in {'available', 'degraded'}
    assert any(entry['state'] == 'available' for entry in manifest['entries'])


@pytest.mark.parametrize('change', ['session', 'channel', 'subject', 'workspace', 'mode', 'plan', 'metadata_plan', 'profile', 'route', 'missing'])
def test_team_request_drift_rejected_before_runtime(host, change):
    adapter = create_adapter(execution_route=host.route)
    request = request_for(host)
    if change == 'session':
        request.session_id = 'other'
    elif change == 'channel':
        request.channel_id = 'other'
    elif change == 'subject':
        request.user_id = 'mallory'
    elif change == 'workspace':
        request.params['project_dir'] = '/other'
    elif change == 'mode':
        request.params['mode'] = 'agent.code.normal'
    elif change == 'plan':
        request.params['mode'] = 'team.code.plan'
    elif change == 'metadata_plan':
        request.metadata = {'mode': 'team.code.plan'}
    elif change == 'profile':
        request.params['execution_profile_id'] = 'default'
    elif change == 'route':
        request._execution_route = replace(host.route, bound=replace(host.route.bound,
                                           binding=replace(host.route.bound.binding)))
    else:
        del request._execution_route
    with pytest.raises(ValueError):
        adapter.select_execution_for_request(request)
    assert host.engines == []


@pytest.mark.asyncio
async def test_team_adapter_preserves_original_startup_lock_and_request_lifetime(host, monkeypatch):
    import asyncio
    from jiuwenswarm.server.runtime.agent_adapter import team_helpers
    adapter = create_adapter(execution_route=host.route)
    await adapter.create_instance()
    request = request_for(host, mode=False)
    lock = asyncio.Lock()
    calls = []
    manager = SimpleNamespace(begin_request=lambda *args: calls.append(('begin', args)),
                              end_request=lambda *args: calls.append(('end', args)),
                              get_startup_lock=lambda _: lock)
    monkeypatch.setattr(team_helpers, 'get_team_manager', lambda _: manager)
    heartbeat = object()
    adapter.set_heartbeat_service(heartbeat)
    chunk = AgentResponseChunk(request_id='request', channel_id='web', payload={'event_type': 'chat.delta', 'content': 'x'})
    async def existing_stream(req, inputs, parent, *, team_manager, startup):
        assert req.request_id == request.request_id and parent is None and inputs == {'query': 'hello'}
        assert req._execution_route is request._execution_route
        assert req.params['mode'] == req.metadata['mode'] == 'team.code.normal'
        assert team_manager is manager and lock.locked()
        assert team_helpers._TEAM_HEARTBEAT_SERVICE.get() is heartbeat
        try:
            yield chunk
        finally:
            calls.append(('closed', ()))
    monkeypatch.setattr(team_helpers, '_process_team_message_stream', existing_stream)
    previous = team_helpers._TEAM_HEARTBEAT_SERVICE.get()
    stream = adapter.process_message_stream_impl(request, {'query': 'hello'})
    try:
        assert await anext(stream) is chunk
        assert not lock.locked()
        assert [c[0] for c in calls] == ['begin']
    finally:
        await stream.aclose()
    assert [c[0] for c in calls] == ['begin', 'closed', 'end']
    assert team_helpers._TEAM_HEARTBEAT_SERVICE.get() is previous
    assert host.engines == []
    assert 'mode' not in request.params and request.metadata is None


@pytest.mark.asyncio
@pytest.mark.parametrize('intent', ['pause', 'cancel', 'resume'])
async def test_facade_team_control_uses_frozen_topology_when_request_omits_mode(host, monkeypatch, intent):
    from jiuwenswarm.server.runtime.agent_adapter.interface import JiuWenSwarm
    facade = object.__new__(JiuWenSwarm)
    facade._adapter = create_adapter(execution_route=host.route)
    facade._runtime_execution_route = host.route
    facade._session_manager = SimpleNamespace(get_session_id=lambda sid: sid, cancel_session_task=AsyncMock())
    manager = SimpleNamespace(pause_session_runtime=AsyncMock(return_value=True),
                              cancel_session_runtime=AsyncMock(return_value=True))
    monkeypatch.setattr('jiuwenswarm.agents.harness.team.get_team_manager', lambda _: manager)
    request = request_for(host, mode=False)
    request.req_method = ReqMethod.CHAT_CANCEL
    request.params['intent'] = intent
    request._execution_route = None  # Ordinary Gateway controls carry no route.
    response = await facade._process_interrupt(request)
    assert response.ok and response.payload['success'] == (intent == 'cancel')
    assert manager.pause_session_runtime.await_count == 0
    assert manager.cancel_session_runtime.await_count == int(intent == 'cancel')
    assert facade._session_manager.cancel_session_task.await_count == int(intent == 'cancel')
    request.session_id = 'victim'
    with pytest.raises(ValueError, match='identity changed'):
        await facade._process_interrupt(request)
    request.session_id = 'session'
    request.params['mode'] = 'team.code.normal'
    request._execution_route = replace(host.route, surface=replace(host.route.surface,
        identity=replace(host.route.surface.identity, topology='single')))
    with pytest.raises(ValueError, match='binding changed'):
        await facade._process_interrupt(request)
    assert manager.pause_session_runtime.await_count == 0
    assert manager.cancel_session_runtime.await_count == int(intent == 'cancel')


@pytest.mark.asyncio
async def test_team_adapter_cleanup_is_scoped_and_exit_failure_retryable(host, monkeypatch):
    adapter = create_adapter(execution_route=host.route)
    await adapter.create_instance()
    manager = SimpleNamespace(stop_session_runtime=AsyncMock(side_effect=[RuntimeError('exit unconfirmed'), True]),
                              cancel_session_runtime=AsyncMock())
    monkeypatch.setattr('jiuwenswarm.agents.harness.team.get_team_manager', lambda _: manager)
    assert await adapter.cleanup_session_adapter('other') is False
    with pytest.raises(RuntimeError, match='exit unconfirmed'):
        await adapter.cleanup()
    assert adapter._created
    await adapter.cleanup()
    assert not adapter._created
    assert manager.stop_session_runtime.await_count == 2
    await adapter.abort_on_gateway_disconnect(exclude_session_ids={'session'})
    manager.cancel_session_runtime.assert_not_awaited()
    await adapter.abort_on_gateway_disconnect()
    manager.cancel_session_runtime.assert_awaited_once_with(
        'session', reason='External Team gateway disconnect', workflow_disposition='pause',
        require_exit_confirmation=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('operation', ['handle_swarmflow_reply', 'handle_heartbeat'])
async def test_unconnected_operations_are_explicit_errors(host, operation):
    adapter = create_adapter(execution_route=host.route)
    result = await getattr(adapter, operation)(request_for(host))
    assert not result.ok
    assert result.payload['code'] == 'EXTERNAL_TEAM_OPERATION_UNAVAILABLE'
    assert host.engines == []


@pytest.mark.asyncio
@pytest.mark.parametrize('ending', ['eof', 'empty', 'error', 'terminal', 'cancel'])
async def test_external_team_stream_requires_authoritative_terminal(host, monkeypatch, ending):
    import asyncio
    from openjiuwen.agent_teams.schema.team import TeamRole
    from jiuwenswarm.server.runtime.agent_adapter import team_helpers
    events = []
    manager = SimpleNamespace(release_current_round=AsyncMock(), clear_pending_runtime=Mock(),
                              clear_active_runtime=Mock(), pop_stream_task=Mock())
    monkeypatch.setattr(team_helpers, 'get_team_manager', lambda _: manager)
    async def broadcast(channel, session, event):
        events.append(event)
    monkeypatch.setattr(team_helpers, '_broadcast_event', broadcast)
    monkeypatch.setattr(team_helpers, 'get_background_task_controller', lambda _: None)
    snapshot = AsyncMock(side_effect=AssertionError('EOF cannot synthesize Team completion'))
    monkeypatch.setattr(team_helpers, '_broadcast_team_state_snapshot', snapshot)
    async def runner(**kwargs):
        if ending != 'empty':
            yield SimpleNamespace(type='answer', role=TeamRole.LEADER,
                                  payload={'output': {'output': 'answer'}, 'result_type': 'answer'})
        if ending == 'error':
            raise RuntimeError('connection lost')
        if ending == 'cancel':
            raise asyncio.CancelledError()
        if ending == 'terminal':
            yield SimpleNamespace(type='team.completed', role=TeamRole.LEADER,
                                  payload={'event_type': 'team.completed'})
    monkeypatch.setattr(team_helpers, 'Runner', SimpleNamespace(run_agent_team_streaming=runner))
    coro = team_helpers._consume_stream_with_query('web', 'session',
            SimpleNamespace(team_name='team', execution_provider=host.provider), 'hello', round_id=7)
    if ending == 'cancel':
        with pytest.raises(asyncio.CancelledError):
            await coro
    else:
        await coro
    snapshot.assert_not_awaited()
    if ending != 'empty':
        assert any(e.get('event_type') == 'chat.final' and e.get('content') == 'answer' for e in events)
    if ending == 'error':
        assert any(e.get('error') == 'connection lost' for e in events)
    manager.release_current_round.assert_awaited_once_with('session')
    manager.pop_stream_task.assert_called_once_with('session')
    terminal = [e for e in events if e.get('event_type') == 'chat.processing_status' and e.get('is_complete')]
    if ending in {'eof', 'empty', 'error'}:
        assert len(terminal) == 1
        assert terminal[0]['terminal_status'] == 'unknown'
        assert terminal[0]['code'] == 'EXECUTION_TERMINAL_UNKNOWN'
        assert any(e.get('event_type') == 'chat.error' for e in events)
    elif ending == 'terminal':
        assert len(terminal) == 1 and 'terminal_status' not in terminal[0]
        assert not any(e.get('event_type') == 'chat.error' for e in events)
    else:
        assert terminal == []
    assert not any(e.get('event_type') == 'team.completed' for e in events)


@pytest.mark.asyncio
async def test_facade_retains_external_team_owner_when_cleanup_is_unconfirmed(host):
    from jiuwenswarm.server.runtime.agent_adapter.interface import JiuWenSwarm
    facade = object.__new__(JiuWenSwarm)
    adapter = SimpleNamespace(cleanup=AsyncMock(side_effect=[RuntimeError('exit unconfirmed'), None]))
    facade._adapter = adapter
    facade._runtime_execution_route = host.route
    facade._session_manager = SimpleNamespace(close_all_sessions=AsyncMock())
    with pytest.raises(RuntimeError, match='exit unconfirmed'):
        await facade.cleanup()
    assert facade._adapter is adapter and facade._runtime_execution_route is host.route
    await facade.cleanup()
    assert facade._adapter is None and facade._runtime_execution_route is None
    assert adapter.cleanup.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('control', ['idle_stop', 'active_cancel'])
async def test_original_runner_drives_external_team_and_releases_member_transport(host, monkeypatch, control):
    import asyncio
    from contextlib import suppress
    from openjiuwen.core.runner import Runner
    from openjiuwen.harness_protocol import (
        HarnessCapability, TurnEventKind, TurnStatus, HarnessState, TurnTermination, TurnTerminationKind,
    )
    from jiuwenswarm.runtime.harness import team_execution as module
    from tests.unit_tests.runtime.harness.test_team_execution import spec_for
    transports = []
    executed = asyncio.Event()
    running = asyncio.Event()
    cancelled = asyncio.Event()
    interrupted_results = []
    real_engine = module.create_harness_engine
    def engine(*args, **kwargs):
        value = real_engine(*args, **kwargs)
        execute = value.harness._execute_turn
        async def observed(turn):
            running.set()
            if control == 'active_cancel':
                await cancelled.wait()
            result = await execute(turn)
            executed.set()
            if control == 'active_cancel':
                result = replace(result[1], status=TurnStatus.INTERRUPTED,
                                 termination=TurnTermination(TurnTerminationKind.USER_ABORT))
                interrupted_results.append(result)
                return TurnEventKind.ABORTED, result
            return result
        value.harness._execute_turn = observed
        if control == 'active_cancel':
            value.harness.card = replace(value.harness.card, capabilities=frozenset({HarnessCapability.GRACEFUL_ABORT}))
            async def interrupt(turn, mode):
                cancelled.set()
            value.harness._interrupt_turn = interrupt
        return value
    monkeypatch.setattr(module, 'create_harness_engine', engine)
    real_transport = module.ManagedProductToolTransport
    def transport(*args, **kwargs):
        value = real_transport(*args, **kwargs)
        transports.append(value)
        return value
    monkeypatch.setattr(module, 'ManagedProductToolTransport', transport)
    spec = spec_for(host)
    chunks = []
    idle = asyncio.Event()
    async def consume():
        async for chunk in Runner.run_agent_team_streaming(agent_team=spec,
                inputs={'query': 'hello'}, session='session'):
            chunks.append(chunk)
            payload = getattr(chunk, 'payload', None)
            if (executed.is_set() and isinstance(payload, dict)
                    and payload.get('event_type') in {'team.idle', 'team.completed'}):
                idle.set()
    await Runner.start()
    task = asyncio.create_task(consume())
    try:
        async with asyncio.timeout(20):
            expected = running if control == 'active_cancel' else idle
            waiter = asyncio.create_task(expected.wait())
            try:
                done, _ = await asyncio.wait({task, waiter}, return_when=asyncio.FIRST_COMPLETED)
                if task in done:
                    await task
                assert expected.is_set(), [(getattr(c, 'type', None), getattr(c, 'payload', None)) for c in chunks]
            finally:
                waiter.cancel()
                with suppress(asyncio.CancelledError):
                    await waiter
            assert any(isinstance(getattr(c, 'payload', None), dict)
                       and c.payload.get('event_type') == 'team.runtime_ready' for c in chunks)
            assert len(host.engines) == 1
            assert host.engines[0].harness.readers == 1
            if control == 'active_cancel':
                assert host.engines[0].harness.state is HarnessState.RUNNING
            assert await Runner.stop_agent_team(team_name='team', session_id='session')
            if control == 'active_cancel':
                assert cancelled.is_set()
                assert len(interrupted_results) == 1
                assert interrupted_results[0].status is TurnStatus.INTERRUPTED
            assert host.engines[0].harness.state is HarnessState.TERMINATED
    finally:
        if not task.done():
            task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        await Runner.stop()
    assert transports and all(t.exit_confirmed and not t.started for t in transports)


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['command', 'attach', 'tui'])
async def test_unintegrated_goal_does_not_become_an_ordinary_team_turn(host, monkeypatch, kind):
    from jiuwenswarm.server.runtime.agent_adapter import team_helpers
    # The negative case deliberately omits the Goal persistence capability;
    # the independent Team identity archive remains present and valid.
    assert host.route.recovery.path.is_file()
    route = replace(host.route, recovery=None)
    if kind == 'tui':
        route = replace(route, channel_id='tui', surface=replace(route.surface,
            identity=replace(route.surface.identity, channel_id='tui')))
    adapter = create_adapter(execution_route=route)
    await adapter.create_instance()
    assert not adapter.supports_goal_execution and adapter._goal_runtime is None
    request = request_for(host)
    request._execution_route = route
    request.channel_id = route.channel_id
    if kind == 'command':
        request.req_method = ReqMethod.COMMAND_GOAL
        request.params.update(action='set', objective='goal')
    elif kind == 'tui':
        request.params['query'] = '/goal set root objective'
    else:
        request.params['attach_goal'] = True
    monkeypatch.setattr(team_helpers, 'process_team_message_stream', Mock(side_effect=AssertionError('ordinary Team Turn')))
    chunks = [c async for c in adapter.process_message_stream_impl(request, {'query': 'hello'})]
    assert len(chunks) == 1
    assert chunks[0].payload['code'] == 'EXTERNAL_TEAM_OPERATION_UNAVAILABLE'
    assert not host.engines


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', [True, False])
@pytest.mark.parametrize('intent', ['pause', 'resume'])
async def test_unavailable_active_control_keeps_owner_and_never_calls_legacy_ack(host, monkeypatch, mode, intent):
    from jiuwenswarm.server.runtime.agent_adapter.interface import JiuWenSwarm
    facade = object.__new__(JiuWenSwarm)
    adapter = create_adapter(execution_route=host.route)
    facade._adapter = adapter
    facade._runtime_execution_route = host.route
    facade._session_manager = SimpleNamespace(get_session_id=lambda sid: sid,
        cancel_session_task=AsyncMock(side_effect=AssertionError('cancelled owner')))
    facade._process_team_interrupt = AsyncMock(side_effect=AssertionError('legacy Team control'))
    monkeypatch.setattr('jiuwenswarm.agents.harness.team.get_team_manager',
                        Mock(side_effect=AssertionError('Team runtime allocation')))
    request = request_for(host, mode=mode)
    request.req_method = ReqMethod.CHAT_CANCEL
    request.params['intent'] = intent
    response = await facade._process_interrupt(request)
    assert response.ok  # existing envelope acknowledgement; success is the operation result
    assert response.payload['event_type'] == 'chat.interrupt_result'
    assert response.payload['intent'] == intent
    assert response.payload['success'] is False
    assert response.payload['code'] == 'EXTERNAL_TEAM_OPERATION_UNAVAILABLE'
    assert facade._adapter is adapter and facade._runtime_execution_route is host.route
    facade._process_team_interrupt.assert_not_awaited()
    facade._session_manager.cancel_session_task.assert_not_awaited()
    assert not host.engines


@pytest.mark.asyncio
@pytest.mark.parametrize('action', ['get', 'pause', 'clear', 'set', 'resume'])
@pytest.mark.parametrize('entry', ['direct', 'unary', 'stream'])
async def test_goal_rejection_survives_original_facade_and_does_not_start_work(host, monkeypatch, action, entry):
    from jiuwenswarm.server.runtime.agent_adapter.interface import JiuWenSwarm
    from jiuwenswarm.server.runtime.agent_adapter import team_helpers
    assert host.route.recovery.path.is_file()
    route = replace(host.route, recovery=None)
    adapter = create_adapter(execution_route=route)
    await adapter.create_instance()
    assert not adapter.supports_goal_execution and adapter._goal_runtime is None
    facade = object.__new__(JiuWenSwarm)
    facade._adapter = adapter
    facade._ensure_adapter = lambda **kwargs: adapter
    facade._session_manager = SimpleNamespace(get_session_id=lambda sid: sid)
    request = request_for(host, mode=False)
    request._execution_route = route
    request.req_method = ReqMethod.COMMAND_GOAL
    request.params.update(action=action, objective='root objective')
    unexpected = Mock(side_effect=AssertionError('unsupported Goal must not execute'))
    monkeypatch.setattr(team_helpers, 'process_team_message_stream', unexpected)
    monkeypatch.setattr('jiuwenswarm.server.runtime.agent_adapter.goal_model.request_goal_assessor_factory', unexpected)
    monkeypatch.setattr('jiuwenswarm.server.runtime.agent_adapter.interface.append_history_record', unexpected)
    if entry == 'direct':
        result = await adapter.handle_goal_command_structured(request.params, 'session')
        assert result['result_type'] == 'goal_error'
        assert result['error_code'] == result['code'] == 'EXTERNAL_TEAM_OPERATION_UNAVAILABLE'
        assert result['action'] == action
    elif entry == 'unary':
        result = await facade.execute_message(request)
        assert not result.ok
        assert result.payload['code'] == 'EXTERNAL_TEAM_OPERATION_UNAVAILABLE'
        assert result.payload['action'] == action and result.payload['error']
    else:
        # set/resume continue through ordinary facade setup; its final adapter
        # stream is tested here without fabricating a second execution owner.
        stream = (adapter.process_message_stream_impl(request, {'query': 'root objective'})
                  if action in {'set', 'resume'} else facade._process_message_stream(request))
        chunks = [chunk async for chunk in stream]
        assert len(chunks) == 1 and chunks[0].is_complete
        assert chunks[0].payload['event_type'] == 'chat.error'
        assert chunks[0].payload['code'] == 'EXTERNAL_TEAM_OPERATION_UNAVAILABLE'
        assert chunks[0].runtime_completion != 'completed'
    unexpected.assert_not_called()
    assert not host.engines


@pytest.mark.asyncio
async def test_admitted_recovery_enables_original_team_goal_without_starting_provider(host):
    from jiuwenswarm.runtime.harness.team_goal import ExternalTeamGoalRuntime

    adapter = create_adapter(execution_route=host.route)
    assert not adapter.supports_goal_execution
    await adapter.create_instance()
    assert adapter.supports_goal_execution
    assert isinstance(adapter._goal_runtime, ExternalTeamGoalRuntime)
    assert not host.engines


@pytest.mark.asyncio
@pytest.mark.parametrize('entry', ['unary', 'stream'])
async def test_goal_facade_rejects_wrong_session_before_adapter_control(host, monkeypatch, entry):
    from jiuwenswarm.server.runtime.agent_adapter.interface import JiuWenSwarm
    adapter = create_adapter(execution_route=host.route)
    control = AsyncMock(side_effect=AssertionError('wrong owner control'))
    monkeypatch.setattr(adapter, 'handle_goal_command_structured', control)
    facade = object.__new__(JiuWenSwarm)
    facade._adapter = adapter
    facade._ensure_adapter = lambda **kwargs: adapter
    facade._session_manager = SimpleNamespace(get_session_id=lambda sid: sid)
    request = request_for(host, mode=False)
    request.req_method = ReqMethod.COMMAND_GOAL
    request.params['action'] = 'get'
    request.session_id = 'victim'
    if entry == 'unary':
        result = await facade.execute_message(request)
        assert not result.ok and 'identity changed' in result.payload['error']
    else:
        chunks = [c async for c in facade._process_message_stream(request)]
        assert len(chunks) == 1 and 'identity changed' in chunks[0].payload['error']
    control.assert_not_awaited()
    assert not host.engines


@pytest.mark.asyncio
@pytest.mark.parametrize('method', [ReqMethod.CHAT_SEND, ReqMethod.CHAT_ANSWER])
@pytest.mark.parametrize('drift', [None, 'session', 'channel', 'subject', 'route'])
async def test_team_control_answer_uses_bound_route_and_never_opens_stream(host, monkeypatch, method, drift):
    from jiuwenswarm.server.runtime.agent_adapter.interface import JiuWenSwarm
    from openjiuwen.agent_teams.external.interaction_address import encode_interaction_address
    facade = object.__new__(JiuWenSwarm)
    adapter = create_adapter(execution_route=host.route)
    facade._runtime_execution_route = host.route
    facade._ensure_adapter = lambda **kwargs: adapter
    facade._build_inputs = Mock(side_effect=AssertionError('answer became new input'))
    adapter.process_message_stream_impl = Mock(side_effect=AssertionError('answer opened a producer'))
    interact = AsyncMock(return_value=(True, None))
    monkeypatch.setattr('jiuwenswarm.agents.harness.team.get_team_manager', lambda _: SimpleNamespace(interact=interact))
    request = AgentRequest(request_id='answer', channel_id='web', session_id='session', user_id='alice',
        req_method=method, params={'mode': 'team.code.normal', 'source': 'confirm_interrupt',
            'request_id': encode_interaction_address('team', 'review:invocation', 'session', 'cycle', 'request'),
            'answers': [{'selected_options': ['allow_once']}]})
    if drift in {'session', 'channel', 'subject'}:
        setattr(request, {'session': 'session_id', 'channel': 'channel_id', 'subject': 'user_id'}[drift], 'other')
    elif drift == 'route':
        request._execution_route = replace(host.route, channel_id='other')
    if drift:
        with pytest.raises(ValueError):
            _ = [chunk async for chunk in facade.deliver_control_input(request)]
        interact.assert_not_awaited()
    else:
        chunks = [chunk async for chunk in facade.deliver_control_input(request)]
        assert len(chunks) == 1 and chunks[0].payload['event_type'] == 'runtime.accepted'
        assert chunks[0].payload['resolved'] is True
        assert chunks[0].payload['request_id'] == request.request_id
        assert chunks[0].payload['interaction_id'] == request.params['request_id']
        interact.assert_awaited_once()
        assert getattr(request, '_execution_route', None) is None
    assert not host.engines


@pytest.mark.parametrize('case', ['send', 'answer', 'background', 'unbound', 'wrong_channel', 'wrong_session', 'ordinary'])
def test_runtime_only_classifies_controls_for_bound_external_team(host, case):
    from jiuwenswarm.runtime.service import AgentRuntime
    from jiuwenswarm.runtime.session import SessionWorkKind
    facade = SimpleNamespace(_runtime_execution_route=host.route if case != 'unbound' else None)
    runtime = AgentRuntime(agent_manager=SimpleNamespace(get_agent_for_session_nowait=lambda ch, sid: facade), initializer=AsyncMock())
    request = request_for(host)
    request.params.update(source='confirm_interrupt', request_id='pending', answers=[{'selected_options': ['allow_once']}])
    if case == 'answer':
        request.req_method = ReqMethod.CHAT_ANSWER
    if case == 'wrong_channel':
        request.channel_id = 'other'
    if case == 'wrong_session':
        request.session_id = 'other'
    if case == 'ordinary':
        request.params = {'mode': 'team.code.normal', 'query': 'hello'}
    kind = runtime._request_work_kind(request, background=case == 'background')
    assert kind is (SessionWorkKind.CONTROL_INPUT if case in {'send', 'answer'} else None)
    assert not host.engines


@pytest.mark.parametrize('stream', [True, False])
def test_ordinary_external_team_uses_runtime_owner_before_member_approval(host, monkeypatch, stream):
    from jiuwenswarm.runtime.service import AgentRuntime
    from jiuwenswarm.runtime.session import SessionWorkKind
    monkeypatch.setattr('jiuwenswarm.common.config.get_config', lambda: host.config)
    monkeypatch.setattr('jiuwenswarm.server.runtime.session.session_metadata.get_session_metadata',
                        lambda *args, **kwargs: host.metadata)
    runtime = object.__new__(AgentRuntime)
    runtime._agent_manager = SimpleNamespace(get_agent_for_session_nowait=lambda *args: None)
    request = request_for(host)
    request.is_stream = stream
    assert runtime._request_work_kind(request) is (
        SessionWorkKind.CHAT_STREAM if stream else SessionWorkKind.CHAT_UNARY)
    assert runtime._request_work_kind(request, background=True) is None
    host.metadata.pop('execution_profile_id')
    assert runtime._request_work_kind(request) is None


def test_team_trusted_route_uses_admitted_subject_and_keeps_wire_user_unmodified(host):
    route = replace(host.route, trusted_subject_id=host.route.bound.binding.subject_id)
    adapter = create_adapter(execution_route=route)
    request = request_for(host)
    request._execution_route = route
    request.user_id = 'untrusted-wire-alias'
    adapter.select_execution_for_request(request)
    assert request.user_id == 'untrusted-wire-alias'
    assert adapter.route.bound.binding.subject_id == 'alice'
    assert not host.engines


@pytest.mark.parametrize('changed', ['drop', 'replace', 'inject'])
def test_team_route_rejects_trusted_marker_drift(host, changed):
    route = (host.route if changed == 'inject'
             else replace(host.route, trusted_subject_id=host.route.bound.binding.subject_id))
    adapter = create_adapter(execution_route=route)
    supplied = replace(route, trusted_subject_id=None if changed == 'drop' else (
        'alice' if changed == 'inject' else 'different-trusted-subject'
    ))
    request = request_for(host)
    request._execution_route = supplied
    with pytest.raises(ValueError, match='binding changed'):
        adapter.select_execution_for_request(request)
    assert adapter.route is route
    assert not host.engines


def test_team_trusted_marker_must_match_its_admitted_binding(host):
    route = replace(host.route, trusted_subject_id='different-subject')
    adapter = create_adapter(execution_route=route)
    request = request_for(host)
    request._execution_route = route
    request.user_id = 'different-subject'
    with pytest.raises(ValueError, match='binding changed'):
        adapter.select_execution_for_request(request)
    assert not host.engines


@pytest.mark.parametrize('changed', ['session', 'channel'])
def test_team_trusted_route_does_not_override_session_or_channel(host, changed):
    route = replace(host.route, trusted_subject_id=host.route.bound.binding.subject_id)
    adapter = create_adapter(execution_route=route)
    request = request_for(host)
    request._execution_route = route
    request.user_id = 'untrusted-wire-alias'
    setattr(request, 'session_id' if changed == 'session' else 'channel_id', 'other')
    with pytest.raises(ValueError, match='identity changed'):
        adapter.select_execution_for_request(request)
    assert not host.engines
