"""Temporary reviewer binding, original IO, history/usage and recovery guard."""
import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from openjiuwen.agent_teams import TeamReviewRuntimeBuild
from openjiuwen.agent_teams.tools.locales import make_translator
from openjiuwen.agent_teams.tools.task_manager import TeamTaskManager
from openjiuwen.agent_teams.tools.tool_task import VerifyTaskTool, ViewTaskToolV2
from openjiuwen.core.session.agent_team import create_agent_team_session
from openjiuwen.harness_protocol import TurnUsage, HarnessState
from jiuwenswarm.runtime.harness import team_execution as module
from jiuwenswarm.runtime.harness.event_projection import ExternalEventProjection
from jiuwenswarm.runtime.harness.team_review import validate_review_recovery
from tests.unit_tests.runtime.harness.test_team_execution import host, spec_for, ScriptedHarness


async def prepare(host, monkeypatch, *, usage=True, history=True):
    spec = spec_for(host)
    leader = spec.build()
    spec.dispatch_mode = 'scheduled'
    manager = TeamTaskManager(team_name='team', member_name='reviewer', db=leader.team_backend.db,
                              messager=None, dispatch_mode='scheduled')
    tr = make_translator('en')
    tools = (VerifyTaskTool(manager, tr, desc_key='verify_task_scheduled'), ViewTaskToolV2(leader.team_backend, tr))
    session = create_agent_team_session(session_id='session', team_id='team')
    await session.pre_run()
    session.write_stream = AsyncMock()
    monkeypatch.setattr(ExternalEventProjection, 'persist_member_output', AsyncMock(return_value=history))
    original = ScriptedHarness._execute_turn
    async def execute(self, turn):
        kind, result = await original(self, turn)
        return kind, replace(result, usage=TurnUsage(input_tokens=2, output_tokens=1, total_tokens=3)) if usage else result
    monkeypatch.setattr(ScriptedHarness, '_execute_turn', execute)
    factory = module.ExternalTeamMemberFactory(host.route, team_name='team')
    request = TeamReviewRuntimeBuild(spec, 'reviewer', 'work', 1, 'invocation-one', 'Review output', 'en',
                                      tools, session, spec.build_context)
    return factory, request, leader


@pytest.mark.asyncio
@pytest.mark.parametrize('sink_kind', ['session', 'runner'])
async def test_review_uses_independent_binding_and_one_consumer(host, monkeypatch, sink_kind):
    factory, request, leader = await prepare(host, monkeypatch)
    sink = AsyncMock() if sink_kind == 'runner' else request.team_session.write_stream
    if sink_kind == 'runner':
        request = replace(request, output_sink=sink)
    review = factory.build_review_runtime(request)
    try:
        assert await asyncio.wait_for(review.run_once('Inspect'), 5) == 'done'
        assert review.binding.host_session_id != host.route.bound.binding.host_session_id
        assert review.runtime.harness.readers == 1
        context = review.runtime.harness.contexts[0]
        assert context.metadata['execution_kind'] == 'scheduled_review'
        assert context.metadata['review_invocation_id'] == request.invocation_id
        assert review.runtime.member_session is not None
        with pytest.raises(ValueError, match='unconfirmed'):
            validate_review_recovery(request.team_session)
        await review.dispose()
        validate_review_recovery(request.team_session)
        restored = create_agent_team_session(session_id='session', team_id='team')
        await restored.pre_run()
        validate_review_recovery(restored)
        assert review.runtime.harness.state is HarnessState.TERMINATED
        assert review._transport.exit_confirmed
        chunks = [call.args[0].payload for call in sink.await_args_list]
        if sink_kind == 'runner':
            request.team_session.write_stream.assert_not_awaited()
        assert chunks[-1]['execution_kind'] == 'scheduled_review'
        assert chunks[-1]['terminal_status'] == 'completed'
        with pytest.raises(RuntimeError, match='replayed'):
            await review.run_once('Again')
    finally:
        await review.dispose()
        await leader.harness.stop()
        await leader.team_backend.db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['usage', 'history'])
async def test_unknown_usage_or_history_blocks_cold_replay(host, monkeypatch, failure):
    factory, request, leader = await prepare(host, monkeypatch, usage=failure != 'usage', history=failure != 'history')
    review = factory.build_review_runtime(request)
    try:
        with pytest.raises(RuntimeError, match='unconfirmed'):
            await asyncio.wait_for(review.run_once('Inspect'), 5)
        await review.dispose()
        with pytest.raises(ValueError, match='unconfirmed'):
            validate_review_recovery(request.team_session)
        restored = create_agent_team_session(session_id='session', team_id='team')
        await restored.pre_run()
        with pytest.raises(ValueError, match='unconfirmed'):
            validate_review_recovery(restored)
        another = factory.build_review_runtime(replace(request, invocation_id='next'))
        try:
            with pytest.raises(ValueError, match='replay refused'):
                await another.run_once('Inspect')
            assert not another.runtime.harness.contexts
        finally:
            await another.dispose()
    finally:
        await review.dispose()
        await leader.harness.stop()
        await leader.team_backend.db.close()


@pytest.mark.asyncio
async def test_review_exit_failure_keeps_transport_and_marker_until_retry(host, monkeypatch):
    factory, request, leader = await prepare(host, monkeypatch)
    review = factory.build_review_runtime(request)
    try:
        await asyncio.wait_for(review.run_once('Inspect'), 5)
        review.runtime.harness.fail_stop = True
        with pytest.raises(RuntimeError, match='exit failed'):
            await review.dispose()
        assert not review._transport.exit_confirmed
        with pytest.raises(ValueError, match='unconfirmed'):
            validate_review_recovery(request.team_session)
        review.runtime.harness.fail_stop = False
        await review.dispose()
        validate_review_recovery(request.team_session)
        assert review._transport.exit_confirmed
    finally:
        review.runtime.harness.fail_stop = False
        await review.dispose()
        await leader.harness.stop()
        await leader.team_backend.db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('field,value', [('reviewer', 'other'), ('task_id', ''), ('review_round', 0)])
async def test_review_rejects_invalid_scope_before_engine_allocation(host, monkeypatch, field, value):
    factory, request, leader = await prepare(host, monkeypatch)
    before = len(host.engines)
    try:
        with pytest.raises(ValueError):
            factory.build_review_runtime(replace(request, **{field: value}))
        assert len(host.engines) == before
    finally:
        await leader.harness.stop()
        await leader.team_backend.db.close()


@pytest.mark.asyncio
async def test_review_interaction_uses_original_pending_and_rejects_stale(host, monkeypatch):
    from openjiuwen.core.session import InteractiveInput
    from openjiuwen.harness_protocol import ToolApprovalRequest, ToolApprovalDecision, HarnessStateError
    factory, request, leader = await prepare(host, monkeypatch)
    original = ScriptedHarness._execute_turn
    responses = []
    async def execute(self, turn):
        response = await self.contexts[0].interactions.handle(
            ToolApprovalRequest(request_id='same', call_id='tool', tool_name='shell'))
        responses.append(response)
        return await original(self, turn)
    monkeypatch.setattr(ScriptedHarness, '_execute_turn', execute)
    review = factory.build_review_runtime(request)
    task = asyncio.create_task(review.run_once('Inspect'))
    try:
        async with asyncio.timeout(5):
            while True:
                chunks = [call.args[0].payload for call in request.team_session.write_stream.await_args_list]
                pending = next((p for p in chunks if p.get('event_type') == 'chat.ask_user_question'), None)
                if pending:
                    break
                await asyncio.sleep(0)
            wrong = InteractiveInput()
            wrong.update('other', {'approved': True})
            with pytest.raises(HarnessStateError):
                await review.send(wrong)
            assert not task.done()
            answer = InteractiveInput()
            answer.update(pending['request_id'], {'approved': False})
            await review.send(answer)
            with pytest.raises(HarnessStateError):
                await review.send(answer)
            assert await task == 'done'
            assert responses[0].decision is ToolApprovalDecision.DENY
            assert review.runtime.harness.readers == 1
        from openjiuwen.agent_teams.schema.stream import TeamOutputSchema
        from jiuwenswarm.server.runtime.agent_adapter.team_helpers import _is_teammate_output
        projected = [call.args[0] for call in request.team_session.write_stream.await_args_list]
        assert projected and all(isinstance(chunk, TeamOutputSchema) and _is_teammate_output(chunk) for chunk in projected)
        assert all(chunk.source_member == request.reviewer for chunk in projected)
        assert all(chunk.payload['execution_kind'] == 'scheduled_review' for chunk in projected)
    finally:
        task.cancel()
        await review.dispose()
        await asyncio.gather(task, return_exceptions=True)
        await leader.harness.stop()
        await leader.team_backend.db.close()


@pytest.mark.asyncio
async def test_review_cancellation_preserves_unknown_state(host, monkeypatch):
    factory, request, leader = await prepare(host, monkeypatch)
    entered = asyncio.Event()
    stopped = asyncio.Event()
    original = ScriptedHarness._execute_turn
    async def execute(self, turn):
        from openjiuwen.harness_protocol import TurnEventKind, TurnStatus, TurnTermination, TurnTerminationKind
        entered.set()
        await stopped.wait()
        _, result = await original(self, turn)
        return TurnEventKind.ABORTED, replace(result, status=TurnStatus.INTERRUPTED, termination=TurnTermination(TurnTerminationKind.HARNESS_STOP))
    async def close(self):
        stopped.set()
    monkeypatch.setattr(ScriptedHarness, '_close_session', close)
    monkeypatch.setattr(ScriptedHarness, '_execute_turn', execute)
    review = factory.build_review_runtime(request)
    task = asyncio.create_task(review.run_once('Inspect'))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await review.dispose()
        assert review._transport.exit_confirmed
        restored = create_agent_team_session(session_id='session', team_id='team')
        await restored.pre_run()
        with pytest.raises(ValueError, match='unconfirmed'):
            validate_review_recovery(restored)
    finally:
        await review.dispose()
        await leader.harness.stop()
        await leader.team_backend.db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['task', 'round', 'status', 'selection'])
async def test_review_vote_rechecks_bound_task_round_and_authorization(host, monkeypatch, change):
    from types import SimpleNamespace
    from openjiuwen.harness_protocol import ToolInvocation
    from jiuwenswarm.runtime.harness import team_review
    factory, request, leader = await prepare(host, monkeypatch)
    captured = []
    original = team_review.ProductToolGateway
    def gateway(*args, **kwargs):
        value = original(*args, **kwargs)
        captured.append(value)
        return value
    monkeypatch.setattr(team_review, 'ProductToolGateway', gateway)
    manager = request.tools[0].task_manager
    monkeypatch.setattr(manager, 'get', AsyncMock(return_value=SimpleNamespace(
        review_round=2 if change == 'round' else 1,
        status='completed' if change == 'status' else 'in_review')))
    invoke = AsyncMock(side_effect=AssertionError('rejected vote reached original tool'))
    monkeypatch.setattr(request.tools[0], 'invoke', invoke)
    review = factory.build_review_runtime(request)
    try:
        await asyncio.wait_for(review.run_once('Inspect'), 5)
        if change == 'selection':
            host.config['execution']['profiles']['selected']['config_revision'] = 'changed'
        result = await captured[0].invoke(ToolInvocation(call_id='vote', name='verify_task', arguments={
            'task_id': 'other' if change == 'task' else 'work', 'decision': 'pass'}))
        assert result.is_error
        invoke.assert_not_called()
    finally:
        await review.dispose()
        await leader.harness.stop()
        await leader.team_backend.db.close()
