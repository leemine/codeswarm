"""Actual Runtime/Native Goal -> AbilityManager/Tool authority, local synthetic IO."""
import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from openjiuwen.core.foundation.llm import ToolCall
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.tools.goal import GoalReportSink, SubmitGoalReportTool
from openjiuwen.core.controller.schema.execution_origin import ExecutionOrigin, execution_origin_scope
from contextlib import nullcontext
from openjiuwen.harness.goal.schema import GoalRecord
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.governance.organization_auth import authenticated_scope, current_identity
from jiuwenswarm.runtime import AgentRuntime
from jiuwenswarm.governance.resources import ResourceDefinition
from jiuwenswarm.governance.tool_context import tool_authority_scope
from jiuwenswarm.runtime.context import set_runtime_context, reset_runtime_context
from jiuwenswarm.runtime.session import SessionWorkKind
from tests.unit_tests.runtime import test_native_goal_readmission_runtime as base

credentials = base.credentials
goal_case = base.goal_case
native_case = base.native_case
full_case = base.full_case
@pytest.fixture
async def case(full_case):
    f = full_case
    # Exercise the production default resource store/resolver, not the source
    # fixture's deliberately injected resolver-less resource authorizer.
    runtime = AgentRuntime(agent_manager=f.manager, initializer=AsyncMock(),
        trusted_identity_resolver=lambda _: current_identity(), project_authorizer=f.access,
        organization_session_host=f.host)
    f.state.runtime = runtime
    coordinator = runtime._session_coordinator
    await coordinator.register_session(f.sid, 'web')
    record = GoalRecord.create(session_id=f.sid, objective='saved', max_attempts=1)
    f.c.outer.goal_manager._store.save(record)
    return SimpleNamespace(**locals())


async def execute(x):
    f, runtime = x.f, x.runtime
    request = AgentRequest('report-goal', session_id=f.sid, channel_id='web', is_stream=True,
        req_method=ReqMethod.CHAT_SEND, params={'project_id': f.project.project_id, 'attach_goal': True})
    async def body():
        real = runtime._resource_authorizers_for(request)
        bundle = replace(f.c.bundle, providers=real.providers, native_lifecycle_factory=real.native_lifecycle_factory)
        token = set_runtime_context(runtime, f.manager)
        try:
            with tool_authority_scope(None, provider_authorizers=bundle):
                await f.child._attach_native_goal_request(request, {'query': 'attach'})
                owner = x.coordinator.native_execution_owner(f.sid, request.request_id)
                await asyncio.wait_for(owner._native_admission.owned_turn._entry.result, 4)
            await asyncio.wait_for(owner._native_admission.confirmed.wait(), 4)
        finally:
            reset_runtime_context(token)
    with authenticated_scope(f.bob):
        await asyncio.wait_for(x.coordinator.run_unary(f.sid, request.request_id, SessionWorkKind.GOAL_ATTACH, body), 6)


@pytest.mark.asyncio
@pytest.mark.parametrize('change', [None, 'missing_grant', 'same_name', 'manager', 'late_attempt',
    'foreign_sink', 'source', 'policy_retarget', 'get_closed'])
async def test_original_goal_report_requires_exact_target_and_explicit_resource(case, monkeypatch, change):
    x, f = case, case.f
    c, store = f.c, f.access
    # The model-free source fixture bypasses DeepAgent._create_react_agent;
    # preserve its production shared AbilityManager (deep_agent.py:1206).
    c.react.ability_manager = c.outer.ability_manager
    revision = store.resource_grants(f.project.project_id, f.bob.identity())['resource_revision']
    if change != 'missing_grant':
        store.register_resource(f.project.project_id,
            ResourceDefinition('goal-report', 'tool',
                'native:get_current_goal' if change == 'get_closed' else 'native:submit_goal_report'),
            owner_subject_id='bob', actions=('invoke',), expected_revision=revision)
    seen = []
    async def invoke(inputs, session=None, **kwargs):
        assert (await c.model.invoke('synthetic goal report')).content == 'OK'
        rail, manager = c.outer._task_completion_rail, c.react.ability_manager
        tool = next(t for t in rail._goal_tools if type(t) is SubmitGoalReportTool)
        sink = tool._sink
        old_manager, old_revision = rail._goal_manager, sink._revision
        scope = nullcontext()
        if change == 'source':
            scope = execution_origin_scope(ExecutionOrigin(object()))
        if change == 'policy_retarget':
            from jiuwenswarm.server.runtime.session.project_access import ProjectAccessStore
            original_policy = ProjectAccessStore.authorize_resource
            def policy(actual, project, identity, req):
                result = original_policy(actual, project, identity, req)
                if req.resource_id == 'goal-report':
                    rail._goal_manager = object()
                return result
            monkeypatch.setattr(ProjectAccessStore, 'authorize_resource', policy)
        if change == 'same_name':
            class Replacement(SubmitGoalReportTool):
                async def invoke(self, inputs, **kwargs):
                    seen.append('replacement executed')
                    return await super().invoke(inputs, **kwargs)
            replacement = Replacement(sink, agent_id='replacement-report')
            manager.add_ability(replacement.card, replacement)
        elif change == 'foreign_sink':
            replacement = SubmitGoalReportTool(GoalReportSink(), agent_id='replacement-sink')
            manager.add_ability(replacement.card, replacement)
        elif change == 'manager':
            rail._goal_manager = object()
        elif change == 'late_attempt':
            sink._revision += 1
        try:
            with scope:
                result = await manager.execute(AgentCallbackContext(agent=c.react),
                    ToolCall(id='report', type='function', name=('get_current_goal' if change == 'get_closed' else 'submit_goal_report'),
                        arguments=json.dumps({} if change == 'get_closed' else {'status': 'complete', 'evidence': 'actual goal tool'})), session=session)
            seen.append((result, sink.report))
        finally:
            rail._goal_manager, sink._revision = old_manager, old_revision
        return {'result_type': 'answer', 'output': 'attempt ended'}
    c.react.invoke = invoke
    await execute(x)
    assert len(seen) == 1
    result, report = seen[0]
    if change is None:
        assert 'report_accepted' in str(result)
        assert report is not None and report.evidence == 'actual goal tool'
        assert c.outer.goal_manager.peek().status.value == 'completed'
    else:
        assert report is None and 'report_accepted' not in str(result)
    assert not c.native._requests
