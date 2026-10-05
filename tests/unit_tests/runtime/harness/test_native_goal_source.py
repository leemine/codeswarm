"""Initial Goal source through real Native/Goal/TaskLoop and consumer boundaries.

Only model response, Session IO/event bus and host decisions are synthetic.
No Provider/network or Runtime lifecycle is mocked into a successful receipt.
"""
import asyncio
import json

import httpx
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from openjiuwen.core.foundation.llm import ToolCall, Model, ModelClientConfig, ModelRequestConfig
from openjiuwen.core.foundation.tool import tool, current_tool_invocation
from openjiuwen.core.runner import Runner
from openjiuwen.core.runner.callback import AsyncCallbackFramework
from openjiuwen.core.single_agent.ability_manager import AbilityManager
from openjiuwen.core.single_agent.agent_callback_manager import AgentCallbackManager
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.goal import GoalAssessment, GoalAssessmentStatus, GoalEvaluator, GoalStopConfig, GoalStopStrategy
from openjiuwen.harness.schema.interaction import SendInputRequest
from openjiuwen.harness_protocol import AgentExecutionSpec, HarnessContext, TurnEventKind

from jiuwenswarm.agents.harness.common.rails.permissions.resource_authority_rail import (
    NativeExecutionScopeRail, NativeResourceAuthorityRail,
)
from jiuwenswarm.governance.model_consumer import NativeModelRequestAuthority
from jiuwenswarm.governance.model_credentials import ModelCredentialBinding
from jiuwenswarm.governance.tool_context import (
    ExecutionResourceAuthorities, current_native_execution_slice, tool_authority_scope,
)
from jiuwenswarm.runtime.harness.binding_store import ExecutionBindingStore
from jiuwenswarm.runtime.harness.config_source import ExecutionConfigSource
from jiuwenswarm.runtime.harness.native_session import NativeExecutionSession
from tests.unit_tests.governance._managed_native_fixture import model_free_agent
from tests.unit_tests.runtime.harness.test_native_request_origin import _Admission


class _GoalAdmission(_Admission):
    def __init__(self):
        self.check_hook = None
        super().__init__()

    def check(self):
        super().check()
        if self.check_hook is not None:
            self.check_hook()


@pytest.fixture
async def goal_case(tmp_path, monkeypatch):
    monkeypatch.setattr(Runner, 'callback_framework', AsyncCallbackFramework())
    session = SimpleNamespace(get_session_id=lambda: 'goal-session', get_agent_id=lambda: 'goal-agent')
    state = SimpleNamespace(slices=[], models=[], tools=[], side_effects=[], error=None,
                            before=None, outer_before=None, called=asyncio.Event(), contexts=[], http=[], outer_slices=[], tool_phases=[], attempts=1)
    manager = AbilityManager(owner_id='goal-' + tmp_path.name)
    react = SimpleNamespace(card=SimpleNamespace(name='inner', id='inner'), ability_manager=manager,
                            agent_callback_manager=AgentCallbackManager('inner-' + tmp_path.name))

    @tool(name='goal_echo', description='Synthetic local side effect')
    async def echo(text: str):
        state.side_effects.append(text)
        return text

    manager.add_ability(echo.card, echo)
    outer = model_free_agent(react, session, monkeypatch)
    # Actual outer iteration callback opens/closes the original execution slice.
    await outer.agent_callback_manager.register_rail(NativeExecutionScopeRail(), outer)
    # This exercises actual AbilityManager -> Tool final authority, not a direct
    # call to a saved host callback.
    await react.agent_callback_manager.register_rail(NativeResourceAuthorityRail(), react)

    async def remember(ctx):
        state.contexts.append(ctx)
        if state.before:
            await state.before(ctx)
    from openjiuwen.core.single_agent.rail.base import AgentCallbackEvent
    await outer.agent_callback_manager.register_callback(AgentCallbackEvent.BEFORE_TASK_ITERATION,
                                                         remember, priority=11000)

    binding = ModelCredentialBinding('goal-model', 'https://model.invalid/v1')
    async def network(_transport, request):
        payload = json.loads(request.content)
        assert request.headers['Authorization'] == 'Bearer synthetic-goal'
        state.http.append((str(request.url), payload['model']))
        return httpx.Response(200, json={'id': 'goal', 'object': 'chat.completion',
            'created': 1, 'model': payload['model'], 'choices': [{'index': 0,
            'message': {'role': 'assistant', 'content': 'OK'}, 'finish_reason': 'stop'}]})
    monkeypatch.setattr(httpx.AsyncHTTPTransport, 'handle_async_request', network)
    model = Model(ModelClientConfig(client_provider='OpenAI', api_base=binding.api_base,
        api_key='MUST_NOT_BE_CONSUMED', max_retries=0), ModelRequestConfig(model='goal-model'),
        request_authority=NativeModelRequestAuthority(binding))

    async def outer_consumer(ctx):
        state.outer_slices.append(current_native_execution_slice())
        if state.outer_before:
            await state.outer_before(ctx)
        assert (await model.invoke('outer auxiliary')).content == 'OK'
    await outer.agent_callback_manager.register_callback(AgentCallbackEvent.BEFORE_INVOKE,
                                                         outer_consumer, priority=9000)
    async def invoke(inputs, session=None, **kwargs):
        try:
            state.slices.append(current_native_execution_slice())
            assert (await model.invoke('inner goal')).content == 'OK'
            result = await manager.execute(AgentCallbackContext(agent=react),
                ToolCall(id='goal-call', type='function', name=echo.card.name, arguments='{"text":"goal"}'),
                session=session)
            assert 'goal' in str(result)
            outer._task_completion_rail._goal_report_sink.submit(
                GoalAssessment(status=(GoalAssessmentStatus.COMPLETE if len(state.side_effects) == state.attempts
                                       else GoalAssessmentStatus.CONTINUE),
                               evidence='actual consumers completed'))
            return {'result_type': 'answer', 'output': 'goal completed'}
        except BaseException as exc:
            state.error = exc
            raise
        finally:
            state.called.set()
    react.invoke = invoke
    admission = _GoalAdmission()
    async def model_authority(actual_binding, target, *, native_session):
        assert actual_binding == binding and native_session is native
        assert target.model == 'goal-model' and target.url == binding.destination
        state.models.append(target)
        return {'Authorization': 'Bearer synthetic-goal'}
    async def tool_authority(operation):
        state.tools.append(operation)
        state.tool_phases.append(current_tool_invocation() is not None)
        return True
    def capture_lifecycle(actual_native, actual_request):
        assert actual_native is native and actual_request is request
        return admission.lifecycle
    bundle = ExecutionResourceAuthorities({'native': tool_authority}, model_authority,
        native_lifecycle_factory=capture_lifecycle)

    async def dispatch(*, action, **kwargs):
        assert action == 'set'
        record = await outer.goal_manager.set(**kwargs)
        return {'result_type': 'goal_stream', 'goal': record.to_dict()}
    bound = ExecutionBindingStore().bind(
        ExecutionConfigSource(explicit=AgentExecutionSpec('native', 'initial-goal')),
        subject_id='bob', host_session_id='goal-session', workspace=str(tmp_path))
    native = NativeExecutionSession(bound, agent_factory=lambda _: outer,
        session_factory=AsyncMock(return_value=session), goal_dispatcher=dispatch,
        require_execution_origin=True)
    with tool_authority_scope(None, provider_authorizers=bundle):
        await native.start(HarnessContext(agent_name='goal', agent_id='outer',
            host_session_id='goal-session', cwd=str(tmp_path), system_prompt=''))
    outer._task_completion_rail._goal_evaluator = GoalEvaluator(GoalStopConfig(strategy=GoalStopStrategy.AGENT_REPORT))
    request = SendInputRequest('initial-goal-request', {'query': 'goal objective'})
    async def submit():
        with tool_authority_scope(None, provider_authorizers=bundle):
            receipt, result = await native.submit_goal('set', request=request, objective='goal objective', max_attempts=state.attempts)
        await asyncio.wait_for(result, 3)
        await asyncio.wait_for(admission.bound[0]._entry.terminal_event.wait(), 4)
        return receipt
    state.__dict__.update(locals())
    try:
        yield state
    finally:
        await asyncio.wait_for(native.stop(), 4)


@pytest.mark.asyncio
async def test_initial_goal_actual_iteration_consumes_original_tool_and_model_authority(goal_case):
    c = goal_case
    receipt = await c.submit()
    assert c.error is None
    assert c.called.is_set()
    assert len(c.models) == 2 and len(c.http) == 2 and len(c.tools) == 3  # before approval, after approval, final Tool gate
    assert c.tool_phases == [False, False, True]
    assert len(c.outer_slices) == 1 and not c.outer_slices[0].active
    assert c.side_effects == ['goal']
    assert len(c.slices) == 1 and not c.slices[0].active
    owned = c.admission.bound[0]
    assert owned.request_id == c.request.request_id and owned.turn_id == receipt.turn_id
    assert c.admission.terminal == [(owned, TurnEventKind.FINISHED)]
    assert c.outer.goal_manager.peek().status.value == 'completed'
    assert c.native._requests == {}


@pytest.mark.asyncio
@pytest.mark.parametrize('stage', ['outer', 'iteration'])
@pytest.mark.parametrize('change', ['child', 'no_source', 'same_value_foreign_source', 'expired',
                                    'wrong_token', 'wrong_revision', 'malformed_record',
                                    'replacement_entry'])
async def test_actual_goal_callback_cannot_rebind_unproved_source(goal_case, stage, change):
    """Try the same actual ctx while its original callback is active, then restore.

    The valid Goal must still complete using its original callbacks; a failed
    proof must neither clear stored state nor authorize a synthetic child Task.
    """
    from contextlib import nullcontext
    from dataclasses import replace
    from openjiuwen.core.controller.schema.execution_origin import ExecutionOrigin, execution_origin_scope
    from openjiuwen.harness.goal.store import SESSION_GOAL_RECORD_KEY
    from jiuwenswarm.governance.resources import ResourceAccessDenied
    c = goal_case
    checked = []

    async def inspect(ctx):
        original_context = ctx.inputs.run_context
        original_raw = c.session.get_state(SESSION_GOAL_RECORD_KEY)
        pending = c.native._native.active_turn
        entry = c.admission.bound[0]._entry
        token = pending.content.metadata['native.host_request']
        scope = nullcontext()
        if change == 'no_source':
            scope = execution_origin_scope(None)
        elif change == 'same_value_foreign_source':
            scope = execution_origin_scope(ExecutionOrigin(pending._origin.host_value,
                                                          _checker=c.admission.check))
        elif change == 'expired':
            c.admission.live = False
        elif change == 'wrong_token':
            ctx.inputs.run_context = replace(original_context,
                extra={**original_context.extra, 'native.host_request': 'foreign-token'})
        elif change == 'wrong_revision':
            ctx.inputs.run_context = replace(original_context,
                extra={**original_context.extra, 'revision': original_context.extra['revision'] + 1})
        elif change == 'malformed_record':
            c.session.update_state({SESSION_GOAL_RECORD_KEY: {'malformed': True}})
        elif change == 'replacement_entry':
            c.native._requests[token] = replace(entry)  # equal values, different original owner

        async def attempt():
            with pytest.raises(ResourceAccessDenied):
                c.native._execution_slice_for(ctx)
        try:
            with scope:
                if change == 'child':
                    await asyncio.create_task(attempt())
                else:
                    await attempt()
            if change == 'malformed_record':
                assert c.session.get_state(SESSION_GOAL_RECORD_KEY) == {'malformed': True}
            checked.append(True)
        finally:
            c.admission.live = True
            ctx.inputs.run_context = original_context
            c.session.update_state({SESSION_GOAL_RECORD_KEY: original_raw})
            c.native._requests[token] = entry
    if stage == 'outer':
        c.outer_before = inspect
    else:
        c.before = inspect
    await c.submit()
    assert checked == [True]
    assert len(c.http) == 2 and c.side_effects == ['goal'] and c.error is None


@pytest.mark.asyncio
async def test_managed_initial_goal_requires_real_request_and_keeps_resume_attach_closed(goal_case):
    from openjiuwen.harness_protocol import UnsupportedHarnessCapabilityError
    c = goal_case
    with tool_authority_scope(None, provider_authorizers=c.bundle):
        with pytest.raises(PermissionError, match='actual request'):
            await c.native.submit_goal('set', objective='goal')
        with pytest.raises(UnsupportedHarnessCapabilityError, match='idle Goal resume'):
            await c.native.submit_goal('resume', request=c.request)
        with pytest.raises(UnsupportedHarnessCapabilityError, match='admission'):
            await c.native.attach_goal(request=c.request)
    assert not c.admission.bound and not c.models and not c.tools
    assert c.native._requests == {} and c.outer.goal_manager.peek() is None


@pytest.mark.asyncio
@pytest.mark.parametrize('stage', ['outer', 'iteration'])
@pytest.mark.parametrize('change', ['request', 'context', 'binding'])
@pytest.mark.parametrize('reenter_at', [1, 3])
async def test_goal_proof_rechecks_facts_after_last_source_callback(goal_case, stage, change, reenter_at):
    from dataclasses import replace
    from jiuwenswarm.governance.resources import ResourceAccessDenied
    c = goal_case
    checks = []

    async def inspect(ctx):
        entry = c.admission.bound[0]._entry
        request, context, engine = entry.request, ctx.inputs.run_context, c.native.engine
        count = 0
        def mutate():
            nonlocal count
            count += 1
            if count == reenter_at:  # strict capture, Round check, final source check
                if change == 'request':
                    entry.request = replace(request)
                elif change == 'context':
                    ctx.inputs.run_context = replace(context,
                        extra={**context.extra, 'revision': context.extra['revision'] + 1})
                else:
                    c.native.engine = replace(engine, binding=replace(engine.binding))
        c.admission.check_hook = mutate
        try:
            with pytest.raises(ResourceAccessDenied):
                c.native._execution_slice_for(ctx)
            assert count >= reenter_at
            checks.append(True)
        finally:
            c.admission.check_hook = None
            entry.request = request
            ctx.inputs.run_context = context
            c.native.engine = engine
    if stage == 'outer':
        c.outer_before = inspect
    else:
        c.before = inspect
    await c.submit()
    assert checks == [True] and len(c.http) == 2 and c.side_effects == ['goal']


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['task_inputs', 'scheduler_slot'])
async def test_goal_final_source_check_cannot_replace_original_task_dispatch(goal_case, change):
    from jiuwenswarm.governance.resources import ResourceAccessDenied
    c = goal_case
    checked = []
    async def inspect(ctx):
        owned = c.outer._active_interaction_round
        stored = owned._task_capture.stored
        scheduler = owned._controller.task_scheduler
        original_inputs = stored.inputs
        original_slot = scheduler._owned_execution_tasks[owned.task_id]
        count = 0
        def mutate():
            nonlocal count
            count += 1
            if count == 3:
                if change == 'task_inputs':
                    stored.inputs = []
                else:
                    scheduler._owned_execution_tasks.pop(owned.task_id)
        c.admission.check_hook = mutate
        try:
            with pytest.raises(ResourceAccessDenied):
                c.native._execution_slice_for(ctx)
            assert count == 3
            checked.append(True)
        finally:
            c.admission.check_hook = None
            stored.inputs = original_inputs
            scheduler._owned_execution_tasks[owned.task_id] = original_slot
    c.before = inspect
    await c.submit()
    assert checked == [True] and c.side_effects == ['goal'] and len(c.http) == 2


@pytest.mark.asyncio
async def test_automatic_goal_attempt_keeps_original_pending_turn_and_authorities(goal_case):
    c = goal_case
    c.attempts = 2
    receipt = await c.submit()
    assert c.side_effects == ['goal', 'goal'] and c.error is None
    assert len(c.http) == 4 and len(c.slices) == len(c.outer_slices) == 2
    assert len(c.admission.bound) == 1
    owned = c.admission.bound[0]
    assert owned.turn_id == receipt.turn_id and owned.request_id == c.request.request_id
    assert c.admission.terminal == [(owned, TurnEventKind.FINISHED)]
    assert c.outer.goal_manager.peek().attempt_count == 2
    assert all(bound.model_authorizer is owned._entry.guarded_model_authority
               and bound.tool_authorizer is owned._entry.guarded_authority
               and not bound.active for bound in c.slices + c.outer_slices)
