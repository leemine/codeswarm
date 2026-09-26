"""Goal history keeps the original wire while using durable delivery keys."""
from unittest.mock import AsyncMock

import pytest
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.server.runtime.agent_adapter import goal_history as module


@pytest.fixture(autouse=True)
def clean_pending():
    module.pending_goal_objective_history.clear()
    yield
    module.pending_goal_objective_history.clear()


@pytest.mark.asyncio
async def test_deferred_objective_waits_for_prior_turn_and_retains_failed_write(monkeypatch):
    writer = AsyncMock(side_effect=[RuntimeError("disk"), None])
    monkeypatch.setattr(module, "_write", writer)
    request = AgentRequest(request_id="request", session_id="s", channel_id="web")
    await module.record_goal_set(request, action="set", result_type="goal_stream", goal_payload={"goal_id": "g", "objective": "objective"}, defer=True)
    writer.assert_not_awaited()
    with pytest.raises(RuntimeError, match="disk"):
        await module.flush_goal_set("s")
    assert "s" in module.pending_goal_objective_history
    await module.flush_goal_set("s")
    assert not module.pending_goal_objective_history
    assert [c.args[1] for c in writer.call_args_list] == ["goal-objective-g", "goal-objective-g"]


@pytest.mark.asyncio
async def test_confirm_and_pause_do_not_write_objective(monkeypatch):
    writer = AsyncMock()
    monkeypatch.setattr(module, "_write", writer)
    request = AgentRequest(request_id="request", session_id="s")
    for action, result in [("set", "goal_confirm_required"), ("pause", "goal_control")]:
        await module.record_goal_set(request, action=action, result_type=result, goal_payload={"goal_id": "g", "objective": "objective"})
    writer.assert_not_awaited()


@pytest.mark.asyncio
async def test_completion_wire_and_delivery_key_are_stable_across_replay(monkeypatch):
    writer = AsyncMock()
    monkeypatch.setattr(module, "_write", writer)
    monkeypatch.setattr(module, "load_history_records", lambda _sid: [])
    kwargs = dict(session_id="s", channel_id="web", channel_metadata={}, mode="agent", goal_payload={"status": "completed", "goal_id": "g", "last_assessment": {"evidence": "verified"}})
    await module.record_goal_completed(**kwargs)
    await module.record_goal_completed(**kwargs)
    first, second = [c.args for c in writer.call_args_list]
    assert first == second
    record, key = first
    assert key == record["request_id"] == record["extra"]["id"] == "goal-completed-g"
    assert record["content"] == 'goal.completed:{"evidence": "verified"}'
    assert record["extra"]["is_goal_completed_message"] is True


@pytest.mark.asyncio
async def test_existing_legacy_completion_card_is_not_written_again(monkeypatch):
    writer = AsyncMock()
    monkeypatch.setattr(module, "_write", writer)
    monkeypatch.setattr(module, "load_history_records", lambda _sid: [{"id": "goal-completed-g"}])
    await module.record_goal_completed(session_id="s", channel_id="web", channel_metadata={}, mode="agent", goal_payload={"status": "completed", "goal_id": "g"})
    writer.assert_not_awaited()
