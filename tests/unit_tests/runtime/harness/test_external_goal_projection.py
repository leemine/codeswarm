"""Attempt completion cannot prematurely finish the product Goal stream."""
from unittest.mock import AsyncMock

import pytest
from openjiuwen.core.session.stream.base import OutputSchema
from openjiuwen.harness_protocol import TurnEventKind
from openjiuwen.harness_providers.io_adapter import ProjectedOutput

from jiuwenswarm.runtime.harness.event_projection import ExternalEventProjection


def register(projection, turn, *, goal=True):
    projection.register_turn(
        turn, request_id="root", channel_id="web", mode="agent",
        goal_attempt=goal, goal_id="goal-1" if goal else None,
    )


def text(turn, value, index=1):
    return ProjectedOutput(turn, chunk=OutputSchema(type="llm_output", index=index, payload={"content": value}))


@pytest.mark.asyncio
async def test_two_attempts_have_only_one_root_terminal_and_no_duplicate_final_text():
    projection = ExternalEventProjection("session")
    register(projection, "first")
    assert projection.owned_payload(text("first", "first answer"))["event_type"] == "chat.delta"
    final = ProjectedOutput("first", chunk=OutputSchema(type="answer", index=2, payload={"output": "first answer"}))
    assert projection.owned_payload(final) is None
    assert projection.owned_payload(ProjectedOutput("first", terminal=TurnEventKind.FINISHED)) is None
    assert await projection.finish_goal_turn("first") is None
    assert "first" not in projection._turns
    register(projection, "second")
    assert projection.owned_payload(text("second", "next answer"))["content"] == "next answer"
    assert projection.owned_payload(ProjectedOutput("second", terminal=TurnEventKind.FINISHED)) is None
    terminal = await projection.finish_goal_turn("second", terminal_payload={"event_type": "chat.final", "content": "", "terminal_status": "completed"})
    assert terminal == {"event_type": "chat.final", "content": "", "terminal_status": "completed", "goal_id": "goal-1"}
    assert await projection.finish_goal_turn("second", terminal_payload=terminal) is None
    assert projection._turns == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", [TurnEventKind.FINISHED, TurnEventKind.FAILED, TurnEventKind.ABORTED])
async def test_detached_attempt_terminal_waits_for_owner_decision(terminal):
    projection = ExternalEventProjection("session")
    projection._publish = AsyncMock()
    register(projection, "turn")
    projection.owned_payload(text("turn", "prefix"))
    await projection(ProjectedOutput("turn", terminal=terminal))
    projection._publish.assert_not_awaited()
    assert "turn" in projection._turns
    await projection.finish_goal_turn("turn", terminal_payload={"event_type": "chat.final", "content": "", "terminal_status": "cancelled"})
    payload = projection._publish.call_args.args[1]
    assert payload["content"] == "prefix"
    assert payload["terminal_status"] == "cancelled"
    assert projection._publish.call_args.kwargs["delivery_id"] == "harness:turn:goal-root-terminal"
    await projection(ProjectedOutput("turn", terminal=terminal))
    projection._publish.assert_awaited_once()


@pytest.mark.asyncio
async def test_failed_detached_persistence_retains_goal_terminal_for_same_key_retry():
    projection = ExternalEventProjection("session")
    projection._publish = AsyncMock(side_effect=[RuntimeError("disk"), None])
    register(projection, "turn")
    await projection(ProjectedOutput("turn", terminal=TurnEventKind.FINISHED))
    payload = {"event_type": "chat.final", "content": "done"}
    with pytest.raises(RuntimeError, match="disk"):
        await projection.finish_goal_turn("turn", terminal_payload=payload)
    assert "turn" in projection._turns
    await projection.finish_goal_turn("turn", terminal_payload=payload)
    assert projection._turns == {}
    assert {c.kwargs["delivery_id"] for c in projection._publish.call_args_list} == {"harness:turn:goal-root-terminal"}


@pytest.mark.asyncio
async def test_ordinary_turn_still_finishes_on_provider_terminal():
    projection = ExternalEventProjection("session")
    register(projection, "ordinary", goal=False)
    payload = projection.owned_payload(ProjectedOutput("ordinary", terminal=TurnEventKind.FINISHED))
    assert payload["terminal_status"] == "completed"
    assert "ordinary" not in projection._turns
    await projection.close()


@pytest.mark.asyncio
async def test_detached_owner_release_waits_for_durable_terminal_and_retries_failure():
    import asyncio

    entered, allow = asyncio.Event(), asyncio.Event()
    released = AsyncMock()
    projection = ExternalEventProjection("session", on_detached_terminal=released)
    register(projection, "ordinary", goal=False)

    async def persist(*_args, **_kwargs):
        entered.set()
        await allow.wait()

    projection._publish = persist
    task = asyncio.create_task(projection(ProjectedOutput("ordinary", terminal=TurnEventKind.FINISHED)))
    await entered.wait()
    released.assert_not_awaited()
    allow.set()
    await task
    released.assert_awaited_once_with("ordinary")

    register(projection, "retry", goal=False)
    released.reset_mock()
    projection._publish = AsyncMock(side_effect=[RuntimeError("disk"), None])
    projection._report_delivery_failure = AsyncMock()
    await projection(ProjectedOutput("retry", terminal=TurnEventKind.FINISHED))
    released.assert_not_awaited()
    assert "retry" in projection._turns
    await projection(ProjectedOutput("retry", terminal=TurnEventKind.FINISHED))
    released.assert_awaited_once_with("retry")
    assert "retry" not in projection._turns
    await projection.close()
