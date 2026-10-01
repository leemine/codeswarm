"""Member approvals, original product tools and durable typed projection."""
import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from openjiuwen.core.session.agent_team import create_agent_team_session
from openjiuwen.agent_teams.runtime.manager import TeamRuntimeManager
from openjiuwen.harness_protocol import (ToolApprovalRequest, ToolApprovalDecision, UserInputRequest,
    ToolInvocation, TurnLifecycleEvent, TurnEventKind, HarnessEvent)
from openjiuwen.harness_providers.io_adapter import ProjectedOutput
from openjiuwen.core.session.stream.base import OutputSchema
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.runtime.harness import team_execution as module
from jiuwenswarm.server.runtime.agent_adapter.team_engine_adapter import ExternalTeamAgentAdapter
from tests.unit_tests.runtime.harness.test_team_execution import host as host, spec_for
from tests.unit_tests.runtime.harness.test_team_engine_adapter import request_for


@pytest.mark.asyncio
@pytest.mark.parametrize('approval', [True, False])
async def test_product_answer_reaches_exact_original_member_pending(host, monkeypatch, approval):
    leader = spec_for(host).build()
    runtime = leader.harness
    manager = TeamRuntimeManager()
    manager._resolve_entry = AsyncMock(return_value=SimpleNamespace(
        agent=leader, team_name='team', current_session_id='session'))
    async def interact(session_id, answer):
        result = await manager.interact(answer, team_name='team', session_id=session_id)
        return bool(result), getattr(result, 'reason', None)
    monkeypatch.setattr('jiuwenswarm.agents.harness.team.get_team_manager',
                        lambda _: SimpleNamespace(interact=interact))
    await runtime.start(team_session=create_agent_team_session(session_id='session'))
    pending = asyncio.create_task(runtime.request_interaction(
        ToolApprovalRequest('same', 'call', 'shell') if approval else UserInputRequest('same', 'Choose', choices=('yes', 'no'))))
    try:
        chunk = await asyncio.wait_for(anext(runtime.outputs()), 2)
        payload = chunk.payload
        assert payload['event_type'] == 'chat.ask_user_question'
        assert payload['source_member'] == 'team_leader'
        assert payload['_team_history_owned']
        request = request_for(host)
        request.req_method = ReqMethod.CHAT_ANSWER
        request.params.update({'request_id': payload['request_id'], 'source': payload['source'],
            'answers': [{'question': payload['questions'][0]['question'],
                         'selected_options': ['reject' if approval else 'yes']}]})
        adapter = ExternalTeamAgentAdapter(host.route)
        response = await adapter.handle_user_answer(request)
        assert response.ok and response.payload['resolved']
        assert not (await adapter.handle_user_answer(request)).ok
        result = await asyncio.wait_for(pending, 1)
        if approval:
            assert result.decision is ToolApprovalDecision.DENY
        else:
            assert result.content['answers']['Choose'] == 'yes'
        assert host.engines[0].harness.readers == 1
    finally:
        await runtime.stop()
        await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.asyncio
async def test_original_subagent_tools_are_member_scoped_and_revoked(host, monkeypatch):
    from tests.unit_tests.runtime.harness.test_external_subagent_runtime import _Factory, _install_factory
    child_factory = _Factory()
    _install_factory(monkeypatch, child_factory)
    products = []
    original = module.ExternalSubagentRuntime
    def capture(*args, **kwargs):
        value = original(*args, **kwargs)
        products.append(value)
        return value
    monkeypatch.setattr(module, 'ExternalSubagentRuntime', capture)
    leader = spec_for(host).build()
    await leader.harness.start(team_session=create_agent_team_session(session_id='session'))
    try:
        product = products[0]
        names = product.gateway.tool_names
        assert {'subagent_spawn', 'subagent_wait', 'subagent_list', 'subagent_close', 'view_task'} <= set(names)
        assert not any(name.startswith('goal_') or name.startswith('heartbeat_') for name in names)
        call = ToolInvocation('spawn-1', 'subagent_spawn', {
            'subagent_type': 'code_agent', 'task_description': 'implement', 'display_name': 'Coder', 'role': 'code'})
        result = await product.gateway.invoke(call)
        assert not result.is_error, result.content
        async with asyncio.timeout(3):
            while not child_factory.created:
                await asyncio.sleep(0)
        child_request, parent = child_factory.created[0]
        assert parent.parent_session_id.startswith('session:team-member:')
        assert child_request.subagent_id.startswith(parent.parent_session_id + '_sub_')
        assert product.parent_session.get_state() == leader.harness.member_session.get_state()
        host.metadata['user_id'] = 'revoked'
        blocked = await product.gateway.invoke(ToolInvocation('list-1', 'subagent_list', {}))
        assert blocked.is_error
    finally:
        await leader.harness.stop()
    assert child_factory.closed
    assert products[0]._closed


def test_process_spawn_rejected_before_runtime_allocation(host):
    spec = spec_for(host)
    spec.spawn_mode = 'process'
    with pytest.raises(ValueError, match='inprocess'):
        spec.build()
    assert host.engines == []


@pytest.mark.asyncio
async def test_projection_scopes_tool_ids_persists_without_waiter_and_drops_replay(host, monkeypatch):
    import jiuwenswarm.runtime.harness.event_projection as ep
    persisted = []
    monkeypatch.setattr(ep, 'append_history_record', lambda **kw: persisted.append(kw))
    monkeypatch.setattr(ep, 'append_history_record_durable', lambda **kw: persisted.append(kw))
    leader = spec_for(host).build()
    await leader.harness.start(team_session=create_agent_team_session(session_id='session'))
    runtime = leader.harness
    # Use the installed observer and projection, never a second Provider cursor.
    event = HarnessEvent(host_session_id='session', agent_id='member', provider_session_id=runtime.session_id,
                         timestamp=1, sequence=1, turn_id='turn-1', event=TurnLifecycleEvent(TurnEventKind.STARTED))
    try:
        await runtime._host_event_observer(event)
        projected = await runtime._output_projection(ProjectedOutput(turn_id='turn-1', chunk=OutputSchema(
            type='tool_call', index=1, payload={'tool_name': 'shell', 'tool_call_id': 'same-tool', 'arguments': {}})))
        assert projected.payload['tool_call']['tool_call_id'].startswith('team-tool-')
        await runtime._output_projection(ProjectedOutput(turn_id='turn-1', chunk=OutputSchema(
            type='llm_output', index=2, payload={'content': 'visible text'})))
        terminal = await runtime._output_projection(ProjectedOutput(turn_id='turn-1', terminal=TurnEventKind.FINISHED))
        assert terminal.payload['event_type'] == 'team.member_turn'
        assert any(p['event_type'] == 'chat.final' and p['content'] == 'visible text' for p in persisted)
        assert any(p['event_type'] == 'team.member_turn' for p in persisted)
        before = len(persisted)
        assert await runtime._output_projection(ProjectedOutput(turn_id='turn-1', terminal=TurnEventKind.FINISHED)) is None
        assert len(persisted) == before
        assert host.engines[0].harness.readers == 1
    finally:
        await runtime.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize('action', ['allow_once', 'reject', 'cancel'])
async def test_browser_approval_uses_member_channel_and_cleanup(host, monkeypatch, action):
    from tests.unit_tests.runtime.harness.test_external_browser_admission import _identity
    from openjiuwen.core.session import InteractiveInput
    monkeypatch.setenv('BROWSER_RUNTIME_MCP_ENABLED', 'true')
    browsers = []
    real = module.ExternalBrowserAdmission
    def capture(**kwargs):
        value = real(**kwargs)
        browsers.append(value)
        return value
    monkeypatch.setattr(module, 'ExternalBrowserAdmission', capture)
    leader = spec_for(host).build()
    await leader.harness.start(team_session=create_agent_team_session(session_id='session'))
    member_session = host.engines[0].binding.host_session_id
    identity = _identity(host.route.runtime_paths)
    identity = replace(identity, instance=replace(identity.instance, parent_session_id=member_session,
                                                  subagent_id=member_session + '_sub_browser_1'))
    decision = asyncio.create_task(browsers[0](identity, ToolInvocation('click-1', 'browser_click', {})))
    try:
        chunk = await asyncio.wait_for(anext(leader.harness.outputs()), 3)
        payload = chunk.payload
        assert payload['source_member'] == 'team_leader'
        assert leader.harness.member_session.get_state('external_member_interaction_pending') is True
        if action == 'cancel':
            await leader.harness.stop()
            assert await asyncio.wait_for(decision, 2) is False
        else:
            answer = InteractiveInput()
            answer.update(payload['request_id'], {'answers': {payload['questions'][0]['question']: action}})
            await leader.harness.send(answer)
            assert await asyncio.wait_for(decision, 2) is (action == 'allow_once')
            with pytest.raises(Exception, match='stale'):
                await leader.harness.send(answer)
    finally:
        await leader.harness.stop()
        await asyncio.gather(decision, return_exceptions=True)


@pytest.mark.asyncio
async def test_unconfirmed_interaction_marker_blocks_cold_start_and_is_not_erased(host):
    leader = spec_for(host).build()
    session = create_agent_team_session(session_id='session')
    await leader.harness.start(team_session=session)
    member = leader.harness.member_session
    await leader.harness.stop()
    member.update_state({'external_member_interaction_pending': True})
    await member.commit()
    from openjiuwen.harness_protocol import HarnessStateError
    with pytest.raises(HarnessStateError, match='unconfirmed'):
        await leader.harness.start(team_session=session)
    assert member.get_state('external_member_interaction_pending') is True


@pytest.mark.asyncio
async def test_member_uses_real_product_child_factory_with_same_provider(host, monkeypatch):
    from jiuwenswarm.runtime.harness import bridge
    from openjiuwen.harness.engine import HarnessEngine
    from tests.unit_tests.runtime.harness.test_team_execution import ScriptedHarness
    children = []
    def child_engine(spec, *, binding):
        engine = HarnessEngine(binding, ScriptedHarness(spec.provider_id))
        children.append(engine)
        return engine
    monkeypatch.setattr(bridge, 'create_harness_engine', child_engine)
    products = []
    original = module.ExternalSubagentRuntime
    def capture(*args, **kwargs):
        product = original(*args, **kwargs)
        products.append(product)
        return product
    monkeypatch.setattr(module, 'ExternalSubagentRuntime', capture)
    leader = spec_for(host).build()
    await leader.harness.start(team_session=create_agent_team_session(session_id='session'))
    try:
        result = await products[0].gateway.invoke(ToolInvocation('spawn-real', 'subagent_spawn', {
            'subagent_type': 'code_agent', 'task_description': 'local deterministic task',
            'display_name': 'Code helper', 'role': 'code'}))
        assert not result.is_error, result.content
        async with asyncio.timeout(5):
            while not children or not children[0].harness.contexts:
                await asyncio.sleep(0)
        assert children[0].binding.provider_id == host.provider
        assert children[0].binding.subject_id != host.route.bound.binding.subject_id
        assert children[0].binding.host_session_id.startswith(host.engines[0].binding.host_session_id + '_sub_')
        assert children[0].harness.readers == 1
    finally:
        await leader.harness.stop()
    assert all(child.harness.state.value == 'terminated' for child in children)


@pytest.mark.asyncio
async def test_facade_does_not_rewrite_history_owned_member_output(host, monkeypatch):
    from tests.unit_tests.agentserver import test_interrupt_history as fixture
    original = fixture._ScriptedAdapter
    class MemberAdapter(original):
        async def process_message_stream_impl(self, request, inputs):
            request._execution_route = host.route
            async for chunk in super().process_message_stream_impl(request, inputs):
                yield chunk
    monkeypatch.setattr(fixture, '_ScriptedAdapter', MemberAdapter)
    recorded = await fixture._run_stream(monkeypatch, [
        {'event_type': 'chat.delta', 'content': 'already persisted', '_team_history_owned': True},
        {'event_type': 'chat.final', 'content': 'already persisted', '_team_history_owned': True},
        {'event_type': 'chat.ask_user_question', 'request_id': 'scoped-question',
         'questions': [{'question': 'Continue?'}], '_team_history_owned': True},
    ])
    assert recorded == []


@pytest.mark.asyncio
@pytest.mark.parametrize('history_failure', [False, True])
async def test_hidden_teammate_question_is_delivered_and_reopens_terminal_boundary(host, monkeypatch, history_failure):
    from jiuwenswarm.server.runtime.agent_adapter import team_helpers as helper
    from openjiuwen.agent_teams.schema.stream import TeamOutputSchema
    from openjiuwen.agent_teams.schema.team import TeamRole
    events = []
    manager = SimpleNamespace(release_current_round=AsyncMock(), clear_pending_runtime=lambda *_: None,
        clear_active_runtime=lambda *_: None, pop_stream_task=lambda *_: None,
        get_workflow_handler=lambda *_: None, pop_held_idle=lambda *_: None)
    monkeypatch.setattr(helper, 'get_team_manager', lambda _: manager)
    monkeypatch.setattr(helper, '_team_hide_teammate_enabled', lambda: True)
    monkeypatch.setattr(helper, 'get_background_task_controller', lambda _: None)
    async def broadcast(channel, session, event):
        events.append(event)
    monkeypatch.setattr(helper, '_broadcast_event', broadcast)
    async def stream(**kwargs):
        yield TeamOutputSchema(type='message', index=0, role=TeamRole.LEADER,
            payload={'event_type': 'team.idle'})
        yield TeamOutputSchema(type='team_projection', index=1, role=TeamRole.TEAMMATE,
            source_member='worker', payload={'event_type': 'chat.ask_user_question',
                'request_id': 'member-question', 'questions': [{'question': 'Allow?'}]})
        if history_failure:
            yield TeamOutputSchema(type='team_projection', index=2, role=TeamRole.TEAMMATE,
                source_member='worker', payload={'event_type': 'chat.error',
                    'code': 'HISTORY_PERSISTENCE_UNCONFIRMED', 'terminal_status': 'unknown'})
            yield TeamOutputSchema(type='message', index=3, role=TeamRole.LEADER,
                payload={'event_type': 'team.idle'})
    monkeypatch.setattr(helper, 'Runner', SimpleNamespace(run_agent_team_streaming=stream))
    await helper._consume_stream_with_query('web', 'session',
        SimpleNamespace(team_name='team', execution_provider=host.provider), 'query', round_id=1)
    assert any(e.get('event_type') == 'chat.ask_user_question' and e.get('member_name') == 'worker' for e in events)
    if history_failure:
        failed_at = next(i for i, e in enumerate(events) if e.get('code') == 'HISTORY_PERSISTENCE_UNCONFIRMED')
        terminals = [e for e in events[failed_at:] if e.get('event_type') == 'chat.processing_status']
        assert terminals and all(e.get('terminal_status') == 'unknown' for e in terminals)
    else:
        assert any(e.get('code') == 'EXECUTION_TERMINAL_UNKNOWN' for e in events)


@pytest.mark.asyncio
async def test_history_failure_is_visible_and_cannot_be_rewritten_as_member_success(host, monkeypatch):
    import jiuwenswarm.runtime.harness.event_projection as ep
    def fail(**kwargs):
        raise OSError('isolated history unavailable')
    monkeypatch.setattr(ep, 'append_history_record_durable', fail)
    leader = spec_for(host).build()
    runtime = leader.harness
    await runtime.start(team_session=create_agent_team_session(session_id='session'))
    try:
        await runtime._host_event_observer(HarnessEvent(
            host_session_id='session', agent_id='member', provider_session_id=runtime.session_id,
            timestamp=1, sequence=1, turn_id='turn-failed-history',
            event=TurnLifecycleEvent(TurnEventKind.STARTED)))
        failure = await runtime._output_projection(ProjectedOutput(turn_id='turn-failed-history',
            chunk=OutputSchema(type='tool_result', index=1, payload={
                'tool_name': 'shell', 'tool_call_id': 'call', 'output': 'done'})))
        assert failure.payload['code'] == 'HISTORY_PERSISTENCE_UNCONFIRMED'
        monkeypatch.setattr(ep, 'append_history_record_durable', lambda **kwargs: None)
        terminal = await runtime._output_projection(ProjectedOutput(
            turn_id='turn-failed-history', terminal=TurnEventKind.FINISHED))
        assert terminal.payload['terminal_status'] == 'unknown'
        assert terminal.payload['event_type'] == 'chat.error'
        assert host.engines[0].harness.readers == 1
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_goal_round_observes_actual_member_and_original_product_child_once(host, monkeypatch):
    from openjiuwen.harness.engine import HarnessEngine
    from openjiuwen.harness_protocol import TurnUsage
    from jiuwenswarm.runtime.harness import bridge
    from jiuwenswarm.runtime.harness.goal_evidence import GoalAttemptIdentity
    from jiuwenswarm.runtime.session import RuntimeSessionCoordinator, SessionWorkKind
    from jiuwenswarm.agents.harness.team.team_manager import TeamManager
    from tests.unit_tests.runtime.harness.test_team_execution import ScriptedHarness
    manager = TeamManager()
    monkeypatch.setattr('jiuwenswarm.agents.harness.team.get_team_manager', lambda _: manager)
    children = []
    def with_usage(harness, count):
        original = harness._execute_turn
        async def execute(turn):
            kind, result = await original(turn)
            return kind, replace(result, usage=TurnUsage(input_tokens=count, output_tokens=2))
        harness._execute_turn = execute
        return harness
    def child_engine(spec, *, binding):
        result = HarnessEngine(binding, with_usage(ScriptedHarness(spec.provider_id), 5))
        children.append(result)
        return result
    monkeypatch.setattr(bridge, 'create_harness_engine', child_engine)
    products = []
    original_products = module.ExternalSubagentRuntime
    def capture(*args, **kwargs):
        product = original_products(*args, **kwargs)
        products.append(product)
        return product
    monkeypatch.setattr(module, 'ExternalSubagentRuntime', capture)
    leader = spec_for(host).build()
    with_usage(leader.harness.harness, 10)
    await leader.team_backend.db.initialize()
    coordinator = RuntimeSessionCoordinator()
    await coordinator.register_session('session', 'web')
    async def operation():
        owner = coordinator.external_execution_owner('session', 'goal-request')
        await coordinator.acquire_external_execution(owner, goal=True)
        manager.begin_round('session', 'goal-request', defer_terminal_release=True)
        identity = GoalAttemptIdentity('goal', 1, 1, owner.execution_id, owner.generation)
        evidence = manager.bind_goal_attempt_evidence('session', 'goal-request', identity=identity, runtime=coordinator)
        await leader.harness.start(team_session=create_agent_team_session(session_id='session'))
        try:
            await leader.harness.send('collect member evidence')
            async with asyncio.timeout(5):
                while not evidence.all_terminal:
                    await asyncio.sleep(0)
            result = await products[0].gateway.invoke(ToolInvocation('spawn-goal-child', 'subagent_spawn', {
                'subagent_type': 'code_agent', 'task_description': 'collect product evidence',
                'display_name': 'Code helper', 'role': 'code'}))
            assert not result.is_error, result.content
            async with asyncio.timeout(5):
                while evidence.turn_count != 2 or not evidence.all_terminal:
                    await asyncio.sleep(0)
            assert not evidence.error and evidence.usage_complete
            assert not evidence.ready_for_assessment and evidence.take_usage_delta() is None
            # Only the host producer can declare its tested Round boundary;
            # this test does not run a Goal driver or call its manager.
            evidence.seal('completed')
            assert evidence.ready_for_assessment
            usage = evidence.take_usage_delta()
            assert (usage.input_tokens, usage.output_tokens, usage.total_tokens) == (15, 4, 19)
            assert evidence.take_usage_delta() is None
            assert '/product/' in evidence.transcript and '/member/' in evidence.transcript
            assert host.engines[0].harness.readers == 1 and children[0].harness.readers == 1
        finally:
            await leader.harness.stop()
            await manager.release_round('session', 'goal-request')
            coordinator.release_external_execution(owner)
        assert not evidence.accepting and not evidence.ready_for_assessment
    try:
        await coordinator.run_unary('session', 'goal-request', SessionWorkKind.GOAL_STREAM, operation)
    finally:
        await coordinator.close()
        await leader.team_backend.db.close()
