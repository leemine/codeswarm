"""Goal controls during actual assessment await retain one attempt settlement."""
import asyncio
from types import SimpleNamespace

import pytest
from openjiuwen.harness.goal import GoalStatus

from tests.unit_tests.runtime.harness.test_external_goal_runtime import (
    chain as external_chain,
    request,
    run,
)

chain = external_chain


@pytest.mark.parametrize("assessment_status,expected", [
    ("continue", GoalStatus.PAUSED),
    ("complete", GoalStatus.COMPLETED),
    ("blocked", GoalStatus.BLOCKED),
])
async def test_pause_during_assessment_allows_current_settlement(chain, assessment_status, expected):
    entered, release = asyncio.Event(), asyncio.Event()

    class Model:
        async def invoke(self, _messages, **_kwargs):
            entered.set()
            await release.wait()
            return SimpleNamespace(
                content='{"status":"'+assessment_status+'","evidence":"verified"}',
                usage_metadata={"input_tokens": 3, "output_tokens": 1},
            )

    chain.adapter.set_goal_assessor_factory(lambda: Model())
    task = asyncio.create_task(run(chain, request()))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        goal_runtime = chain.adapter._goal_runtime
        before = goal_runtime.manager.peek()
        paused = await goal_runtime.control({"action": "pause"})
        assert paused["goal"]["status"] == "paused"
        assert paused["goal"]["revision"] == before.revision
        release.set()
        chunks = await asyncio.wait_for(task, 5)
        settled = goal_runtime.manager.peek()
        assert settled.status is expected
        assert settled.attempt_count == settled.last_assessed_attempt == 1
        assert settled.token_usage.total_tokens == 16
        assert len(chain.providers[0].sent) == 1
        assert len([chunk for chunk in chunks if chunk.is_complete]) == 1
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_combined_provider_and_assessor_usage_stops_at_budget(chain):
    req = request(max_attempts=5)
    req.params["token_budget"] = 16
    chunks = await run(chain, req)
    goal = chain.adapter._goal_runtime.manager.peek()
    assert goal.status is GoalStatus.BLOCKED
    assert goal.token_usage.total_tokens == 16
    assert goal.attempt_count == goal.last_assessed_attempt == 1
    assert len(chain.providers[0].sent) == len(chain.assessments) == 1
    assert [chunk.runtime_completion for chunk in chunks if chunk.is_complete] == ["failed"]


async def test_assessor_swallowed_cancel_accounts_cost_without_completion(chain):
    entered = asyncio.Event()

    class Model:
        async def invoke(self, _messages, **_kwargs):
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                return SimpleNamespace(
                    content='{"status":"complete","evidence":"late response"}',
                    usage_metadata={"input_tokens": 3, "output_tokens": 1},
                )

    chain.adapter.set_goal_assessor_factory(lambda: Model())
    task = asyncio.create_task(run(chain, request()))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        record = chain.adapter._goal_runtime.manager.peek()
        assert record.status is GoalStatus.PAUSED
        assert record.token_usage.total_tokens == 16
        assert record.attempt_count == 1 and record.last_assessed_attempt == 0
        assert len(chain.providers[0].sent) == 1
        assert chain.adapter._goal_runtime.owner is None
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_rejected_replacement_does_not_change_running_owner_assessor(chain):
    calls = []

    class Model:
        def __init__(self, name):
            self.name = name

        async def invoke(self, _messages, **_kwargs):
            calls.append(self.name)
            return SimpleNamespace(
                content='{"status":"complete","evidence":"verified"}',
                usage_metadata={"input_tokens": 3, "output_tokens": 1},
            )

    original = request("original", objective="original objective")
    replacement = request("replacement", objective="unconfirmed replacement")
    chain.adapter.set_goal_assessor_factory(lambda: Model("original"), request=original)
    provider = chain.providers[0]
    provider.release.clear()
    task = asyncio.create_task(run(chain, original))
    try:
        await asyncio.wait_for(provider.entered.wait(), 5)
        chain.adapter.set_goal_assessor_factory(lambda: Model("replacement"), request=replacement)
        response = await asyncio.wait_for(run(chain, replacement), 5)
        assert any(c.payload.get("event_type") == "goal.confirm_required" for c in response if c.payload)
        provider.release.set()
        await asyncio.wait_for(task, 5)
        assert calls == ["original"]
        assert chain.adapter._goal_runtime.manager.peek().objective == "original objective"
    finally:
        provider.release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
