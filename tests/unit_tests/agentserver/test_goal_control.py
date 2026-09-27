"""Compatibility boundaries for shared Goal control and Native delegation."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call

import pytest
from openjiuwen.harness.goal.schema import GoalOperationError, GoalRecord, GoalStatus

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.runtime.agent_adapter import goal_control
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
    JiuWenSwarmDeepAdapter,
)


def _record(status=GoalStatus.ACTIVE):
    record = GoalRecord.create(session_id="session-1", objective="existing objective")
    record.status = status
    return record


@pytest.mark.asyncio
async def test_missing_manager_preserves_public_error_code():
    result = await goal_control.dispatch_goal_control(None, action=" GET ")
    assert result == {
        "result_type": "goal_error",
        "action": "get",
        "error_code": "goal_manager_not_started",
        "error": "goal manager is not started",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("has_peek", [False, True])
async def test_get_uses_lock_free_peek_when_available(has_peek):
    record = _record()
    manager = SimpleNamespace(get=AsyncMock(return_value=record))
    if has_peek:
        manager.peek = Mock(return_value=record)
    result = await goal_control.dispatch_goal_control(manager, action="get")
    assert result["goal"] == record.to_dict()
    if has_peek:
        manager.peek.assert_called_once_with()
        manager.get.assert_not_awaited()
    else:
        manager.get.assert_awaited_once_with()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", list(GoalStatus))
async def test_pause_calls_manager_even_for_inactive_goal_and_samples_before_status(
    status,
):
    record = _record(status)
    calls = []

    async def get():
        calls.append("get")
        return record

    async def pause():
        calls.append("pause")
        record.status = GoalStatus.PAUSED
        return record

    result = await goal_control.dispatch_goal_control(
        SimpleNamespace(get=get, pause=pause), action="pause"
    )
    assert calls == ["get", "pause"]
    assert result["goal"]["status"] == GoalStatus.PAUSED.value
    if status is GoalStatus.ACTIVE:
        assert result["output"] == "Goal paused."
    else:
        assert result["error_code"] == "invalid_state"
        assert status.value in result["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", list(GoalStatus))
async def test_resume_only_invokes_manager_for_paused_or_blocked(status):
    record = _record(status)
    manager = Mock()
    manager.get = AsyncMock(return_value=record)
    manager.resume = AsyncMock(return_value=_record())
    result = await goal_control.dispatch_goal_control(manager, action="resume")
    expected = [call.get()]
    if status in (GoalStatus.PAUSED, GoalStatus.BLOCKED):
        expected.append(call.resume())
        assert result["result_type"] == "goal_stream"
    elif status is GoalStatus.ACTIVE:
        assert result["output"] == "Goal already active."
    else:
        assert result["error_code"] == "invalid_state"
    assert manager.mock_calls == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["pause", "resume", "clear"])
async def test_missing_goal_does_not_invoke_pause_or_resume(action):
    manager = SimpleNamespace(
        get=AsyncMock(return_value=None),
        pause=AsyncMock(),
        resume=AsyncMock(),
        clear=AsyncMock(return_value=None),
    )
    result = await goal_control.dispatch_goal_control(manager, action=action)
    assert result["error_code"] == "no_goal"
    manager.pause.assert_not_awaited()
    manager.resume.assert_not_awaited()


@pytest.mark.asyncio
async def test_clear_preserves_removed_payload_without_a_current_goal():
    removed = _record()
    result = await goal_control.dispatch_goal_control(
        SimpleNamespace(clear=AsyncMock(return_value=removed)), action="clear"
    )
    assert result["goal"] is None
    assert result["cleared_goal"] == removed.to_dict()


@pytest.mark.asyncio
async def test_set_forwards_unary_values_and_only_translates_goal_operation_errors():
    manager = SimpleNamespace(set=AsyncMock(return_value=_record()))
    result = await goal_control.dispatch_goal_control(
        manager,
        action=" SET ",
        objective=None,
        overwrite_confirmed=True,
        token_budget=" 3 ",
        max_attempts=True,
    )
    manager.set.assert_awaited_once_with(
        "", overwrite_confirmed=True, token_budget=" 3 ", max_attempts=True
    )
    assert result["result_type"] == "goal_stream"
    for code in ("already_exists", "invalid_objective"):
        manager.set.side_effect = GoalOperationError(
            operation="set", code=code, message="controlled error", goal=_record()
        )
        result = await goal_control.dispatch_goal_control(
            manager, action="set", objective="new objective"
        )
        assert result["error_code"] == code
        if code == "already_exists":
            assert result["result_type"] == "goal_confirm_required"
            assert result["requested_objective"] == "new objective"
            assert result["existing_goal"]["objective"] == "existing objective"
    manager.set.side_effect = RuntimeError("unrelated failure")
    with pytest.raises(RuntimeError, match="unrelated failure"):
        await goal_control.dispatch_goal_control(manager, action="set")


@pytest.mark.parametrize(
    "value,converted",
    [
        (None, None),
        (True, None),
        (False, None),
        (3, 3),
        (-2, -2),
        (" 3 ", 3),
        ("-2", -2),
        ("junk", None),
    ],
)
def test_streaming_budgets_convert_while_unary_budgets_remain_raw(value, converted):
    params = {"action": "set", "token_budget": value, "max_attempts": value}
    unary = goal_control.structured_goal_control_kwargs(params)
    stream = goal_control.structured_goal_operation(
        AgentRequest(
            request_id="control-test", req_method=ReqMethod.COMMAND_GOAL, params=params
        )
    )
    assert unary["objective"] is None
    assert stream["objective"] == ""
    for key in ("token_budget", "max_attempts"):
        assert unary[key] == value
        if converted is None:
            assert key not in stream
        else:
            assert stream[key] == converted


@pytest.mark.asyncio
@pytest.mark.parametrize("params", [None, {}, {"action": None}])
async def test_missing_and_null_action_keep_unary_streaming_distinction(params):
    kwargs = goal_control.structured_goal_control_kwargs(params)
    assert kwargs["action"] == ("None" if params else "get")
    assert (
        goal_control.structured_goal_operation(
            AgentRequest(
                request_id="control-test",
                req_method=ReqMethod.COMMAND_GOAL,
                params=params,
            )
        )
        is None
    )
    if params:
        result = await goal_control.dispatch_goal_control(object(), **kwargs)
        assert result["action"] == "none"
        assert result["error"] == "unsupported goal action: None"


@pytest.mark.parametrize(
    "text,intent",
    [
        ("hello", None),
        (" /goal ", {"action": "get"}),
        ("/goal PAUSE", {"action": "pause"}),
        ("/goal resume", {"action": "resume"}),
        ("/goal clear", {"action": "clear"}),
        ("/goal set", {"action": "set", "objective": ""}),
        ("/goal get", {"action": "set", "objective": "get"}),
        ("/goal stop", {"action": "set", "objective": "stop"}),
        ("/goal set x", {"action": "set", "objective": "x"}),
        ("/goalsomething", {"action": "set", "objective": "something"}),
    ],
)
@pytest.mark.asyncio
async def test_slash_legacy_wrapper_preserves_text_and_private_dispatch(text, intent):
    adapter = JiuWenSwarmDeepAdapter.__new__(JiuWenSwarmDeepAdapter)
    adapter._dispatch_goal_control = AsyncMock(return_value={"sentinel": True})
    assert adapter._parse_goal_slash_intent(text) == intent
    result = await adapter._handle_goal_slash_command(text, session_id="chosen")
    if intent is None:
        assert result is None
        adapter._dispatch_goal_control.assert_not_awaited()
    else:
        adapter._dispatch_goal_control.assert_awaited_once_with(
            **intent, session_id="chosen"
        )
        assert result == {"sentinel": True}


@pytest.mark.parametrize(
    "value,expected", [(True, True), (1, False), ("true", False), (None, False)]
)
def test_attach_requires_literal_true(value, expected):
    assert JiuWenSwarmDeepAdapter._wants_attach_goal({"attach_goal": value}) is expected


@pytest.mark.parametrize(
    "pending,attach,channel,query,expected",
    [
        (None, False, " TUI ", "/goal", True),
        ({}, False, "tui", "/goal", False),
        (None, True, "tui", "/goal", False),
        (None, False, "web", "/goal", False),
        (None, False, "tui", None, False),
    ],
)
def test_slash_gate_retains_channel_and_pending_control_rules(
    pending, attach, channel, query, expected
):
    assert (
        JiuWenSwarmDeepAdapter._should_parse_tui_goal_slash(
            pending_goal_op=pending,
            attach_goal_request=attach,
            channel_id=channel,
            query=query,
        )
        is expected
    )


@pytest.mark.asyncio
async def test_native_structured_wrapper_still_uses_overridable_private_dispatch():
    adapter = JiuWenSwarmDeepAdapter.__new__(JiuWenSwarmDeepAdapter)
    adapter._dispatch_goal_control = AsyncMock(return_value={"sentinel": True})
    result = await adapter.handle_goal_command_structured(None, session_id="chosen")
    adapter._dispatch_goal_control.assert_awaited_once_with(
        action="get",
        objective=None,
        overwrite_confirmed=False,
        token_budget=None,
        max_attempts=None,
        session_id="chosen",
    )
    assert result == {"sentinel": True}


@pytest.mark.asyncio
async def test_native_instance_absence_keeps_none_result():
    adapter = JiuWenSwarmDeepAdapter.__new__(JiuWenSwarmDeepAdapter)
    adapter._is_session_scoped_adapter = True
    adapter._instance = None
    assert await adapter.dispatch_goal_control(action="get") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("fails", [False, True])
async def test_native_pool_delegates_public_entry_and_evicts_even_after_failure(fails):
    adapter = JiuWenSwarmDeepAdapter.__new__(JiuWenSwarmDeepAdapter)
    adapter._is_session_scoped_adapter = False
    calls = Mock()
    child = SimpleNamespace(
        dispatch_goal_control=AsyncMock(
            return_value={"sentinel": True},
            side_effect=RuntimeError("child failure") if fails else None,
        )
    )
    adapter._get_or_create_session_adapter = AsyncMock(return_value=child)
    adapter._evict_idle_session_adapters = AsyncMock()
    calls.attach_mock(adapter._get_or_create_session_adapter, "get_child")
    calls.attach_mock(child.dispatch_goal_control, "dispatch")
    calls.attach_mock(adapter._evict_idle_session_adapters, "evict")
    kwargs = dict(
        action="set",
        objective="x",
        overwrite_confirmed=True,
        token_budget=9,
        max_attempts=2,
        session_id="chosen",
    )
    if fails:
        with pytest.raises(RuntimeError, match="child failure"):
            await adapter.dispatch_goal_control(**kwargs)
    else:
        assert await adapter.dispatch_goal_control(**kwargs) == {"sentinel": True}
    assert calls.mock_calls == [
        call.get_child("chosen"),
        call.dispatch(**kwargs),
        call.evict(),
    ]
