# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Native host session and dispatch through real protocol/IO state machinery."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from jiuwenswarm.runtime.harness.binding_store import ExecutionBindingStore
from jiuwenswarm.runtime.harness.config_source import ExecutionConfigSource
from jiuwenswarm.runtime.harness.native_session import NativeExecutionSession
from openjiuwen.core.common.constants.constant import INTERACTION
from openjiuwen.core.session.interaction.interactive_input import InteractiveInput
from openjiuwen.core.session.stream.base import OutputSchema
from openjiuwen.harness.deep_agent import DeepAgent
from openjiuwen.harness.schema.interaction import SendInputRequest
from openjiuwen.harness_protocol import (
    AgentExecutionSpec,
    HarnessContext,
    HarnessState,
    TurnEventKind,
    TurnLifecycleEvent,
)


class _Stream:
    def __init__(self, chunks, gate=None):
        self.chunks = chunks
        self.gate = gate
        self.close = AsyncMock()

    def __aiter__(self):
        return self._iterate()

    async def _iterate(self):
        if self.gate is not None:
            await self.gate.wait()
        for chunk in self.chunks:
            yield chunk


def _setup(tmp_path, rounds, *, goal=None, gate=None):
    agent = MagicMock(spec=DeepAgent)
    agent.card = SimpleNamespace(id="a")
    agent.ensure_initialized = AsyncMock()
    agent.start = AsyncMock()
    agent.stop = AsyncMock()
    agent.send_input = AsyncMock()
    agent.cancel_round = AsyncMock()
    agent.attach_output = AsyncMock(side_effect=[_Stream(c, gate) for c in rounds])
    session = SimpleNamespace(
        get_session_id=lambda: "s", pre_run=AsyncMock(), post_run=AsyncMock()
    )
    bound = ExecutionBindingStore().bind(
        ExecutionConfigSource(explicit=AgentExecutionSpec("native", "r1")),
        subject_id="alice",
        host_session_id="s",
        workspace=str(tmp_path),
    )
    context = HarnessContext(
        agent_name="native",
        agent_id="a",
        host_session_id="s",
        cwd=str(tmp_path),
        system_prompt="",
    )
    terminal = asyncio.Event()
    events = []

    async def observe(event):
        events.append(event)
        if isinstance(event.event, TurnLifecycleEvent) and event.event.kind in {
            TurnEventKind.FINISHED,
            TurnEventKind.ABORTED,
            TurnEventKind.FAILED,
        }:
            terminal.set()

    async def guard(request, *, send):
        await send(request)

    execution = NativeExecutionSession(
        bound,
        agent_factory=lambda ctx: agent,
        session_factory=AsyncMock(return_value=session),
        before_start=AsyncMock(),
        dispatch_guard=guard,
        goal_dispatcher=goal,
        event_observer=observe,
    )
    return execution, agent, session, context, terminal, events


def _answer():
    return OutputSchema(type="answer", index=9, payload={"output": "done"})


@pytest.mark.asyncio
async def test_request_identity_and_host_session_are_preserved(tmp_path):
    execution, agent, session, ctx, terminal, events = _setup(tmp_path, [[_answer()]])
    marker = object()
    original = SendInputRequest(
        request_id="host-id", inputs={"query": "hi", "permission_handoff": marker}
    )
    await execution.start(ctx)
    try:
        receipt = await execution.send_request(original)
        await asyncio.wait_for(terminal.wait(), 3)
        assert agent.send_input.await_args.args[0] is original
        assert agent.start.await_args.kwargs["session"] is session
        assert events[-1].turn_id == receipt.turn_id or events[-1].turn_id is None
    finally:
        await execution.stop()
    assert not execution._requests and not execution._turn_requests
    session.post_run.assert_awaited_once()


@pytest.mark.asyncio
async def test_answer_retains_handoff_and_does_not_duplicate_prompt(tmp_path):
    value = {
        "kind": "ask_user",
        "questions": [{"question": "Continue?"}],
        "revision": 5,
    }
    interrupt = OutputSchema(
        type=INTERACTION, index=0, payload={"id": "q1", "value": value}
    )
    execution, agent, _, ctx, terminal, events = _setup(
        tmp_path, [[interrupt], [_answer()]]
    )
    await execution.start(ctx)
    await execution.send_request(
        SendInputRequest(request_id="initial", inputs={"query": "hi"})
    )
    outputs = execution.io.outputs()
    try:
        prompt = await asyncio.wait_for(anext(outputs), 3)
        assert prompt.payload.value == value
        query = InteractiveInput()
        query.update("q1", "yes")
        marker = object()
        resume = SendInputRequest(
            request_id="resume-id",
            inputs={"query": query, "permission_handoff": marker},
        )
        assert await execution.answer_request(resume)
        assert not await execution.answer_request(resume)
        await asyncio.wait_for(terminal.wait(), 3)
        assert agent.send_input.await_args.args[0] is resume
        assert agent.send_input.await_count == 2
        assert not await execution.answer_request(resume)
        assert len({e.turn_id for e in events if e.turn_id}) == 1
    finally:
        await execution.stop()


@pytest.mark.asyncio
async def test_multiple_questions_resume_once(tmp_path):
    interrupts = [
        OutputSchema(
            type=INTERACTION, index=i, payload={"id": f"q{i}", "value": "Question"}
        )
        for i in range(2)
    ]
    execution, agent, _, ctx, terminal, _ = _setup(tmp_path, [interrupts, [_answer()]])
    await execution.start(ctx)
    await execution.send_request(
        SendInputRequest(request_id="initial", inputs={"query": "hi"})
    )
    outputs = execution.io.outputs()
    try:
        for i in range(2):
            prompt = await asyncio.wait_for(anext(outputs), 3)
            query = InteractiveInput()
            query.update(prompt.payload.id, f"a{i}")
            assert await execution.answer_request(
                SendInputRequest(request_id=f"r{i}", inputs={"query": query})
            )
        await asyncio.wait_for(terminal.wait(), 3)
        assert agent.send_input.await_count == 2
        assert agent.send_input.await_args.args[0].inputs["query"].user_inputs == {
            "q0": "a0",
            "q1": "a1",
        }
    finally:
        await execution.stop()


@pytest.mark.asyncio
async def test_goal_starts_inside_turn_and_pause_can_bypass_running_work(tmp_path):
    gate = asyncio.Event()

    async def goal(*, action, **kwargs):
        if action == "set":
            assert execution.engine.harness.state is HarnessState.RUNNING
            agent.attach_output.assert_awaited_once()
            return {"result_type": "goal_stream", "goal": {"status": "active"}}
        assert action == "pause"
        gate.set()
        return {"result_type": "goal_control", "goal": {"status": "paused"}}

    execution, agent, _, ctx, terminal, events = _setup(
        tmp_path, [[_answer()]], goal=goal, gate=gate
    )
    await execution.start(ctx)
    try:
        receipt, result = await execution.submit_goal("set", objective="work")
        assert (await asyncio.wait_for(result, 3))["goal"]["status"] == "active"
        assert execution.engine.harness.state is HarnessState.RUNNING
        assert (await execution.control_goal("pause"))["goal"]["status"] == "paused"
        await asyncio.wait_for(terminal.wait(), 3)
        agent.send_input.assert_not_awaited()
        assert {e.turn_id for e in events if e.turn_id} == {receipt.turn_id}
    finally:
        await execution.stop()


@pytest.mark.asyncio
async def test_rejected_goal_does_not_hang_on_output(tmp_path):
    goal = AsyncMock(return_value={"result_type": "goal_confirm_required"})
    execution, agent, _, ctx, terminal, _ = _setup(
        tmp_path, [[]], goal=goal, gate=asyncio.Event()
    )
    await execution.start(ctx)
    try:
        _, result = await execution.submit_goal("set", objective="overwrite")
        assert (await asyncio.wait_for(result, 3))[
            "result_type"
        ] == "goal_confirm_required"
        await asyncio.wait_for(terminal.wait(), 3)
        agent.send_input.assert_not_awaited()
    finally:
        await execution.stop()


@pytest.mark.asyncio
async def test_scope_validation_precedes_agent_start(tmp_path):
    execution, agent, _, ctx, _, _ = _setup(tmp_path, [])
    from dataclasses import replace

    with pytest.raises(ValueError):
        await execution.start(replace(ctx, host_session_id="other"))
    with pytest.raises(ValueError):
        await execution.start(replace(ctx, cwd=str(tmp_path / "other")))
    agent.start.assert_not_awaited()


@pytest.mark.asyncio
async def test_dispatch_failure_closes_turn_and_releases_python_references(tmp_path):
    execution, agent, _, ctx, terminal, events = _setup(tmp_path, [[_answer()]])
    execution._dispatch_guard = AsyncMock(
        side_effect=RuntimeError("permission reservation expired")
    )
    await execution.start(ctx)
    try:
        await execution.send_request(
            SendInputRequest(
                request_id="r1", inputs={"query": "hi", "handoff": object()}
            )
        )
        await asyncio.wait_for(terminal.wait(), 3)
        terminal_events = [
            e.event for e in events if isinstance(e.event, TurnLifecycleEvent)
        ]
        assert terminal_events[-1].kind is TurnEventKind.FAILED
        agent.send_input.assert_not_awaited()
        assert not execution._requests
    finally:
        await execution.stop()


@pytest.mark.asyncio
async def test_stop_cancels_queued_goal_without_dispatch(tmp_path):
    gate = asyncio.Event()
    goal = AsyncMock(return_value={"result_type": "goal_stream"})
    execution, agent, _, ctx, _, _ = _setup(
        tmp_path, [[_answer()]], goal=goal, gate=gate
    )
    agent.stop.side_effect = gate.set
    await execution.start(ctx)
    await execution.send_request(
        SendInputRequest(request_id="r1", inputs={"query": "hi"})
    )
    _, result = await execution.submit_goal("set", objective="queued")
    await asyncio.wait_for(execution.stop(), 3)
    assert result.cancelled()
    goal.assert_not_awaited()
    assert not execution._requests


@pytest.mark.asyncio
async def test_existing_adapter_builds_with_product_session_and_permission_guard(
    tmp_path, monkeypatch
):
    from jiuwenswarm.runtime.harness.bridge import prepare_native_session
    from jiuwenswarm.server.runtime.agent_adapter import interface_deep
    from jiuwenswarm.server.runtime.session.kv_cache import kv_cache_application_runtime

    execution, agent, session, ctx, _, _ = _setup(tmp_path, [[_answer()]])
    adapter = object.__new__(interface_deep.JiuWenSwarmDeepAdapter)
    adapter._instance = agent
    adapter._is_session_scoped_adapter = True
    adapter._parent_session_id = "s"
    adapter.install_session_input_guard = AsyncMock()
    adapter._send_input_with_permission_resume_guard = AsyncMock(
        side_effect=lambda request, send: send(request)
    )

    # AsyncMock does not await an awaitable returned by a synchronous side effect.
    async def dispatch(request, *, send):
        await send(request)

    adapter._send_input_with_permission_resume_guard.side_effect = dispatch
    cache = object()
    monkeypatch.setattr(
        kv_cache_application_runtime, "get_kv_cache_runtime", lambda: cache
    )

    def make_session(**kwargs):
        assert kwargs["kv_cache_runtime"] is cache
        assert kwargs["session_id"] == "s"
        return session

    monkeypatch.setattr(interface_deep, "create_agent_session", make_session)
    terminal = asyncio.Event()

    async def observe(event):
        if (
            isinstance(event.event, TurnLifecycleEvent)
            and event.event.kind is TurnEventKind.FINISHED
        ):
            terminal.set()

    execution = prepare_native_session(
        ExecutionConfigSource(explicit=AgentExecutionSpec("native", "r1")),
        bindings=ExecutionBindingStore(),
        subject_id="alice",
        host_session_id="s",
        workspace=str(tmp_path),
        adapter=adapter,
        event_observer=observe,
    )
    await execution.start(ctx)
    try:
        request = SendInputRequest(
            request_id="original", inputs={"query": "hi", "handoff": object()}
        )
        await execution.send_request(request)
        await asyncio.wait_for(terminal.wait(), 3)
        adapter.install_session_input_guard.assert_awaited_once()
        adapter._send_input_with_permission_resume_guard.assert_awaited_once()
        assert agent.send_input.await_args.args[0] is request
    finally:
        await execution.stop()


@pytest.mark.asyncio
async def test_answer_then_abort_does_not_dispatch_continuation(tmp_path):
    interrupt = OutputSchema(
        type=INTERACTION, index=0, payload={"id": "q", "value": "Continue?"}
    )
    execution, agent, _, ctx, terminal, events = _setup(
        tmp_path, [[interrupt], [_answer()]]
    )
    await execution.start(ctx)
    await execution.send_request(
        SendInputRequest(request_id="r1", inputs={"query": "hi"})
    )
    try:
        await asyncio.wait_for(anext(execution.io.outputs()), 3)
        answer = InteractiveInput()
        answer.update("q", "yes")
        assert await execution.answer_request(
            SendInputRequest(request_id="r2", inputs={"query": answer})
        )
        await execution.io.abort(immediate=True)
        await asyncio.wait_for(terminal.wait(), 3)
        agent.send_input.assert_awaited_once()
        lifecycle = [
            e.event.kind for e in events if isinstance(e.event, TurnLifecycleEvent)
        ]
        assert lifecycle == [TurnEventKind.STARTED, TurnEventKind.ABORTED]
    finally:
        await execution.stop()
