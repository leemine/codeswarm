"""Actual Runtime principals/Native Turn/GoalManager control; synthetic model IO."""
import asyncio
from types import SimpleNamespace

import pytest
from openjiuwen.core.controller.schema.execution_origin import execution_origin_scope
from openjiuwen.harness.goal.manager import GoalManager
from openjiuwen.harness.goal.schema import GoalStatus
from openjiuwen.harness.goal.store import SessionGoalStore

from jiuwenswarm.governance.organization_auth import authenticated_scope
from jiuwenswarm.runtime.harness.native_goal_control import capture_native_goal_control
from jiuwenswarm.runtime.session import SessionWorkKind
from tests.unit_tests.runtime import test_native_steer_host as fixtures

credentials = fixtures.credentials
native_case = fixtures.native_case
full_case = fixtures.full_case
live = fixtures.live


@pytest.fixture
async def goal(live):
    x = live
    agent = x.f.c.agent
    manager = GoalManager(store=SessionGoalStore(x.f.c.session),
        event_manager=agent._event_manager, control_lock=agent._interaction_control_lock,
        has_output_stream=agent.has_output_stream, cancel_active_round=agent._cancel_active_round,
        emit_event=agent._emit_interaction_event, notify_work=agent._notify_work)
    manager._execution._owner = agent
    agent.goal_manager = manager
    pending = x.saved['admission'].owned_turn._pending
    with execution_origin_scope(pending._origin):
        original = await manager.set('original queued goal')
    try:
        yield SimpleNamespace(**locals())
    finally:
        # Fixture-only state cleanup prevents synthetic models without Goal
        # assessment from continuing forever after the owned user is released.
        manager._store.clear()
        agent._event_manager.discard_goal_work(session_id=x.f.sid, goal_id=original.goal_id)


async def control(g, operation, *, linked=True):
    x = g.x
    async def body():
        admission, check = x.c.native_goal_control_admission(x.f.sid, 'goal-control')
        cap = capture_native_goal_control(x.f.c.native, source=admission.source,
                                         request_id='goal-control', check_current=check)
        yield await operation(cap)
    with authenticated_scope(x.second):
        return [v async for v in x.c.run_stream(
            x.f.sid, 'goal-control', SessionWorkKind.GOAL_CONTROL, body,
            parent_execution_id=x.saved['owner'].execution_id if linked else None)]


@pytest.mark.asyncio
async def test_actual_goal_pause_keeps_original_execution_and_credentials(goal):
    g = goal
    result, = await control(g, lambda cap: cap.pause())
    assert result.status is GoalStatus.PAUSED
    assert g.manager._execution_origin[3] is g.pending._origin
    assert g.x.saved['owner']._execution_authority is g.x.f.bob
    assert not g.x.root.done() and not g.pending.abort_requested
    handle, = g.x.c._registry.select(session_id=g.x.f.sid, request_id='goal-control')
    assert handle._execution_authority is g.x.second
    assert handle._native_admission is None


@pytest.mark.asyncio
async def test_goal_control_without_explicit_parent_is_not_a_new_execution(goal):
    with pytest.raises(RuntimeError):
        await control(goal, lambda cap: cap.pause(), linked=False)
    assert goal.manager.peek().status is GoalStatus.ACTIVE


@pytest.mark.asyncio
async def test_clear_only_queued_goal_does_not_abort_original_user_round(goal):
    g = goal
    result, = await control(g, lambda cap: cap.clear())
    assert result.goal_id == g.original.goal_id and g.manager.peek() is None
    assert not g.x.root.done() and not g.pending.abort_requested
    assert g.x.f.c.agent._active_interaction_round.work.kind == 'user'
    assert not g.x.f.c.agent._event_manager._capture_origin_work(g.pending._origin)


@pytest.mark.asyncio
async def test_control_producer_completion_prevents_late_goal_mutation(goal):
    saved = []
    async def save(cap):
        saved.append(cap)
    await control(goal, save)
    with pytest.raises(RuntimeError):
        await saved[0].pause()
    assert goal.manager.peek().status is GoalStatus.ACTIVE


@pytest.mark.asyncio
async def test_goal_lock_wait_revalidates_original_input_task(goal):
    g = goal
    captured = asyncio.Event()
    saved = []
    async def pause(cap):
        saved.append(cap)
        captured.set()
        return await cap.pause()
    await g.manager._control_lock.acquire()
    task = asyncio.create_task(control(g, pause))
    await asyncio.wait_for(captured.wait(), 2)
    handle, = g.x.c._registry.select(session_id=g.x.f.sid, request_id='goal-control')
    handle.cancellation_requested = True
    g.manager._control_lock.release()
    with pytest.raises(RuntimeError):
        await task
    assert g.manager.peek().status is GoalStatus.ACTIVE


@pytest.mark.asyncio
@pytest.mark.parametrize('replacement', ['binding', 'manager', 'entry', 'origin'])
async def test_original_native_goal_host_references_reject_replacement(goal, replacement):
    from dataclasses import replace
    g = goal
    async def attempt(cap):
        n, entry = g.x.f.c.native, cap.owned._entry
        if replacement == 'binding':
            old = n.engine
            n.engine = replace(old, binding=replace(old.binding))
            def restore():
                n.engine = old
        elif replacement == 'manager':
            old = g.agent.goal_manager
            g.agent.goal_manager = object()
            def restore():
                g.agent.goal_manager = old
        elif replacement == 'entry':
            token = g.pending.content.metadata['native.host_request']
            n._requests[token] = object()
            def restore():
                n._requests[token] = entry
        else:
            old = g.pending._origin
            g.pending._origin = object()
            def restore():
                g.pending._origin = old
        try:
            with pytest.raises(PermissionError):
                await cap.pause()
        finally:
            restore()
    await control(g, attempt)
    assert g.manager.peek().status is GoalStatus.ACTIVE


@pytest.mark.asyncio
@pytest.mark.parametrize('replacement', ['record', 'round'])
async def test_temporary_checker_cannot_retarget_capture(goal, replacement):
    from openjiuwen.harness.goal.schema import GoalRecord
    g=goal
    async def body():
        admission, check=g.x.c.native_goal_control_admission(g.x.f.sid, 'review-control')
        changed=[]
        def callback():
            check()
            if changed:
                return
            changed.append(True)
            if replacement == 'record':
                record=GoalRecord.create(session_id=g.x.f.sid, objective='replacement')
                g.manager._store.save(record)
                g.manager._execution_origin=(record.session_id,record.goal_id,record.revision,g.pending._origin)
            else:
                from dataclasses import replace
                owned=g.agent._active_interaction_round
                owned.work=replace(owned.work).with_execution_origin(g.pending._origin)
        with pytest.raises(PermissionError):
            capture_native_goal_control(g.x.f.c.native, source=admission.source,
                request_id='review-control', check_current=callback)
        yield None
    with authenticated_scope(g.x.second):
        result = [v async for v in g.x.c.run_stream(g.x.f.sid, 'review-control', SessionWorkKind.GOAL_CONTROL,
            body, parent_execution_id=g.x.saved['owner'].execution_id)]
        assert result == [None]
