# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""External Goal uses the real Manager, Runtime registry and Provider boundary."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from openjiuwen.harness.engine import HarnessEngine
from openjiuwen.harness.goal import GoalStatus
from openjiuwen.harness_protocol import (
    HarnessCapability,
    HarnessCard,
    TurnEventKind,
    TurnResult,
    TurnStatus,
    TurnUsage,
)
from openjiuwen.harness_providers.base import SerializedTurnHarness

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.runtime.context import reset_runtime_context, set_runtime_context
from jiuwenswarm.runtime.harness import bridge, external_goal
from jiuwenswarm.runtime.session.coordinator import RuntimeSessionCoordinator
from jiuwenswarm.runtime.session.model import SessionWorkKind
from jiuwenswarm.server.runtime.agent_adapter.engine_adapter import EngineAgentAdapter
from tests.unit_tests.runtime.harness.test_external_execution_route import _route


class Runtime:
    def __init__(self):
        self.coordinator = RuntimeSessionCoordinator()

    def external_execution_owner(self, session, request):
        return self.coordinator.external_execution_owner(session, request)

    async def acquire_external_execution(self, owner, *, goal):
        await self.coordinator.acquire_external_execution(owner, goal=goal)

    async def request_external_execution_cancel(self, owner):
        await self.coordinator.request_external_execution_cancel(owner)

    def holds_external_execution(self, owner):
        return self.coordinator.holds_external_execution(owner)

    def release_external_execution(self, owner):
        self.coordinator.release_external_execution(owner)

    def owns_heartbeat_execution(self, session, request):
        return False


@pytest.fixture
async def chain(tmp_path, monkeypatch):
    providers = []

    class Provider(SerializedTurnHarness):
        card = HarnessCard(
            name="goal-controlled",
            implementation_version="1",
            capabilities=frozenset({HarnessCapability.NATIVE_TOOLS}),
        )

        def __init__(self):
            super().__init__()
            self.sent = []
            self.entered = asyncio.Event()
            self.release = asyncio.Event()
            self.release.set()
            self.close_entered = asyncio.Event()
            self.close_release = asyncio.Event()
            self.close_release.set()
            self.fail_close = False
            self.closed_count = 0
            self.ask = False
            self.answer = None

        async def _open_session(self, context):
            return "goal-provider"

        async def _execute_turn(self, turn):
            self.sent.append(turn)
            self.entered.set()
            if self.ask:
                from openjiuwen.harness_protocol import UserInputRequest

                self.ask = False
                self.answer = await self._request_interaction(
                    UserInputRequest(
                        request_id="goal-question",
                        prompt="Continue?",
                        turn_id=turn.turn_id,
                    )
                )
            await self.release.wait()
            return TurnEventKind.FINISHED, TurnResult(
                status=TurnStatus.COMPLETED,
                final_output="attempt result",
                usage=TurnUsage(input_tokens=10, output_tokens=2, total_tokens=12),
            )

        async def _close_session(self):
            self.closed_count += 1
            self.close_entered.set()
            await self.close_release.wait()
            if self.fail_close:
                raise RuntimeError("test exit unconfirmed")
            self.release.set()

    def build(spec, *, binding):
        provider = Provider()
        providers.append(provider)
        return HarnessEngine(binding, provider)

    monkeypatch.setattr(bridge, "create_harness_engine", build)
    monkeypatch.setattr(external_goal, "record_goal_set", AsyncMock())
    monkeypatch.setattr(external_goal, "flush_goal_set", AsyncMock())
    monkeypatch.setattr(external_goal, "record_goal_completed", AsyncMock())
    runtime = Runtime()
    await runtime.coordinator.register_session("session-1", "web")
    adapter = EngineAgentAdapter(_route(tmp_path))
    await adapter.create_instance()
    assessments = []

    class Model:
        async def invoke(self, messages, **kwargs):
            assert kwargs.get("tools") == []
            assessments.append(messages)
            status = "continue" if len(assessments) == 1 else "complete"
            return SimpleNamespace(
                content='{"status":"'
                + status
                + '","evidence":"verified","next_instruction":"finish"}',
                usage_metadata={"input_tokens": 3, "output_tokens": 1},
            )

    adapter.set_goal_assessor_factory(lambda: Model())
    value = SimpleNamespace(
        adapter=adapter, runtime=runtime, providers=providers, assessments=assessments
    )
    yield value
    for provider in providers:
        provider.fail_close = False
        provider.release.set()
        provider.close_release.set()
    for handle in runtime.coordinator._registry.select(session_id="session-1"):
        if handle.task is not None and not handle.task.done():
            handle.task.cancel()
    await runtime.coordinator.close()
    await adapter.cleanup()


def request(
    request_id="goal", *, action="set", objective="test objective", max_attempts=3
):
    return AgentRequest(
        request_id=request_id,
        channel_id="web",
        session_id="session-1",
        req_method=ReqMethod.COMMAND_GOAL,
        is_stream=True,
        params={
            "mode": "agent",
            "action": action,
            "objective": objective,
            "max_attempts": max_attempts,
        },
    )


async def run(chain, req, kind=SessionWorkKind.GOAL_STREAM):
    token = set_runtime_context(chain.runtime, None)
    if not hasattr(chain, "chunks"):
        chain.chunks = []

    async def operation():
        async for chunk in chain.adapter.process_message_stream_impl(
            req, {"query": "hello"}
        ):
            chain.chunks.append(chunk.payload)
            yield chunk

    try:
        return [
            chunk
            async for chunk in chain.runtime.coordinator.run_stream(
                req.session_id,
                req.request_id,
                kind,
                operation,
                suspension_key=lambda chunk: (
                    (
                        chunk.payload.get("interaction_id")
                        or chunk.payload.get("request_id")
                    )
                    if chunk.payload.get("event_type")
                    in {"harness.activate_interaction", "chat.ask_user_question"}
                    else None
                ),
            )
        ]
    finally:
        reset_runtime_context(token)


async def test_single_producer_two_attempts_counts_usage_and_one_final(chain):
    chunks = await run(chain, request())
    goal = chain.adapter._goal_runtime.manager.peek()
    assert goal.status is GoalStatus.COMPLETED
    assert goal.attempt_count == goal.last_assessed_attempt == 2
    assert goal.token_usage.total_tokens == 32
    assert len(chain.providers[0].sent) == 2
    assert len([chunk for chunk in chunks if chunk.is_complete]) == 1
    assert len(chain.assessments) == 2
    assert (
        chain.adapter._parent_session is chain.adapter._subagent_runtime.parent_session
    )
    assert chain.adapter._goal_runtime.owner is None


async def test_busy_chat_prevents_goal_begin_then_user_boundary_runs_first(chain):
    provider = chain.providers[0]
    provider.release.clear()
    chat = request("chat")
    chat.req_method = ReqMethod.CHAT_SEND
    chat.params = {"mode": "agent"}
    chat_task = asyncio.create_task(run(chain, chat, SessionWorkKind.CHAT_STREAM))
    await provider.entered.wait()
    goal_task = asyncio.create_task(run(chain, request()))
    for _ in range(20):
        await asyncio.sleep(0)
    assert chain.adapter._goal_runtime.manager.peek().attempt_count == 0
    provider.release.set()
    await chat_task
    await goal_task
    assert chain.adapter._goal_runtime.manager.peek().attempt_count == 2


@pytest.mark.parametrize("usage_known", [True, False])
async def test_goal_disconnect_waits_exit_then_pauses_without_resend(
    chain, monkeypatch, usage_known
):
    provider = chain.providers[0]
    original = provider._execute_turn

    async def execute(turn):
        kind, result = await original(turn)
        if not usage_known:
            result = TurnResult(status=result.status, final_output=result.final_output)
        return kind, result

    monkeypatch.setattr(provider, "_execute_turn", execute)
    provider.release.clear()
    provider.close_release.clear()
    task = asyncio.create_task(run(chain, request()))
    await provider.entered.wait()
    task.cancel()
    await provider.close_entered.wait()
    assert not task.done()
    assert chain.adapter._goal_runtime.owner is not None
    provider.close_release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    goal = chain.adapter._goal_runtime.manager.peek()
    assert goal.status is GoalStatus.PAUSED
    assert goal.attempt_count == 1
    assert goal.last_assessed_attempt == 0
    assert chain.adapter._goal_runtime.owner is None
    assert len(provider.sent) == 1
    marker = chain.adapter._parent_session.get_state("harness.goal.external_attempt")
    result = await chain.adapter._goal_runtime.control({"action": "resume"})
    if usage_known:
        assert marker["safe_boundary"] == "finished"
        assert result["result_type"] == "goal_stream"
    else:
        assert marker["accounting_unknown"] is True
        assert "safe_boundary" not in marker
        assert result["error_code"] == "goal_usage_unavailable"


async def test_unary_set_then_explicit_attach_starts_once(chain):
    result = await chain.adapter.handle_goal_command_structured(
        {"action": "set", "objective": "saved first"},
        "session-1",
    )
    assert result["result_type"] == "goal_stream"
    assert chain.adapter._goal_runtime.manager.peek().attempt_count == 0
    assert chain.providers[0].sent == []
    attach = request("attach")
    attach.req_method = ReqMethod.CHAT_SEND
    attach.params = {"mode": "agent", "attach_goal": True}
    await run(chain, attach, SessionWorkKind.GOAL_ATTACH)
    assert chain.adapter._goal_runtime.manager.peek().status is GoalStatus.COMPLETED
    attach.request_id = "attach-completed"
    await run(chain, attach, SessionWorkKind.GOAL_ATTACH)
    assert len(chain.providers[0].sent) == 2


async def test_pause_while_provider_runs_settles_then_does_not_continue(chain):
    provider = chain.providers[0]
    provider.release.clear()
    task = asyncio.create_task(run(chain, request()))
    await provider.entered.wait()
    paused = await chain.adapter.handle_goal_command_structured(
        {"action": "pause"}, "session-1"
    )
    assert paused["goal"]["status"] == "paused"
    provider.release.set()
    chunks = await task
    goal = chain.adapter._goal_runtime.manager.peek()
    assert goal.status is GoalStatus.PAUSED
    assert goal.attempt_count == goal.last_assessed_attempt == 1
    assert goal.token_usage.total_tokens == 16
    assert chunks[-1].runtime_completion == "cancelled"


async def test_queued_user_wins_before_second_goal_attempt(chain):
    provider = chain.providers[0]
    provider.release.clear()
    task = asyncio.create_task(run(chain, request()))
    await provider.entered.wait()
    chat = request("queued-chat")
    chat.req_method = ReqMethod.CHAT_SEND
    chat.params = {"mode": "agent"}
    user = asyncio.create_task(run(chain, chat, SessionWorkKind.CHAT_STREAM))
    for _ in range(20):
        await asyncio.sleep(0)
    assert len(provider.sent) == 1
    provider.release.set()
    await asyncio.gather(task, user)
    assert len(provider.sent) == 3
    assert "<goal_task>" in provider.sent[0].content.content
    assert "<goal_task>" not in provider.sent[1].content.content
    assert "<goal_task>" in provider.sent[2].content.content


async def test_cancel_failure_keeps_exact_owner_and_retries_only_on_cancel(chain):
    provider = chain.providers[0]
    provider.release.clear()
    provider.fail_close = True
    task = asyncio.create_task(run(chain, request()))
    await provider.entered.wait()
    task.cancel()
    await provider.close_entered.wait()
    for _ in range(30):
        await asyncio.sleep(0)
    assert not task.done()
    old_owner = chain.adapter._goal_runtime.owner
    assert old_owner is not None
    assert provider.closed_count == 1
    chat = request("waiting-user")
    chat.req_method = ReqMethod.CHAT_SEND
    chat.params = {"mode": "agent"}
    user = asyncio.create_task(run(chain, chat, SessionWorkKind.CHAT_STREAM))
    for _ in range(20):
        await asyncio.sleep(0)
    assert len(provider.sent) == 1
    with pytest.raises(RuntimeError, match="cleanup is pending"):
        await user
    provider.fail_close = False
    retry = await chain.runtime.coordinator.cancel_execution(
        "session-1", request_id="goal", wait_timeout=1
    )
    assert not retry.timed_out
    with pytest.raises(asyncio.CancelledError):
        await task
    chat.request_id = "retry-user"
    await run(chain, chat, SessionWorkKind.CHAT_STREAM)
    assert len(chain.providers) == 2
    assert provider.closed_count == 2
    assert chain.adapter._goal_runtime.owner is None
    assert (
        chain.adapter._parent_session is chain.adapter._subagent_runtime.parent_session
    )


async def test_two_overwrites_do_not_claim_each_others_goal(chain):
    provider = chain.providers[0]
    provider.release.clear()
    provider.close_release.clear()
    old = asyncio.create_task(run(chain, request()))
    await provider.entered.wait()
    first_req = request("replace-first", objective="first replacement")
    first_req.params["overwrite_confirmed"] = True
    first = asyncio.create_task(run(chain, first_req))
    await provider.close_entered.wait()
    second_req = request("replace-second", objective="second replacement")
    second_req.params["overwrite_confirmed"] = True
    second = asyncio.create_task(run(chain, second_req))
    provider.close_release.set()
    results = await asyncio.gather(old, first, second, return_exceptions=True)
    assert (
        isinstance(results[0], asyncio.CancelledError)
        or results[0][-1].runtime_completion == "cancelled"
    )
    assert not isinstance(results[1], BaseException)
    assert not isinstance(results[2], BaseException)
    goal = chain.adapter._goal_runtime.manager.peek()
    assert goal.objective == "second replacement"
    assert goal.status is GoalStatus.COMPLETED
    assert len(chain.providers) == 2
    assert all(
        "first replacement" not in turn.content.content
        for turn in chain.providers[1].sent
    )


async def test_cold_unknown_marker_is_not_cleared_or_resent(chain):
    from jiuwenswarm.runtime.harness.external_goal import ExternalGoalRuntime

    goal = chain.adapter._goal_runtime
    await goal.manager.set("prior work")
    chain.adapter._parent_session.update_state(
        {"harness.goal.external_attempt": {"turn_id": "unknown"}}
    )
    restored = ExternalGoalRuntime(
        chain.adapter, chain.adapter._parent_session, goal.scope
    )
    chain.adapter._goal_runtime = restored
    chunks = await run(chain, request(action="resume"))
    assert chunks[0].payload["code"] == "goal_recovery_unconfirmed"
    assert "Delete this Session" in chunks[0].payload["error"]
    assert chain.adapter._parent_session.get_state("harness.goal.external_attempt") == {
        "turn_id": "unknown"
    }
    await restored.control({"action": "clear"})
    result = await restored.control({"action": "set", "objective": "new"})
    assert result["error_code"] == "goal_recovery_unconfirmed"
    assert chain.providers[0].sent == []


async def test_goal_waiting_resume_keeps_locator_and_answer_bypasses_permit(chain):
    from openjiuwen.core.session.interaction.interactive_input import InteractiveInput
    from jiuwenswarm.runtime.session.model import SessionExecutionState

    provider = chain.providers[0]
    provider.ask = True
    task = asyncio.create_task(run(chain, request()))
    for _ in range(100):
        await asyncio.sleep(0)
        handles = chain.runtime.coordinator._registry.select(
            session_id="session-1", request_id="goal"
        )
        if handles and handles[0].waiting_control_id:
            break
    handle = handles[0]
    assert (
        handle.state is SessionExecutionState.RUNNING and handle.waiting_control_id
    ), chain.chunks
    locator = handle.waiting_control_id
    resumed = await run(chain, request("resume-wait", action="resume"))
    assert resumed[0].payload["goal"]["attempt_count"] == 1
    assert handle.waiting_control_id == locator
    assert handle.state is SessionExecutionState.RUNNING
    interactive = InteractiveInput()
    interactive.update("goal-question", "yes")
    req = request("answer")
    token = set_runtime_context(chain.runtime, None)
    try:
        # This is the same adapter seam used by Facade.deliver_control_input.
        chunks = [
            chunk
            async for chunk in chain.adapter.process_message_stream_impl(
                req, {"query": interactive}
            )
        ]
        assert chunks == []
    finally:
        reset_runtime_context(token)
    await task
    assert provider.answer is not None
    assert not chain.adapter.execution_session.io.has_pending_interrupt()
    assert len(provider.sent) == 2


@pytest.mark.parametrize("token_budget", [None, 100])
async def test_missing_provider_usage_with_budget_stops_before_assessor(
    chain, monkeypatch, token_budget
):
    original = chain.providers[0]._execute_turn

    async def missing(turn):
        kind, result = await original(turn)
        return kind, TurnResult(status=result.status, final_output=result.final_output)

    monkeypatch.setattr(chain.providers[0], "_execute_turn", missing)
    req = request()
    if token_budget is not None:
        req.params["token_budget"] = token_budget
    chunks = await run(chain, req)
    goal = chain.adapter._goal_runtime.manager.peek()
    assert goal.status is GoalStatus.BLOCKED
    assert goal.attempt_count == 1
    assert goal.token_usage.total_tokens == 0
    assert "goal_usage_unavailable" in goal.last_assessment.evidence
    assert chain.assessments == []
    assert chunks[-1].runtime_completion == "failed"
    result = await chain.adapter._goal_runtime.control({"action": "resume"})
    assert result["error_code"] == "goal_usage_unavailable"


@pytest.mark.parametrize("usage_known", [True, False])
async def test_provider_aborted_is_accounted_but_not_assessed(
    chain, monkeypatch, usage_known
):
    from openjiuwen.harness_protocol import TurnTermination, TurnTerminationKind

    async def aborted(turn):
        chain.providers[0].sent.append(turn)
        return TurnEventKind.ABORTED, TurnResult(
            status=TurnStatus.INTERRUPTED,
            termination=TurnTermination(TurnTerminationKind.USER_ABORT),
            usage=TurnUsage(input_tokens=10, output_tokens=2, total_tokens=12)
            if usage_known
            else None,
        )

    monkeypatch.setattr(chain.providers[0], "_execute_turn", aborted)
    chunks = await run(chain, request())
    goal = chain.adapter._goal_runtime.manager.peek()
    assert goal.status is GoalStatus.PAUSED
    assert goal.attempt_count == 1 and goal.last_assessed_attempt == 0
    assert goal.token_usage.total_tokens == (12 if usage_known else 0)
    assert chain.assessments == []
    assert chain.providers[0].closed_count == 1
    assert chunks[-1].runtime_completion == "cancelled"
    marker = chain.adapter._parent_session.get_state("harness.goal.external_attempt")
    if usage_known:
        assert marker["safe_boundary"] == "exit_confirmed"
        result = await chain.adapter._goal_runtime.control({"action": "resume"})
        assert result["result_type"] == "goal_stream"
    else:
        assert marker["accounting_unknown"] is True
        assert "safe_boundary" not in marker
        result = await chain.adapter._goal_runtime.control({"action": "resume"})
        assert result["error_code"] == "goal_usage_unavailable"
        restored = external_goal.ExternalGoalRuntime(
            chain.adapter,
            chain.adapter._parent_session,
            chain.adapter._goal_runtime.scope,
        )
        assert (await restored.control({"action": "resume"}))[
            "error_code"
        ] == "goal_usage_unavailable"


async def test_cancel_waiting_goal_clears_original_interaction(chain):
    provider = chain.providers[0]
    provider.ask = True
    task = asyncio.create_task(run(chain, request()))
    for _ in range(100):
        await asyncio.sleep(0)
        if chain.adapter.execution_session.io.has_pending_interrupt():
            break
    old_session = chain.adapter.execution_session
    assert old_session.io.has_pending_interrupt()
    interrupt = request("stop")
    interrupt.params = {"intent": "cancel"}
    result = await chain.adapter.process_interrupt(interrupt)
    assert result.ok
    outcome = await asyncio.gather(task, return_exceptions=True)
    assert (
        isinstance(outcome[0], asyncio.CancelledError)
        or outcome[0][-1].runtime_completion == "cancelled"
    )
    assert not old_session.io.has_pending_interrupt()
    assert old_session.closed
    assert chain.adapter._goal_runtime.manager.peek().attempt_count == 1
    assert chain.adapter._goal_runtime.manager.peek().status is GoalStatus.PAUSED


async def test_tui_goal_uses_raw_request_after_facade_renders_inputs(chain):
    req = request("tui-raw")
    req.channel_id = "tui"
    req.req_method = ReqMethod.CHAT_SEND
    req.params = {"mode": "agent", "query": "/goal set raw objective"}
    token = set_runtime_context(chain.runtime, None)
    try:
        chunks = [
            chunk
            async for chunk in chain.runtime.coordinator.run_stream(
                req.session_id,
                req.request_id,
                SessionWorkKind.GOAL_STREAM,
                lambda: chain.adapter.process_message_stream_impl(
                    req,
                    {
                        "query": 'User request follows: {"content":"/goal set raw objective"}'
                    },
                ),
            )
        ]
    finally:
        reset_runtime_context(token)
    assert chain.adapter._goal_runtime.manager.peek().objective == "raw objective"
    assert chain.adapter._goal_runtime.manager.peek().status is GoalStatus.COMPLETED
    assert chunks[-1].runtime_completion == "completed"
    assert "<goal_task>" in chain.providers[0].sent[0].content.content


async def test_structured_invalid_action_preserves_shared_control_shape(chain):
    result = await chain.adapter.handle_goal_command_structured(
        {"action": None}, "session-1"
    )
    assert result["result_type"] == "goal_error"
    assert result["error_code"] == "invalid_action"


async def test_tui_cross_session_message_is_not_a_goal_command(chain):
    from jiuwenswarm.common.session_message import SESSION_MESSAGE_INTERNAL_KEY

    req = request("mailbox")
    req.channel_id = "tui"
    req.req_method = ReqMethod.CHAT_SEND
    req.params = {
        "mode": "agent",
        "query": "/goal set untrusted message",
        SESSION_MESSAGE_INTERNAL_KEY: {"source_session_id": "elsewhere"},
    }
    await run(chain, req, SessionWorkKind.SESSION_MESSAGE)
    assert chain.adapter._goal_runtime.manager.peek() is None
    assert len(chain.providers[0].sent) == 1


async def test_detached_chat_accepted_before_receipt_blocks_goal_begin(
    chain, monkeypatch
):
    provider = chain.providers[0]
    provider.release.clear()
    accepted = asyncio.Event()
    receipt_gate = asyncio.Event()
    original_send = provider.send
    first = True

    async def delayed_send(content, **kwargs):
        nonlocal first
        receipt = await original_send(content, **kwargs)
        if first:
            first = False
            accepted.set()
            await receipt_gate.wait()
        return receipt

    monkeypatch.setattr(provider, "send", delayed_send)
    chat = request("slow-receipt-chat")
    chat.req_method = ReqMethod.CHAT_SEND
    chat.params = {"mode": "agent"}
    ordinary = asyncio.create_task(run(chain, chat, SessionWorkKind.CHAT_STREAM))
    await accepted.wait()
    await provider.entered.wait()
    ordinary.cancel()
    with pytest.raises(asyncio.CancelledError):
        await ordinary
    assert provider.closed_count == 0
    goal = asyncio.create_task(run(chain, request()))
    for _ in range(30):
        await asyncio.sleep(0)
    assert chain.adapter._goal_runtime.manager.peek().attempt_count == 0
    assert len(provider.sent) == 1
    provider.release.set()
    await goal
    assert chain.adapter._goal_runtime.manager.peek().status is GoalStatus.COMPLETED
    assert len(provider.sent) == 3


async def test_failed_ordinary_turn_requires_explicit_stop_before_goal(
    chain, monkeypatch
):
    from openjiuwen.harness_protocol import TurnError

    provider = chain.providers[0]

    async def failed(turn):
        provider.sent.append(turn)
        return TurnEventKind.FAILED, TurnResult(
            status=TurnStatus.FAILED, error=TurnError(message="controlled failure")
        )

    monkeypatch.setattr(provider, "_execute_turn", failed)
    chat = request("failed-chat")
    chat.req_method = ReqMethod.CHAT_SEND
    chat.params = {"mode": "agent"}
    await run(chain, chat, SessionWorkKind.CHAT_STREAM)
    goal = asyncio.create_task(run(chain, request()))
    for _ in range(30):
        await asyncio.sleep(0)
    assert chain.adapter._goal_runtime.manager.peek().attempt_count == 0
    interrupt = request("explicit-stop")
    interrupt.params = {"intent": "cancel"}
    assert (await chain.adapter.process_interrupt(interrupt)).ok
    await asyncio.gather(goal, return_exceptions=True)
    assert provider.closed_count == 1
    assert chain.adapter._ordinary_owner is None
    resumed = request("resume", action="resume")
    await run(chain, resumed)
    assert len(chain.providers) == 2
    assert chain.adapter._goal_runtime.manager.peek().status is GoalStatus.COMPLETED


@pytest.mark.parametrize("token_budget", [None, 100])
async def test_missing_assessor_usage_blocks_without_assuming_zero(chain, token_budget):
    class Model:
        async def invoke(self, messages, **kwargs):
            return SimpleNamespace(
                content='{"status":"complete","evidence":"done"}', usage_metadata={}
            )

    chain.adapter.set_goal_assessor_factory(lambda: Model())
    req = request()
    if token_budget is not None:
        req.params["token_budget"] = token_budget
    await run(chain, req)
    goal = chain.adapter._goal_runtime.manager.peek()
    assert goal.status is GoalStatus.BLOCKED
    assert goal.attempt_count == 1
    assert goal.token_usage.total_tokens == 12
    assert "ASSESSOR_USAGE_UNAVAILABLE" in goal.last_assessment.evidence

    result = await chain.adapter._goal_runtime.control({"action": "resume"})
    assert result["error_code"] == "goal_usage_unavailable"
    await chain.adapter._goal_runtime.control({"action": "clear"})
    result = await chain.adapter._goal_runtime.control(
        {"action": "set", "objective": "new"}
    )
    assert result["error_code"] == "goal_usage_unavailable"
