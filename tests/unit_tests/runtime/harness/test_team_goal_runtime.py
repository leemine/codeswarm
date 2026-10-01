"""Original Runtime/GoalManager and encrypted root store for Team attempts."""
import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from openjiuwen.harness.goal import GoalStatus
from openjiuwen.harness_protocol import TurnEventKind, TurnLifecycleEvent, TurnUsage
from jiuwenswarm.common.schema.agent import AgentResponseChunk
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.runtime.context import set_runtime_context, reset_runtime_context
from jiuwenswarm.runtime.harness import team_goal
from jiuwenswarm.runtime.harness.recovery_store import SessionExecutionRecovery
from jiuwenswarm.runtime.harness.team_goal_evidence import TeamGoalEventObserver
from jiuwenswarm.runtime.session import RuntimeSessionCoordinator, SessionWorkKind
from jiuwenswarm.agents.harness.team.team_manager import TeamManager
from jiuwenswarm.server.runtime.agent_adapter.team_engine_adapter import ExternalTeamAgentAdapter
from tests.unit_tests.runtime.harness.test_team_execution import host as host
from tests.unit_tests.runtime.harness.test_execution_recovery import recovery_env as recovery_env
from tests.unit_tests.runtime.harness.test_team_engine_adapter import request_for
from tests.unit_tests.runtime.harness.test_team_goal_evidence import source, event, finished


@pytest.fixture
async def chain(host, recovery_env, monkeypatch):
    recovery = SessionExecutionRecovery(session_id='session', execution_profile_id='selected',
        binding=host.route.bound.binding, runtime_paths=host.route.runtime_paths)
    host.route = replace(host.route, recovery=recovery)
    adapter = ExternalTeamAgentAdapter(host.route)
    await adapter.create_instance()
    manager = TeamManager()
    monkeypatch.setattr('jiuwenswarm.agents.harness.team.get_team_manager', lambda _: manager)
    for name in ('record_goal_set', 'flush_goal_set', 'record_goal_completed'):
        monkeypatch.setattr(team_goal, name, AsyncMock())
    runtime = RuntimeSessionCoordinator()
    await runtime.register_session('session', 'web')
    state = SimpleNamespace(host=host, adapter=adapter, runtime=runtime, manager=manager,
        rounds=0, stopped=0, assessments=[], usage=True, boundary=True, fail_stop=False,
        assessor_usage=True, fail_assessor=False, entered=asyncio.Event(), release=asyncio.Event(),
        assess_entered=asyncio.Event(), assess_release=asyncio.Event(), chunks=[], statuses=['complete'])
    state.release.set(); state.assess_release.set()
    async def execute(request, inputs, goal):
        state.rounds += 1
        manager.begin_round('session', request.request_id, defer_terminal_release=True)
        goal.bind_round(manager)
        for src in (source(), source('worker', True)):
            src = replace(src, root_session_id='session')
            observer = TeamGoalEventObserver(src, lambda: manager.current_goal_attempt_evidence('session'))
            await observer(event(src, 1, TurnLifecycleEvent(TurnEventKind.STARTED)))
            state.entered.set()
            await state.release.wait()
            await observer(event(src, 2, finished(TurnUsage(input_tokens=5, output_tokens=1) if state.usage else None)))
        if state.boundary:
            yield AgentResponseChunk(request_id=request.request_id, channel_id='web',
                payload={'event_type': 'chat.processing_status', 'is_processing': False, 'is_complete': True})
    async def stop(goal):
        if state.fail_stop:
            raise RuntimeError('owned exit unconfirmed')
        state.stopped += 1
    monkeypatch.setattr(adapter, 'process_goal_round', execute)
    monkeypatch.setattr(adapter, 'confirm_goal_round_exit', stop)
    class Model:
        async def invoke(self, messages, **kwargs):
            assert state.stopped >= state.rounds and kwargs['tools'] == []
            if getattr(state, 'require_provider_exit', False):
                from openjiuwen.harness_protocol import HarnessState
                assert state.host.engines and all(e.harness.state is HarnessState.TERMINATED for e in state.host.engines)
            state.assess_entered.set()
            await state.assess_release.wait()
            if state.fail_assessor: raise RuntimeError('model error')
            state.assessments.append(messages)
            status=state.statuses[min(len(state.assessments)-1, len(state.statuses)-1)]
            return SimpleNamespace(content='{"status":"'+status+'","evidence":"checked","next_instruction":"continue"}',
                                   usage_metadata={'input_tokens':3,'output_tokens':1} if state.assessor_usage else {})
    adapter.set_goal_assessor_factory(lambda: Model())
    yield state
    await runtime.close()


def req(chain, request_id='goal', action='set'):
    request=request_for(chain.host)
    request.request_id=request_id; request.req_method=ReqMethod.COMMAND_GOAL
    request.params.update(action=action, objective='private team objective', max_attempts=3)
    return request


async def run(chain, request=None, kind=SessionWorkKind.GOAL_STREAM):
    request=request or req(chain)
    token=set_runtime_context(chain.runtime, None)
    async def consume():
        async for chunk in chain.adapter.process_message_stream_impl(request, {'query':'hello'}):
            chain.chunks.append(chunk)
        return chain.chunks
    try:
        return await chain.runtime.run_unary('session', request.request_id, kind, consume)
    finally:
        reset_runtime_context(token)


@pytest.mark.asyncio
async def test_two_rounds_assess_after_exit_account_once_and_restore_encrypted_goal(chain):
    chain.statuses=['continue','complete']
    await run(chain)
    goal=chain.adapter._goal_runtime.manager.peek()
    assert goal.status is GoalStatus.COMPLETED and goal.attempt_count==2
    assert goal.token_usage.total_tokens==32  # each round: 12 provider + 4 independent assessor
    assert len(chain.assessments)==chain.rounds==chain.stopped==2
    assert not chain.manager.is_round_active('session')
    assert chain.adapter._goal_runtime.owner is None
    restored=ExternalTeamAgentAdapter(chain.host.route);await restored.create_instance()
    assert restored._goal_runtime.manager.peek().to_dict()==goal.to_dict()
    assert not restored._goal_runtime.cold_unconfirmed
    assert 'private team objective' not in chain.host.route.recovery.path.read_text()
    assert sum(c.runtime_completion=='completed' for c in chain.chunks)==1


@pytest.mark.asyncio
@pytest.mark.parametrize('bad', ['missing_usage','eof','assessor_usage','assessor_error'])
async def test_incomplete_round_or_assessor_cannot_complete_or_repeat_work(chain,bad):
    if bad=='missing_usage':chain.usage=False
    if bad=='eof':chain.boundary=False
    if bad=='assessor_usage':chain.assessor_usage=False
    if bad=='assessor_error':chain.fail_assessor=True
    await run(chain)
    goal=chain.adapter._goal_runtime.manager.peek()
    assert goal.status is GoalStatus.BLOCKED
    assert chain.rounds==1
    assert not any(c.runtime_completion=='completed' for c in chain.chunks)
    if bad!='eof':
        restored=ExternalTeamAgentAdapter(chain.host.route);await restored.create_instance()
        result=await restored.handle_goal_command_structured({'action':'resume'},'session')
        assert result['error_code']=='goal_usage_unavailable'


@pytest.mark.asyncio
async def test_wrong_runtime_work_kind_rejected_before_goal_or_round(chain):
    with pytest.raises(ValueError,match='original Runtime Goal'):
        await run(chain,kind=SessionWorkKind.CHAT_UNARY)
    assert chain.rounds==0 and chain.adapter._goal_runtime.manager.peek() is None


@pytest.mark.asyncio
@pytest.mark.parametrize('payload', [
    {'event_type': 'team.member_turn', 'terminal_status': 'failed'},
    {'event_type': 'team.member_turn', 'terminal_status': 'unknown'},
    {'event_type': 'team.error'},
    {'event_type': 'chat.error'},
])
async def test_failed_member_settles_without_waiting_for_team_idle(chain, monkeypatch, payload):
    closed = asyncio.Event()
    async def failed_review(request, inputs, goal):
        chain.rounds += 1
        chain.manager.begin_round('session', request.request_id, defer_terminal_release=True)
        goal.bind_round(chain.manager)
        try:
            yield AgentResponseChunk(request_id=request.request_id, channel_id='web', payload=payload)
            await asyncio.Event().wait()  # Original task remains IN_REVIEW.
        finally:
            closed.set()
    monkeypatch.setattr(chain.adapter, 'process_goal_round', failed_review)
    await asyncio.wait_for(run(chain), 2)
    assert closed.is_set() and chain.stopped == 1 and not chain.assessments
    goal = chain.adapter._goal_runtime.manager.peek()
    assert goal.status is GoalStatus.BLOCKED and goal.attempt_count == 1
    assert chain.adapter._goal_runtime.accounting_unknown
    assert any(c.runtime_completion == 'failed' for c in chain.chunks)
    assert not any(c.runtime_completion == 'completed' for c in chain.chunks)


@pytest.mark.asyncio
async def test_stop_failure_retains_original_owner_and_durable_replay_block(chain):
    chain.fail_stop=True
    with pytest.raises(RuntimeError,match='exit unconfirmed'):
        await run(chain)
    runtime=chain.adapter._goal_runtime
    assert runtime.owner is not None and runtime.attempt is not None
    assert chain.runtime.holds_external_execution(runtime.owner)
    assert chain.assessments==[]
    restored=ExternalTeamAgentAdapter(chain.host.route);await restored.create_instance()
    result=await restored.handle_goal_command_structured({'action':'resume'},'session')
    assert result['error_code']=='goal_recovery_unconfirmed'
    chain.fail_stop=False
    await runtime._cleanup_attempt(assessor_unknown=False)
    chain.runtime.release_external_execution(runtime.owner)
    runtime.release_owner(runtime.owner)


@pytest.mark.asyncio
async def test_assessor_cancellation_keeps_spent_usage_and_blocks_cold_resend(chain):
    chain.assess_release.clear()
    task=asyncio.create_task(run(chain))
    await asyncio.wait_for(chain.assess_entered.wait(),2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):await task
    assert chain.adapter._goal_runtime.manager.peek().token_usage.total_tokens==12
    restored=ExternalTeamAgentAdapter(chain.host.route);await restored.create_instance()
    assert restored._goal_runtime.accounting_unknown
    assert not chain.manager.is_round_active('session')


@pytest.mark.asyncio
async def test_final_history_retains_permit_until_original_facade_acknowledges(chain,monkeypatch):
    request=req(chain);request._defer_execution_until_history=True
    await run(chain,request)
    goal=chain.adapter._goal_runtime
    assert goal.owner is not None and chain.runtime.holds_external_execution(goal.owner)
    monkeypatch.setattr('jiuwenswarm.server.runtime.agent_adapter.goal_history.flush_goal_set',AsyncMock())
    await chain.adapter.complete_request_history(request)
    assert goal.owner is None and request._execution_history_complete


@pytest.mark.asyncio
@pytest.mark.parametrize('attempts',[1,2])
async def test_original_team_helper_and_runner_execute_and_settle_goal(chain, monkeypatch, attempts):
    from openjiuwen.core.runner import Runner
    from jiuwenswarm.runtime.harness import team_execution
    from jiuwenswarm.server.runtime.agent_adapter import team_helpers
    from tests.unit_tests.runtime.harness.test_team_execution import spec_for
    chain.statuses=['continue']*(attempts-1)+['complete']
    chain.require_provider_exit=True
    adapter = chain.adapter
    monkeypatch.setattr(adapter, 'process_goal_round', ExternalTeamAgentAdapter.process_goal_round.__get__(adapter))
    monkeypatch.setattr(adapter, 'confirm_goal_round_exit', ExternalTeamAgentAdapter.confirm_goal_round_exit.__get__(adapter))
    monkeypatch.setattr(team_helpers, 'get_team_manager', lambda _: chain.manager)
    monkeypatch.setattr(chain.manager, 'get_swarm_enriched_team_spec', AsyncMock(side_effect=lambda **_: spec_for(chain.host)))
    monkeypatch.setattr(team_helpers, '_persist_team_file_monitor_roots', lambda *args: None)
    databases=[]
    build_member=team_execution.ExternalTeamMemberFactory.build_member_runtime
    def member(factory,request):
        databases.append(request.team_backend.db)
        return build_member(factory,request)
    monkeypatch.setattr(team_execution.ExternalTeamMemberFactory,'build_member_runtime',member)
    build=team_execution.create_harness_engine
    def engine(*args,**kwargs):
        value=build(*args,**kwargs);execute=value.harness._execute_turn
        async def with_usage(turn):
            kind,result=await execute(turn)
            return kind,replace(result,usage=TurnUsage(input_tokens=5,output_tokens=1))
        value.harness._execute_turn=with_usage
        return value
    monkeypatch.setattr(team_execution,'create_harness_engine',engine)
    await Runner.start()
    try:
        async with asyncio.timeout(20):
            await run(chain)
        record=adapter._goal_runtime.manager.peek()
        assert record.status is GoalStatus.COMPLETED, record.to_dict()
        assert record.token_usage.total_tokens==10*attempts
        assert len(chain.host.engines)==attempts and all(e.harness.readers==1 for e in chain.host.engines)
        assert not chain.manager.is_round_active('session')
    finally:
        await chain.manager.stop_session_runtime('session',require_exit_confirmation=True)
        await Runner.stop()
        for db in set(databases):
            await db.close()


@pytest.mark.asyncio
async def test_user_cancel_retains_round_until_original_producer_accounts(chain):
    chain.release.clear()
    task=asyncio.create_task(run(chain))
    await asyncio.wait_for(chain.entered.wait(),2)
    assert await chain.adapter.cancel_active_goal()
    with pytest.raises(asyncio.CancelledError): await task
    goal=chain.adapter._goal_runtime
    assert goal.owner is None and not chain.manager.is_round_active('session')
    assert goal.accounting_unknown and goal.manager.peek().status is GoalStatus.PAUSED
    assert chain.assessments==[]


@pytest.mark.asyncio
async def test_goal_pause_only_stops_continuation_and_allows_current_assessment(chain):
    chain.assess_release.clear();chain.statuses=['continue','complete']
    task=asyncio.create_task(run(chain))
    await asyncio.wait_for(chain.assess_entered.wait(),2)
    response=await chain.adapter.handle_goal_command_structured({'action':'pause'},'session')
    assert response['goal']['status']=='paused'
    chain.assess_release.set();await task
    assert chain.rounds==1 and chain.adapter._goal_runtime.manager.peek().status is GoalStatus.PAUSED
    await run(chain,req(chain,'resume','resume'))
    assert chain.rounds==2 and chain.adapter._goal_runtime.manager.peek().status is GoalStatus.COMPLETED


@pytest.mark.asyncio
async def test_completion_history_failure_never_emits_success_and_retains_history_owner(chain,monkeypatch):
    monkeypatch.setattr(team_goal,'record_goal_completed',AsyncMock(side_effect=OSError('history receipt unavailable')))
    request=req(chain);request._defer_execution_until_history=True
    with pytest.raises(OSError,match='history receipt'):
        await run(chain,request)
    goal=chain.adapter._goal_runtime
    assert not any(c.runtime_completion=='completed' for c in chain.chunks)
    assert goal.owner is not None and chain.runtime.holds_external_execution(goal.owner)
    assert not chain.manager.is_round_active('session')


@pytest.mark.asyncio
async def test_persistence_failure_before_provider_submission_never_executes(chain,monkeypatch):
    recovery=chain.host.route.recovery
    original=recovery.save_host_state
    def write(state):
        if state.get('harness.goal.external_attempt'):
            raise OSError('disk receipt unavailable')
        return original(state)
    monkeypatch.setattr(recovery,'save_host_state',write)
    with pytest.raises(OSError,match='disk receipt'):
        await run(chain)
    assert chain.rounds==0 and chain.assessments==[]
    restored=ExternalTeamAgentAdapter(chain.host.route);await restored.create_instance()
    assert restored._goal_runtime.cold_unconfirmed
    result=await restored.handle_goal_command_structured({'action':'resume'},'session')
    assert result['error_code']=='goal_recovery_unconfirmed'


@pytest.mark.asyncio
async def test_goal_bounded_waiter_ignores_leader_final_until_team_boundary():
    from jiuwenswarm.server.runtime.agent_adapter.team_helpers import _wait_for_bounded_team_round_events
    queue=asyncio.Queue()
    await queue.put({'event_type':'chat.final','content':'leader done'})
    await queue.put({'event_type':'chat.processing_status','is_processing':False,'is_complete':True})
    events=[e async for e in _wait_for_bounded_team_round_events(request_queue=queue, round_state={},
        request_id='goal',channel_id='web',session_id='session',require_team_terminal=True)]
    assert len(events)==2 and events[-1]['event_type']=='chat.processing_status'


@pytest.mark.asyncio
async def test_active_goal_refuses_ordinary_team_steer_without_cross_attempt_usage(chain):
    chain.release.clear()
    task=asyncio.create_task(run(chain))
    await asyncio.wait_for(chain.entered.wait(),2)
    request=request_for(chain.host);request.request_id='ordinary'
    chunks=[c async for c in chain.adapter.process_message_stream_impl(request,{'query':'steer'})]
    assert chunks[0].payload['code']=='goal_owner_busy' and chain.rounds==1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):await task


@pytest.mark.asyncio
async def test_manager_exit_waits_for_original_history_consumer_before_sealing(chain,monkeypatch):
    manager=chain.manager
    evidence=object()
    manager.begin_round('session','goal',defer_terminal_release=True)
    manager._active_rounds['session'].goal_evidence=evidence
    entered=asyncio.Event();release=asyncio.Event()
    async def history():
        entered.set();await release.wait()
    stream=asyncio.create_task(history())
    manager._stream_tasks['session']=stream
    stop=AsyncMock(return_value=True)
    monkeypatch.setattr(manager,'_resolve_session_team_name',lambda _: 'team')
    monkeypatch.setattr(manager,'_stop_runner_team_runtime',stop)
    monkeypatch.setattr(manager,'_stop_runner_team_agent_transport',AsyncMock())
    task=asyncio.create_task(manager.confirm_goal_round_exit('session','goal',evidence))
    await entered.wait();await asyncio.sleep(0)
    assert not task.done() and manager.is_round_owner('session','goal')
    release.set();await task
    stop.assert_awaited_once()
    assert manager.current_goal_attempt_evidence('session') is evidence
    manager._active_rounds.pop('session');manager._stream_tasks.pop('session')


@pytest.mark.asyncio
async def test_assessor_late_response_after_cancel_accounts_cost_without_completion(chain):
    entered=asyncio.Event()
    class Model:
        async def invoke(self,*args,**kwargs):
            entered.set()
            try:await asyncio.Event().wait()
            except asyncio.CancelledError:pass
            return SimpleNamespace(content='{"status":"complete","evidence":"late"}',
                                   usage_metadata={'input_tokens':3,'output_tokens':1})
    chain.adapter.set_goal_assessor_factory(lambda:Model())
    task=asyncio.create_task(run(chain));await asyncio.wait_for(entered.wait(),2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):await task
    goal=chain.adapter._goal_runtime
    assert goal.manager.peek().status is GoalStatus.PAUSED
    assert goal.manager.peek().token_usage.total_tokens==16
    assert not goal.accounting_unknown and not any(c.runtime_completion=='completed' for c in chain.chunks)


@pytest.mark.asyncio
async def test_busy_goal_stream_cannot_claim_existing_producer(chain):
    chain.release.clear()
    task=asyncio.create_task(run(chain));await asyncio.wait_for(chain.entered.wait(),2)
    await run(chain,req(chain,'replacement'))
    assert chain.rounds==1 and any((c.payload or {}).get('code')=='goal_owner_busy' for c in chain.chunks)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):await task
