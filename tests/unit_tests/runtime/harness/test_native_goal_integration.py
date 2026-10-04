"""Actual Goal execution/clear with synthetic model and tool IO."""
import asyncio

import pytest
from openjiuwen.harness_protocol import TurnEventKind

from jiuwenswarm.governance.tool_context import tool_authority_scope
from jiuwenswarm.runtime.harness.native_goal_control import capture_native_goal_control
from tests.unit_tests.runtime.harness import test_native_goal_source as fixtures

goal_case = fixtures.goal_case


@pytest.mark.asyncio
async def test_original_user_turn_can_set_goal_and_keep_original_model_tool_consumers(goal_case):
    c = goal_case
    original = c.react.invoke
    controls = []
    first = True
    async def user_then_goal(inputs, session=None, **kwargs):
        nonlocal first
        if first:
            first = False
            owned = c.admission.bound[0]
            assert owned._entry.goal is None
            cap = capture_native_goal_control(c.native, source=owned.source,
                request_id='control-request', check_current=c.admission.check)
            await cap.set('goal objective', max_attempts=1)
            controls.append(cap)
            return {'result_type': 'answer', 'output': 'user scheduled goal'}
        return await original(inputs, session=session, **kwargs)
    c.react.invoke = user_then_goal
    with tool_authority_scope(None, provider_authorizers=c.bundle):
        receipt = await c.native.send_request(c.request)
    await asyncio.wait_for(c.admission.bound[0]._entry.terminal_event.wait(), 10)
    assert len(controls) == 1 and c.error is None
    assert c.side_effects == ['goal'] and len(c.http) == 3
    assert len(c.admission.bound) == 1 and c.admission.bound[0].turn_id == receipt.turn_id
    assert c.admission.terminal == [(c.admission.bound[0], TurnEventKind.FINISHED)]
    assert c.outer.goal_manager.peek().status.value == 'completed'
    assert c.native._requests == {}


@pytest.mark.asyncio
@pytest.mark.parametrize('delay_ack', [False,True])
async def test_actual_goal_clear_ack_does_not_require_parent_still_active(goal_case, monkeypatch, delay_ack):
    c=goal_case
    entered=asyncio.Event()
    async def active_model(*_,**__):
        entered.set()
        await asyncio.Event().wait()
    c.outer_before=active_model
    submitted=asyncio.create_task(c.submit())
    try:
        await asyncio.wait_for(entered.wait(),3)
        owned=c.admission.bound[0]
        cap=capture_native_goal_control(c.native, source=c.admission.lifecycle.source,
            request_id='clear',check_current=lambda:None)
        if delay_ack:
            original=c.outer._start_goal_control_exit
            def start(target):
                task=original(target)
                async def delay():
                    await asyncio.shield(task)
                    # Only delay an already-confirmed exit; let the real Native
                    # observer process EOF before delivering the control ACK.
                    await owned._entry.terminal_event.wait()
                return asyncio.create_task(delay())
            monkeypatch.setattr(c.outer,'_start_goal_control_exit',start)
        removed=await asyncio.wait_for(cap.clear(),3)
        assert removed.goal_id==cap.selector.original[1]
        assert c.outer.goal_manager.peek() is None
    finally:
        await asyncio.wait_for(asyncio.gather(submitted,return_exceptions=True),4)
