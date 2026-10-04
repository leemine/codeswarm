"""Actual Runtime/Goal control with independent authenticated principals."""
import asyncio

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.governance.organization_auth import authenticated_scope
from jiuwenswarm.runtime.native_goal import capture_control
from jiuwenswarm.runtime.session import SessionWorkKind
from tests.unit_tests.runtime.harness import test_native_goal_control as fixtures

credentials = fixtures.credentials
native_case = fixtures.native_case
full_case = fixtures.full_case
live = fixtures.live
goal = fixtures.goal


def request(g, *, stream=False):
    return AgentRequest('goal-control', session_id=g.x.f.sid, channel_id='web',
        req_method=ReqMethod.COMMAND_GOAL, is_stream=stream,
        params={'action': 'pause', 'project_id': g.x.f.project.project_id})


async def run(g, req, operation):
    x = g.x
    parent = x.c.native_goal_parent(x.f.sid)
    assert parent is x.saved['owner']

    async def body():
        agent, cap = capture_control(x.runtime, req, parent)
        assert agent is x.f.facade
        yield await operation(cap)

    with authenticated_scope(x.second):
        return [v async for v in x.c.run_stream(x.f.sid, req.request_id,
            SessionWorkKind.GOAL_CONTROL, body, parent_execution_id=parent.execution_id)]


@pytest.mark.asyncio
async def test_runtime_captures_same_actor_new_credential_without_changing_owner(goal):
    g = goal
    result, = await run(g, request(g), lambda cap: cap.pause())
    assert result.status.value == 'paused'
    assert g.x.saved['owner']._execution_authority is g.x.f.bob
    assert g.manager._execution_origin[3] is g.pending._origin
    assert g.x.f.c.native._native.active_turn is g.pending
    assert len(g.x.f.c.native._requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['params', 'params_ref', 'request_id', 'channel_id', 'method',
                                  'metadata', 'stream', 'facade', 'child'])
async def test_fixed_runtime_goal_request_cannot_be_retargeted(goal, change):
    g, req = goal, request(goal)

    async def operation(cap):
        old_adapter = g.x.f.facade._adapter
        old_child = g.x.f.root._session_adapters[g.x.f.sid]
        try:
            if change == 'params':
                req.params['action'] = 'clear'
            elif change == 'params_ref':
                req.params = dict(req.params)
            elif change == 'metadata':
                req.metadata = {'different': True}
            elif change == 'stream':
                req.is_stream = not req.is_stream
            elif change == 'facade':
                g.x.f.facade._adapter = object()
            elif change == 'child':
                g.x.f.root._session_adapters[g.x.f.sid] = object()
            elif change == 'method':
                req.req_method = ReqMethod.CHAT_SEND
            else:
                setattr(req, change, 'different')
            with pytest.raises(Exception):
                await cap.pause()
        finally:
            g.x.f.facade._adapter = old_adapter
            g.x.f.root._session_adapters[g.x.f.sid] = old_child

    await run(g, req, operation)
    assert g.manager.peek().status.value == 'active'


@pytest.mark.asyncio
async def test_waiting_goal_control_rechecks_second_credential(goal):
    g, req = goal, request(goal)
    captured = asyncio.Event()
    original = g.x.f.authpath.read_bytes()

    async def operation(cap):
        captured.set()
        return await cap.pause()

    await g.manager._control_lock.acquire()
    task = asyncio.create_task(run(g, req, operation))
    try:
        await asyncio.wait_for(captured.wait(), 2)
        g.x.f.auth.revoke(g.x.second)
        g.manager._control_lock.release()
        with pytest.raises(Exception):
            await task
        assert g.manager.peek().status.value == 'active'
        assert g.x.f.bob.identity() == g.x.saved['owner']._execution_authority.identity()
    finally:
        g.x.f.authpath.write_bytes(original)
        if g.manager._control_lock.locked():
            g.manager._control_lock.release()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.parametrize('action', ['get', 'pause', 'clear'])
async def test_public_runtime_uses_original_facade_for_goal_control(goal, stream, action):
    g, req = goal, request(goal, stream=stream)
    req.params['action'] = action
    before = g.x.f.c.native._native.active_turn
    with authenticated_scope(g.x.second):
        if stream:
            results = [event async for event in g.x.runtime.stream(req)]
        else:
            results = await g.x.runtime.invoke(req)
    assert len(results) == 1 and results[0].ok
    assert results[0].payload['action'] == action
    assert results[0].payload.get('event_type', 'goal.snapshot') == 'goal.snapshot'
    assert g.x.f.c.native._native.active_turn is before
    assert g.x.saved['owner']._execution_authority is g.x.f.bob
    handle, = g.x.c._registry.select(session_id=g.x.f.sid, request_id='goal-control')
    assert handle.parent_execution_id == g.x.saved['owner'].execution_id
    assert handle._execution_authority is g.x.second and handle._native_admission is None
