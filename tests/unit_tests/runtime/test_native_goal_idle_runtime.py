"""Actual authenticated Runtime/facade/idle Goal controls, synthetic model IO."""
import asyncio
import pytest
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.governance.organization_auth import authenticated_scope
from tests.unit_tests.runtime import test_native_goal_readmission_runtime as base

credentials = base.credentials
goal_case = base.goal_case
native_case = base.native_case
full_case = base.full_case
case = base.case

@pytest.mark.asyncio
@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.parametrize('action', ['pause', 'clear'])
async def test_prepared_idle_public_runtime_uses_original_facade_without_turn(case, stream, action):
    x, f = case, case.f
    req = AgentRequest('idle-' + action, session_id=f.sid, channel_id='web', is_stream=stream,
        req_method=ReqMethod.COMMAND_GOAL, params={'action': action, 'project_id': f.project.project_id})
    before = dict(f.c.native._requests)
    with authenticated_scope(f.bob):
        result = ([v async for v in x.runtime.stream(req)] if stream else await x.runtime.invoke(req))
    assert len(result) == 1 and result[0].ok
    assert result[0].payload['action'] == action
    assert f.c.native._requests == before and not f.c.side_effects
    assert f.c.native._native._first_managed_turn is None
    assert f.c.outer.goal_manager.peek() is None if action == 'clear' else f.c.outer.goal_manager.peek().status.value == 'paused'

@pytest.mark.asyncio
async def test_completed_original_goal_can_be_cleared_after_registry_eviction(case):
    x, f = case, case.f
    old = await base.execute(x)
    x.coordinator._registry._remove(old.owner)
    req = AgentRequest('idle-clear', session_id=f.sid, channel_id='web',
        req_method=ReqMethod.COMMAND_GOAL, params={'action': 'clear', 'project_id': f.project.project_id})
    before = len(f.c.side_effects)
    with authenticated_scope(f.bob):
        results = await x.runtime.invoke(req)
    assert len(results) == 1 and results[0].ok
    assert results[0].payload['cleared_goal']['goal_id'] == x.record.goal_id
    assert f.c.outer.goal_manager.peek() is None and len(f.c.side_effects) == before
    assert old.owner._execution_authority is f.bob

@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['binding', 'session', 'store', 'control_lock', 'send_lock', 'events'])
async def test_first_controller_callback_cannot_replace_original_idle_target(case, change):
    from dataclasses import replace
    from jiuwenswarm.runtime.native_goal_idle import capture_control
    from jiuwenswarm.runtime.session import SessionWorkKind
    from jiuwenswarm.governance.preparation import GovernanceError
    x, f = case, case.f
    req = AgentRequest('idle-retarget', session_id=f.sid, channel_id='web',
        req_method=ReqMethod.COMMAND_GOAL, params={'action': 'clear', 'project_id': f.project.project_id})
    native, agent = f.c.native, f.c.outer
    manager = agent.goal_manager
    original = x.runtime._trusted_identity_resolver
    restore = []
    def change_attr(obj, attr, value):
        restore.append((obj, attr, getattr(obj, attr)))
        object.__setattr__(obj, attr, value)
    def resolver(request):
        if not restore:
            if change == 'binding':
                change_attr(native.engine, 'binding', replace(native.engine.binding))
            elif change == 'session':
                change_attr(manager._store, '_session', object())
            elif change == 'store':
                from openjiuwen.harness.goal.store import SessionGoalStore
                change_attr(manager, '_store', SessionGoalStore(f.c.session))
            elif change == 'control_lock':
                lock = asyncio.Lock()
                change_attr(manager, '_control_lock', lock)
                change_attr(agent, '_interaction_control_lock', lock)
            elif change == 'send_lock':
                change_attr(agent, '_interaction_send_lock', asyncio.Lock())
            else:
                change_attr(agent, '_event_manager', object())
        return original(request)
    async def body():
        x.runtime._trusted_identity_resolver = resolver
        try:
            with pytest.raises(GovernanceError):
                capture_control(x.runtime, req)
        finally:
            x.runtime._trusted_identity_resolver = original
            for obj, attr, old in reversed(restore):
                object.__setattr__(obj, attr, old)
    with authenticated_scope(f.bob):
        await x.coordinator.run_unary(f.sid, req.request_id, SessionWorkKind.GOAL_CONTROL, body)
    assert manager.peek().goal_id == x.record.goal_id and not f.c.side_effects
