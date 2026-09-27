# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Original Heartbeat admission around actual External Goal attempts."""

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from openjiuwen.harness.goal import GoalStatus
from openjiuwen.harness_protocol import TurnUsage, UserInputRequest

from jiuwenswarm.common.schema.message import ReqMethod
from tests.unit_tests.agentserver.test_heartbeat_session_runtime import (
    SESSION,
    _external_provider_chain,
    _finish,
    _settle,
    _user_request,
    _user_turn,
    make_chain as _make_chain,
)

make_chain = _make_chain


async def _goal_chain(make_chain, tmp_path, monkeypatch, *, status, ask=False):
    chain = await _external_provider_chain(make_chain, tmp_path, monkeypatch)
    provider = chain.providers[0]
    provider.allow_close.set()
    original = provider._execute_turn
    first = True

    async def execute(turn):
        nonlocal first
        is_goal = first
        first = False
        if is_goal and ask:
            provider.entered.set()
            await provider._request_interaction(
                UserInputRequest(
                    request_id="external-goal-question",
                    prompt="May this Goal continue?",
                    turn_id=turn.turn_id,
                )
            )
        kind, result = await original(turn)
        return kind, replace(
            result, usage=TurnUsage(input_tokens=5, output_tokens=2, total_tokens=7)
        )

    monkeypatch.setattr(provider, "_execute_turn", execute)
    model = SimpleNamespace(
        invoke=AsyncMock(
            return_value=SimpleNamespace(
                content='{"status":"'
                + status
                + '","evidence":"deterministic Goal evidence"}',
                usage_metadata={"input_tokens": 2, "output_tokens": 1},
            )
        )
    )
    chain.adapter.set_goal_assessor_factory(lambda: model)
    chain.assessor = model
    chain.manager.get_agent_for_session_nowait.return_value = chain.facade
    from jiuwenswarm.server.runtime.agent_adapter import goal_model

    monkeypatch.setattr(
        goal_model, "request_goal_assessor_factory", lambda _request: lambda: model
    )
    prepare = chain.runtime._prepare_chat_turn

    async def prepare_bound(request, channel_id, **kwargs):
        prepared = await prepare(request, channel_id, **kwargs)
        request._execution_route = chain.adapter.route
        request._bound_execution = chain.adapter.route.bound
        return prepared

    chain.runtime._prepare_chat_turn = prepare_bound
    return chain


async def _run_goal(chain):
    request = _user_request(
        chain, objective="Finish the retained objective", action="set", max_attempts=3
    )
    request.request_id = "external-goal"
    request.req_method = ReqMethod.COMMAND_GOAL
    return [event async for event in chain.runtime.stream(request, trigger_hook=False)]


async def _assert_due_heartbeat_deferred(chain):
    await chain.heartbeat.scheduler._tick_once()
    job = await _settle(chain)
    assert job.run_count == 0
    assert job.next_run_at == 1000.0
    assert job.run_state.current_run_id is None
    assert len(chain.providers[0].sent) == 1
    assert not await chain.heartbeat.admission.try_begin_heartbeat(
        SESSION, "racing-due-run"
    )


async def _assert_heartbeat_runs_without_resuming_goal(chain):
    before = chain.adapter._goal_runtime.manager.peek().to_dict()
    await chain.heartbeat.scheduler._tick_once()
    job = await _settle(chain)
    assert job.run_count == 1
    assert job.run_state.last_run_status == "succeeded"
    assert len(chain.providers) == 1
    assert len(chain.providers[0].sent) == 2
    assert chain.adapter._goal_runtime.manager.peek().to_dict() == before
    assert chain.assessor.invoke.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stopped", [GoalStatus.PAUSED, GoalStatus.BLOCKED, GoalStatus.COMPLETED]
)
async def test_due_heartbeat_waits_for_actual_goal_and_preserves_stopped_record(
    make_chain, tmp_path, monkeypatch, stopped
):
    assessment = (
        "continue"
        if stopped is GoalStatus.PAUSED
        else "blocked"
        if stopped is GoalStatus.BLOCKED
        else "complete"
    )
    chain = await _goal_chain(make_chain, tmp_path, monkeypatch, status=assessment)
    provider = chain.providers[0]
    task = asyncio.create_task(_run_goal(chain))
    try:
        await asyncio.wait_for(provider.entered.wait(), 3)
        goal = chain.adapter._goal_runtime.manager.peek()
        assert goal.status is GoalStatus.ACTIVE and goal.attempt_count == 1
        await _assert_due_heartbeat_deferred(chain)
        if stopped is GoalStatus.PAUSED:
            await chain.adapter._goal_runtime.manager.pause()
        provider.release.set()
        await asyncio.wait_for(task, 5)
        goal = chain.adapter._goal_runtime.manager.peek()
        assert goal.status is stopped
        assert goal.attempt_count == goal.last_assessed_attempt == 1
        assert goal.token_usage.total_tokens == 10
        await _assert_heartbeat_runs_without_resuming_goal(chain)
    finally:
        provider.release.set()
        provider.allow_close.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await _finish(chain)
        await chain.facade.cleanup()


@pytest.mark.asyncio
async def test_due_heartbeat_waits_through_paused_goal_question_then_runs_after_answer(
    make_chain, tmp_path, monkeypatch
):
    chain = await _goal_chain(
        make_chain, tmp_path, monkeypatch, status="continue", ask=True
    )
    provider = chain.providers[0]
    task = asyncio.create_task(_run_goal(chain))
    try:
        async with asyncio.timeout(3):
            while not (
                chain.adapter.execution_session.io.has_pending_interrupt()
                and chain.coordinator.has_control_target(
                    SESSION, "external-goal-question"
                )
            ):
                assert not task.done()
                await asyncio.sleep(0)
        await _assert_due_heartbeat_deferred(chain)
        await chain.adapter._goal_runtime.manager.pause()
        await _assert_due_heartbeat_deferred(chain)
        assert not task.done()
        provider.release.set()
        await asyncio.wait_for(
            _user_turn(
                chain,
                query="",
                request_id="external-goal-question",
                source="ask_user_interrupt",
                answers=[
                    {"question": "May this Goal continue?", "custom_input": "yes"}
                ],
            ),
            5,
        )
        await asyncio.wait_for(task, 5)
        assert not chain.adapter.execution_session.io.has_pending_interrupt()
        assert chain.adapter._goal_runtime.manager.peek().status is GoalStatus.PAUSED
        await _assert_heartbeat_runs_without_resuming_goal(chain)
    finally:
        provider.release.set()
        provider.allow_close.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await _finish(chain)
        await chain.facade.cleanup()
