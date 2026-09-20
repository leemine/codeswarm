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
from openjiuwen.harness.schema.interaction import InputDispatchMode, SendInputRequest
from openjiuwen.harness_protocol import (
    AgentExecutionSpec,
    DeliveryMode,
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
        outputs = execution.io.output_envelopes()
        projected = await asyncio.wait_for(anext(outputs), 3)
        finished = await asyncio.wait_for(anext(outputs), 3)
        assert projected.turn_id == receipt.turn_id
        assert projected.chunk.type == "answer"
        assert finished.turn_id == receipt.turn_id
        assert finished.terminal is TurnEventKind.FINISHED
    finally:
        await execution.stop()
    assert not execution._requests and not execution._turn_requests
    session.post_run.assert_awaited_once()


@pytest.mark.asyncio
async def test_active_turn_exposes_original_request_id_for_detached_cancel(tmp_path):
    gate = asyncio.Event()
    execution, _, _, ctx, terminal, _ = _setup(
        tmp_path, [[_answer()]], gate=gate
    )
    await execution.start(ctx)
    try:
        receipt = await execution.send_request(
            SendInputRequest(request_id="host-request", inputs={"query": "hi"})
        )
        assert execution.request_id_for_turn(receipt.turn_id) == "host-request"
        gate.set()
        await asyncio.wait_for(terminal.wait(), 3)
        assert execution.request_id_for_turn(receipt.turn_id) is None
    finally:
        await execution.stop()


@pytest.mark.asyncio
async def test_turn_output_reader_keeps_one_session_consumer_across_requests(tmp_path):
    execution, _, _, ctx, _, _ = _setup(tmp_path, [[_answer()], [_answer()]])
    detached = AsyncMock()
    await execution.start(ctx)
    execution.enable_turn_outputs(detached_output=detached)
    try:
        for request_id in ("first", "second"):
            receipt = await execution.send_request(
                SendInputRequest(request_id=request_id, inputs={"query": request_id})
            )

            async def collect():
                return [item async for item in execution.turn_outputs(receipt.turn_id)]

            items = await asyncio.wait_for(collect(), 3)
            assert [item.turn_id for item in items] == [receipt.turn_id] * 2
            assert items[0].chunk.type == "answer"
            assert items[1].terminal is TurnEventKind.FINISHED
        detached.assert_not_awaited()
    finally:
        await execution.stop()


@pytest.mark.asyncio
async def test_native_feature_chunks_retain_product_payload_through_turn_route(
    tmp_path, monkeypatch
):
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
        JiuWenSwarmDeepAdapter,
    )

    monkeypatch.setattr(
        JiuWenSwarmDeepAdapter, "_persist_subagent_roster_history", MagicMock()
    )
    chunks = [
        OutputSchema(
            type="tool_result", index=1,
            payload={"tool_result": {
                "tool_call_id": "call-1", "result": {"ok": True},
                "rendered_result": "完成", "raw_output": "raw",
            }},
        ),
        OutputSchema(
            type="subagent_updated", index=2,
            payload={"subagent_updated": {
                "subagent_id": "child-1", "parent_session_id": "s",
                "revision": 2, "status": "running",
            }},
        ),
        OutputSchema(
            type="stage_result", index=3,
            payload={"stage": "review", "status": "success", "task_id": "task-1"},
        ),
    ]
    execution, _, _, ctx, _, _ = _setup(tmp_path, [chunks])
    await execution.start(ctx)
    execution.enable_turn_outputs()
    try:
        receipt = await execution.send_request(
            SendInputRequest(request_id="features", inputs={"query": "run"})
        )
        observed = await asyncio.wait_for(
            _collect_turn(execution, receipt.turn_id), 3
        )
        assert [(item.chunk.type, item.chunk.payload) for item in observed[:-1]] == [
            (chunk.type, chunk.payload) for chunk in chunks
        ]
        projected = [
            JiuWenSwarmDeepAdapter._parse_stream_chunk(item.chunk)
            for item in observed[:-1]
        ]
        assert [item["event_type"] for item in projected] == [
            "chat.tool_result", "chat.subtask_update", "harness.stage_result"
        ]
        assert projected[0]["rendered_result"] == "完成"
        assert projected[1]["task_id"] == "child-1"
        assert projected[2]["task_id"] == "task-1"
        assert observed[-1].terminal is TurnEventKind.FINISHED
    finally:
        await execution.stop()


async def _collect_turn(execution, turn_id):
    return [item async for item in execution.turn_outputs(turn_id)]


@pytest.mark.asyncio
async def test_deep_adapter_dispatch_uses_native_turn_reader_for_two_requests(tmp_path):
    from jiuwenswarm.common.schema.agent import AgentRequest
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
        JiuWenSwarmDeepAdapter,
    )

    execution, agent, _, context, _, _ = _setup(
        tmp_path, [[_answer()], [_answer()]]
    )
    adapter = object.__new__(JiuWenSwarmDeepAdapter)
    adapter._native_execution = execution
    adapter._resolve_input_dispatch_mode = lambda _params: InputDispatchMode.FOLLOW_UP
    adapter._permission_inputs_for_dispatch = lambda _request, inputs, _mode: inputs
    adapter._permission_dispatch = SimpleNamespace(release=lambda _inputs: None)
    await execution.start(context)
    execution.enable_turn_outputs()
    try:
        for request_id in ("first", "second"):
            request = AgentRequest(request_id=request_id, session_id="s")
            stream, dispatched = await adapter._attach_and_send_inputs(
                request, {"query": request_id}, send_without_output=False
            )
            assert stream is not None and not dispatched
            chunks = [chunk async for chunk in stream]
            await stream.close(abort_active_round=False)
            assert len(chunks) == 1 and chunks[0].type == "answer"
        assert agent.send_input.await_count == 2
        assert execution._output_router._mailboxes == {}
    finally:
        await execution.stop()


@pytest.mark.asyncio
async def test_native_product_stream_uses_existing_adapter_projection(
    tmp_path, monkeypatch
):
    from jiuwenswarm.common.schema.agent import AgentRequest
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
        JiuWenSwarmDeepAdapter,
    )

    native_round = [
        OutputSchema(type="llm_output", index=1, payload={"content": "done"}),
        OutputSchema(
            type="tool_result",
            index=2,
            payload={
                "tool_result": {
                    "tool_call_id": "call-1",
                    "tool_name": "read_file",
                    "result": {"ok": True},
                    "rendered_result": "file read",
                }
            },
        ),
        _answer(),
    ]
    execution, agent, _, context, _, _ = _setup(tmp_path, [native_round, native_round])
    adapter = JiuWenSwarmDeepAdapter()
    adapter._instance = agent
    adapter._native_execution = execution
    adapter._is_session_scoped_adapter = True
    adapter._parent_session_id = "s"
    monkeypatch.setattr(adapter, "_has_valid_model_config", lambda _model_name="": True)
    monkeypatch.setattr(adapter, "_bind_runtime_cron_context", lambda **_kwargs: None)
    monkeypatch.setattr(adapter, "_reset_runtime_cron_context", lambda _tokens: None)
    monkeypatch.setattr(adapter, "_resolve_model_for_request", lambda _request: None)
    monkeypatch.setattr(
        adapter, "_apply_model_to_react_agent", lambda _model, **_kwargs: None
    )
    monkeypatch.setattr(adapter, "_mark_session_active", lambda _session_id: None)
    monkeypatch.setattr(
        adapter, "_register_session_agent_task", lambda _session_id: None
    )
    monkeypatch.setattr(
        adapter, "_unregister_session_agent_task", lambda _session_id: None
    )
    monkeypatch.setattr(
        adapter, "_unmark_session_active", lambda _session_id, **_kwargs: None
    )
    monkeypatch.setattr(adapter, "_update_runtime_config", AsyncMock())

    await execution.start(context)
    execution.enable_turn_outputs()
    try:
        request = AgentRequest(
            request_id="product-stream",
            channel_id="web",
            session_id="s",
            params={"query": "hi", "mode": "agent"},
            is_stream=True,
        )
        chunks = [
            item
            async for item in adapter.process_message_stream_impl(
                request, {"query": "hi"}
            )
        ]
        events = [item.payload for item in chunks if isinstance(item.payload, dict)]
        assert any(item.get("event_type") == "chat.final" for item in events)
        assert any(
            item.get("event_type") == "chat.tool_result"
            and item.get("rendered_result") == "file read"
            for item in events
        )
        response = await adapter.process_message_impl(
            AgentRequest(
                request_id="product-unary",
                channel_id="web",
                session_id="s",
                params={"query": "again", "mode": "agent"},
            ),
            {"query": "again"},
        )
        assert response.ok is True
        assert response.payload.get("content") == "done"
        assert agent.send_input.await_count == 2
    finally:
        await execution.stop()


@pytest.mark.asyncio
async def test_deep_adapter_reuses_suspended_turn_reader_after_question(tmp_path):
    from jiuwenswarm.common.schema.agent import AgentRequest
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
        JiuWenSwarmDeepAdapter,
    )

    interrupt = OutputSchema(
        type=INTERACTION, index=0, payload={"id": "q", "value": "Continue?"}
    )
    execution, agent, _, context, _, _ = _setup(
        tmp_path, [[interrupt], [_answer()]]
    )
    adapter = object.__new__(JiuWenSwarmDeepAdapter)
    adapter._native_execution = execution
    adapter._resolve_input_dispatch_mode = lambda _params: InputDispatchMode.FOLLOW_UP
    adapter._permission_inputs_for_dispatch = lambda _request, inputs, _mode: inputs
    adapter._permission_dispatch = SimpleNamespace(release=lambda _inputs: None)
    await execution.start(context)
    execution.enable_turn_outputs()
    try:
        question_request = AgentRequest(request_id="first", session_id="s")
        stream, _ = await adapter._attach_and_send_inputs(
            question_request, {"query": "first"}, send_without_output=False
        )
        question_chunks = [chunk async for chunk in stream]
        assert [chunk.type for chunk in question_chunks] == [INTERACTION]
        await stream.close(abort_active_round=False)

        answer = InteractiveInput()
        answer.update("q", "yes")
        answer_request = AgentRequest(request_id="answer", session_id="s")
        resumed, _ = await adapter._attach_and_send_inputs(
            answer_request, {"query": answer}, send_without_output=False
        )
        assert resumed is stream
        answer_chunks = [chunk async for chunk in resumed]
        assert [chunk.type for chunk in answer_chunks] == ["answer"]
        await resumed.close(abort_active_round=False)
        assert agent.send_input.await_count == 2
        assert execution._output_router._mailboxes == {}
        with pytest.raises(RuntimeError, match="no longer pending"):
            await adapter._attach_and_send_inputs(
                answer_request, {"query": answer}, send_without_output=False
            )
    finally:
        await execution.stop()


@pytest.mark.asyncio
async def test_closed_turn_reader_does_not_stop_next_turn(tmp_path):
    execution, _, _, ctx, _, _ = _setup(
        tmp_path, [[_answer() for _ in range(8)], [_answer()]]
    )
    await execution.start(ctx)
    execution.enable_turn_outputs(queue_size=1)
    try:
        first = await execution.send_request(
            SendInputRequest(request_id="first", inputs={"query": "first"})
        )
        stream = execution.turn_outputs(first.turn_id)
        assert (await asyncio.wait_for(anext(stream), 3)).chunk.type == "answer"
        await stream.aclose()
        second = await execution.send_request(
            SendInputRequest(request_id="second", inputs={"query": "second"})
        )

        async def collect():
            return [item async for item in execution.turn_outputs(second.turn_id)]

        items = await asyncio.wait_for(collect(), 3)
        assert items[-1].terminal is TurnEventKind.FINISHED
    finally:
        await execution.stop()


@pytest.mark.asyncio
async def test_stopping_session_unblocks_waiting_turn_reader(tmp_path):
    gate = asyncio.Event()
    execution, _, _, ctx, _, _ = _setup(tmp_path, [[_answer()]], gate=gate)
    await execution.start(ctx)
    execution.enable_turn_outputs()
    receipt = await execution.send_request(
        SendInputRequest(request_id="waiting", inputs={"query": "wait"})
    )
    stream = execution.turn_outputs(receipt.turn_id)
    waiting = asyncio.create_task(anext(stream))
    await asyncio.sleep(0)
    stopping = asyncio.create_task(execution.stop())
    with pytest.raises(RuntimeError, match="before a terminal event"):
        await asyncio.wait_for(waiting, 3)
    gate.set()
    await asyncio.wait_for(stopping, 3)
    await stream.aclose()


@pytest.mark.asyncio
async def test_abandon_unopened_turn_reader_preserves_next_request(tmp_path):
    execution, _, _, ctx, _, _ = _setup(
        tmp_path, [[_answer() for _ in range(8)], [_answer()]]
    )
    await execution.start(ctx)
    execution.enable_turn_outputs(queue_size=1)
    try:
        first = await execution.send_request(
            SendInputRequest(request_id="first", inputs={"query": "first"})
        )
        execution.abandon_turn_output(first.turn_id)
        second = await execution.send_request(
            SendInputRequest(request_id="second", inputs={"query": "second"})
        )

        async def collect():
            return [item async for item in execution.turn_outputs(second.turn_id)]

        items = await asyncio.wait_for(collect(), 3)
        assert items[-1].terminal is TurnEventKind.FINISHED
    finally:
        await execution.stop()


@pytest.mark.asyncio
async def test_abandon_drains_queued_and_inflight_output_once(tmp_path):
    execution, _, _, ctx, _, _ = _setup(
        tmp_path, [[_answer() for _ in range(8)]]
    )
    detached = AsyncMock()
    await execution.start(ctx)
    execution.enable_turn_outputs(queue_size=1, detached_output=detached)
    try:
        receipt = await execution.send_request(
            SendInputRequest(request_id="first", inputs={"query": "first"})
        )

        async def mailbox_is_full():
            while execution._output_router._mailboxes[receipt.turn_id].queue.qsize() < 1:
                await asyncio.sleep(0)

        await asyncio.wait_for(mailbox_is_full(), 3)
        execution.abandon_turn_output(receipt.turn_id)

        async def all_detached():
            while not any(
                call.args[0].terminal is TurnEventKind.FINISHED
                for call in detached.await_args_list
            ):
                await asyncio.sleep(0)

        await asyncio.wait_for(all_detached(), 3)
        items = [call.args[0] for call in detached.await_args_list]
        assert len(items) == 9
        assert [item.chunk.type for item in items[:-1]] == ["answer"] * 8
        assert items[-1].terminal is TurnEventKind.FINISHED
    finally:
        await execution.stop()


@pytest.mark.asyncio
async def test_cancelled_reader_recovers_item_taken_during_close():
    from jiuwenswarm.runtime.harness.output_router import _Mailbox, TurnOutputRouter
    from openjiuwen.harness_providers.io_adapter import ProjectedOutput

    mailbox = _Mailbox(asyncio.Queue(maxsize=1))
    reader = asyncio.create_task(TurnOutputRouter._next(mailbox))
    await asyncio.sleep(0)
    item = ProjectedOutput("turn", chunk=_answer())
    mailbox.queue.put_nowait(item)
    mailbox.closed.set()
    reader.cancel()
    with pytest.raises(asyncio.CancelledError):
        await reader
    recovered = list(mailbox.recovered)
    while not mailbox.queue.empty():
        recovered.append(mailbox.queue.get_nowait())
    assert recovered == [item]
    assert mailbox.reader_idle.is_set()


@pytest.mark.asyncio
async def test_steer_does_not_reopen_abandoned_turn_output(tmp_path):
    gate = asyncio.Event()
    execution, _, _, ctx, terminal, _ = _setup(
        tmp_path, [[_answer()]], gate=gate
    )
    await execution.start(ctx)
    execution.enable_turn_outputs(queue_size=1)
    try:
        first = await execution.send_request(
            SendInputRequest(request_id="first", inputs={"query": "first"})
        )

        async def wait_until_running():
            while execution.engine.harness.state is not HarnessState.RUNNING:
                await asyncio.sleep(0)

        await asyncio.wait_for(wait_until_running(), 3)
        execution.abandon_turn_output(first.turn_id)
        steered = await execution.send_request(
            SendInputRequest(
                request_id="steered", inputs={"query": "more"},
                mode=InputDispatchMode.STEER,
            )
        )
        assert steered.turn_id == first.turn_id
        assert steered.accepted_mode is DeliveryMode.STEER
        with pytest.raises(ValueError, match="no registered owner"):
            await anext(execution.turn_outputs(first.turn_id))
        gate.set()
        await asyncio.wait_for(terminal.wait(), 3)
    finally:
        gate.set()
        await execution.stop()


@pytest.mark.asyncio
async def test_idle_goal_uses_same_turn_output_reader(tmp_path):
    async def goal(*, action, **kwargs):
        assert action == "set"
        return {"result_type": "goal_stream", "goal": {"status": "active"}}

    execution, _, _, ctx, _, _ = _setup(
        tmp_path, [[_answer()]], goal=goal
    )
    await execution.start(ctx)
    execution.enable_turn_outputs()
    try:
        receipt, result = await execution.submit_goal("set", objective="finish")
        assert (await asyncio.wait_for(result, 3))["result_type"] == "goal_stream"

        async def collect():
            return [item async for item in execution.turn_outputs(receipt.turn_id)]

        items = await asyncio.wait_for(collect(), 3)
        assert items[0].chunk.type == "answer"
        assert items[-1].terminal is TurnEventKind.FINISHED
    finally:
        await execution.stop()


@pytest.mark.asyncio
async def test_goal_control_without_stream_releases_turn_mailbox(tmp_path):
    async def goal(*, action, **kwargs):
        assert action == "set"
        return {"result_type": "goal_confirm_required"}

    execution, _, _, ctx, terminal, _ = _setup(tmp_path, [[]], goal=goal)
    await execution.start(ctx)
    execution.enable_turn_outputs()
    try:
        receipt, result = await execution.submit_goal("set", objective="replace")
        assert (await asyncio.wait_for(result, 3))["result_type"] == "goal_confirm_required"
        await asyncio.wait_for(terminal.wait(), 3)
        await asyncio.sleep(0)
        with pytest.raises(ValueError, match="no registered owner"):
            await anext(execution.turn_outputs(receipt.turn_id))
    finally:
        await execution.stop()


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
async def test_running_goal_replacement_uses_current_output_turn(tmp_path):
    gate = asyncio.Event()
    calls = []

    async def goal(*, action, **kwargs):
        if action == "get":
            return {"result_type": "goal_control", "goal": {"status": "completed"}}
        calls.append((action, kwargs.get("objective")))
        return {"result_type": "goal_stream", "goal": {"status": "active"}}

    execution, agent, _, ctx, terminal, events = _setup(
        tmp_path, [[_answer()]], goal=goal, gate=gate
    )
    await execution.start(ctx)
    try:
        original, first = await execution.submit_goal("set", objective="first")
        await asyncio.wait_for(first, 3)
        replacement, second = await asyncio.wait_for(
            execution.submit_goal(
                "set", objective="second", overwrite_confirmed=True
            ),
            3,
        )
        await asyncio.wait_for(second, 3)
        assert replacement.accepted_mode is DeliveryMode.STEER
        assert replacement.turn_id == original.turn_id
        assert calls == [("set", "first"), ("set", "second")]
        assert len(execution._requests) == 1
        gate.set()
        await asyncio.wait_for(terminal.wait(), 3)
        assert len({event.turn_id for event in events if event.turn_id}) == 1
        agent.send_input.assert_not_awaited()
    finally:
        gate.set()
        await execution.stop()


@pytest.mark.asyncio
async def test_running_goal_replacement_reattaches_if_output_reaches_eof(tmp_path):
    output_gate = asyncio.Event()
    goal_entered = asyncio.Event()
    goal_gate = asyncio.Event()

    async def goal(*, action, **kwargs):
        if action == "get":
            return {"result_type": "goal_control", "goal": {"status": "active"}}
        goal_entered.set()
        await goal_gate.wait()
        return {"result_type": "goal_stream", "goal": {"status": "active"}}

    execution, agent, _, ctx, _, events = _setup(
        tmp_path, [[_answer()], [_answer()]], goal=goal, gate=output_gate
    )
    await execution.start(ctx)
    try:
        original = await execution.send_request(
            SendInputRequest(request_id="original", inputs={"query": "start"})
        )
        async def wait_for_first_lease():
            while agent.attach_output.await_count == 0:
                await asyncio.sleep(0)
        await asyncio.wait_for(wait_for_first_lease(), 3)
        replacement_task = asyncio.create_task(
            execution.submit_goal("set", objective="replacement")
        )
        await asyncio.wait_for(goal_entered.wait(), 3)
        output_gate.set()
        async def wait_for_first_terminal():
            while not any(
                isinstance(event.event, TurnLifecycleEvent)
                and event.event.kind is TurnEventKind.FINISHED
                for event in events
            ):
                await asyncio.sleep(0)
        await asyncio.wait_for(wait_for_first_terminal(), 3)
        goal_gate.set()
        replacement, result = await asyncio.wait_for(replacement_task, 3)
        assert replacement.turn_id == original.turn_id
        assert (await result)["result_type"] == "goal_stream"
        async def wait_for_new_lease():
            while agent.attach_output.await_count < 2:
                await asyncio.sleep(0)
        await asyncio.wait_for(wait_for_new_lease(), 3)
        async def wait_for_second_turn():
            while len({event.turn_id for event in events if event.turn_id}) < 2:
                await asyncio.sleep(0)
        await asyncio.wait_for(wait_for_second_turn(), 3)
        assert len({event.turn_id for event in events if event.turn_id}) == 2
        agent.send_input.assert_awaited_once()
    finally:
        goal_gate.set()
        output_gate.set()
        await execution.stop()


@pytest.mark.asyncio
async def test_goal_output_after_request_eof_reaches_detached_projection(tmp_path):
    output_gate = asyncio.Event()
    goal_entered = asyncio.Event()
    goal_gate = asyncio.Event()

    async def goal(*, action, **kwargs):
        if action == "get":
            return {"result_type": "goal_control", "goal": {"status": "active"}}
        goal_entered.set()
        await goal_gate.wait()
        return {"result_type": "goal_stream", "goal": {"status": "active"}}

    execution, agent, _, ctx, _, _ = _setup(
        tmp_path, [[_answer()], [_answer()]], goal=goal, gate=output_gate
    )
    detached = AsyncMock()
    await execution.start(ctx)
    execution.enable_turn_outputs(detached_output=detached)
    try:
        original = await execution.send_request(
            SendInputRequest(request_id="original", inputs={"query": "start"})
        )
        async def first_lease_ready():
            while agent.attach_output.await_count == 0:
                await asyncio.sleep(0)

        await asyncio.wait_for(first_lease_ready(), 3)
        replacement_task = asyncio.create_task(
            execution.submit_goal("set", objective="replacement")
        )
        await asyncio.wait_for(goal_entered.wait(), 3)
        output_gate.set()
        async def drain_original():
            return [item async for item in execution.turn_outputs(original.turn_id)]

        await asyncio.wait_for(drain_original(), 3)
        goal_gate.set()
        replacement, result = await asyncio.wait_for(replacement_task, 3)
        assert replacement.turn_id == original.turn_id
        assert (await result)["result_type"] == "goal_stream"

        async def detached_terminal():
            while not any(
                call.args[0].terminal is TurnEventKind.FINISHED
                for call in detached.await_args_list
            ):
                await asyncio.sleep(0)

        await asyncio.wait_for(detached_terminal(), 3)
        items = [call.args[0] for call in detached.await_args_list]
        assert items[0].chunk.type == "answer"
        assert items[0].turn_id != original.turn_id
        assert items[-1].terminal is TurnEventKind.FINISHED
    finally:
        output_gate.set()
        goal_gate.set()
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


def test_started_legacy_session_cannot_be_claimed_by_native_execution(tmp_path):
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
        JiuWenSwarmDeepAdapter,
    )

    adapter = object.__new__(JiuWenSwarmDeepAdapter)
    adapter._instance = SimpleNamespace(_interaction_started=True)
    adapter._is_session_scoped_adapter = True
    adapter._parent_session_id = "s"
    bound = ExecutionBindingStore().bind(
        ExecutionConfigSource(explicit=AgentExecutionSpec("native", "r1")),
        subject_id="alice",
        host_session_id="s",
        workspace=str(tmp_path),
    )
    with pytest.raises(RuntimeError, match="legacy interaction path"):
        adapter.build_native_execution(bound)


@pytest.mark.asyncio
async def test_adapter_selects_one_native_lifecycle_before_legacy_start(
    tmp_path, monkeypatch
):
    from jiuwenswarm.server.runtime.agent_adapter import interface_deep
    from jiuwenswarm.server.runtime.session.kv_cache import kv_cache_application_runtime

    _, agent, session, context, _, _ = _setup(tmp_path, [])
    adapter = object.__new__(interface_deep.JiuWenSwarmDeepAdapter)
    adapter._instance = agent
    adapter._is_session_scoped_adapter = True
    adapter._parent_session_id = "s"
    adapter.install_session_input_guard = AsyncMock()
    adapter._send_input_with_permission_resume_guard = AsyncMock()
    monkeypatch.setattr(interface_deep, "create_agent_session", lambda **_: session)
    monkeypatch.setattr(
        kv_cache_application_runtime, "get_kv_cache_runtime", lambda: None
    )
    source = ExecutionConfigSource(
        explicit=AgentExecutionSpec("native", "lifecycle-r1")
    )
    bindings = ExecutionBindingStore()

    execution = await adapter.start_native_interaction(
        source=source,
        bindings=bindings,
        subject_id="alice",
        workspace=str(tmp_path),
        context=context,
    )
    try:
        agent.start.assert_awaited_once()
        with pytest.raises(RuntimeError, match="already owns"):
            await adapter.start_interaction("s")
        with pytest.raises(RuntimeError, match="already started"):
            await adapter.start_native_interaction(
                source=source,
                bindings=bindings,
                subject_id="alice",
                workspace=str(tmp_path),
                context=context,
            )
    finally:
        await adapter.stop_interaction()
    assert execution is not adapter._native_execution
    agent.stop.assert_awaited_once()
    assert not bindings._bindings


@pytest.mark.asyncio
async def test_first_mcp_child_construction_starts_selected_native_route(
    tmp_path, monkeypatch,
):
    from jiuwenswarm.common.schema.agent import AgentRequest
    from jiuwenswarm.runtime.context import (
        reset_runtime_context,
        set_runtime_context,
    )
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
        JiuWenSwarmDeepAdapter,
    )

    source = ExecutionConfigSource(
        explicit=AgentExecutionSpec("native", "selected-r1")
    )
    bindings = ExecutionBindingStore()
    bound = bindings.bind(
        source, subject_id="alice", host_session_id="s", workspace=str(tmp_path)
    )
    request = AgentRequest(request_id="r", session_id="s")
    request._bound_execution = bound
    request._execution_source = source
    request._execution_bindings = bindings
    parent = JiuWenSwarmDeepAdapter()
    parent.select_execution_for_request(request)
    execution = SimpleNamespace(
        enable_turn_outputs=MagicMock(),
        request_id_for_turn=MagicMock(return_value=None),
    )
    child = SimpleNamespace(
        _instance=SimpleNamespace(card=SimpleNamespace(id="agent")),
        _agent_name="main_agent",
        create_instance=AsyncMock(),
        persist_skill_retrieval_session_profile=MagicMock(),
        restore_skill_retrieval_session=MagicMock(),
        start_native_interaction=AsyncMock(return_value=execution),
        start_interaction=AsyncMock(),
        mark_session_mcp_reconcile_started=MagicMock(),
        _session_selected_mcp=set(),
        _pending_skill_scan_mcp_names=set(),
        register_mcp_by_name=AsyncMock(),
        clear_pending_skill_scan_mcp_names=MagicMock(),
        refresh_skill_rails=AsyncMock(),
    )
    monkeypatch.setattr(parent, "_new_session_scoped_adapter", lambda _sid: child)
    monkeypatch.setattr(parent, "_load_skill_retrieval_session_profile", lambda _sid: None)
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.session_ops_service.warmup_session_context",
        AsyncMock(),
    )
    runtime = object()
    token = set_runtime_context(runtime, object())
    try:
        await parent.reconcile_session_mcp("s", ["filesystem"])
    finally:
        reset_runtime_context(token)
    assert parent._session_adapters["s"] is child
    child.start_native_interaction.assert_awaited_once()
    child.start_interaction.assert_not_awaited()
    execution.enable_turn_outputs.assert_called_once()
    assert callable(execution.enable_turn_outputs.call_args.kwargs["detached_output"])
    assert execution.enable_turn_outputs.call_args.kwargs["detached_output"]._runtime is runtime
    child.register_mcp_by_name.assert_awaited_once_with("filesystem")
    assert child._session_selected_mcp == {"filesystem"}


@pytest.mark.asyncio
async def test_detached_native_question_resumes_without_request_reader():
    from jiuwenswarm.common.schema.agent import AgentRequest
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
        JiuWenSwarmDeepAdapter,
    )

    adapter = object.__new__(JiuWenSwarmDeepAdapter)
    execution = SimpleNamespace(
        _native=SimpleNamespace(active_turn=SimpleNamespace(turn_id="goal-turn")),
        has_turn_output_owner=MagicMock(return_value=False),
        answer_request=AsyncMock(return_value=True),
    )
    adapter._native_execution = execution
    adapter._native_interaction_stream = None
    adapter._resolve_input_dispatch_mode = MagicMock(return_value=None)
    adapter._permission_inputs_for_dispatch = MagicMock(
        side_effect=lambda _request, inputs, _mode: inputs
    )
    answer = InteractiveInput()
    answer.update("question", "continue")
    stream, dispatched = await adapter._attach_and_send_inputs(
        AgentRequest(request_id="answer", session_id="s"),
        {"query": answer},
        send_without_output=False,
    )
    assert stream is None and dispatched is True
    execution.answer_request.assert_awaited_once()


@pytest.mark.asyncio
async def test_session_cleanup_releases_binding_even_before_child_construction(tmp_path):
    from jiuwenswarm.server.runtime.agent_manager import AgentManager

    manager = object.__new__(AgentManager)
    manager.agents = {}
    manager._agent_create_params = {}
    manager.execution_bindings = ExecutionBindingStore()
    manager._session_execution_bindings = {}
    bound = manager.execution_bindings.bind(
        ExecutionConfigSource(explicit=AgentExecutionSpec("native", "r1")),
        subject_id="alice", host_session_id="s", workspace=str(tmp_path),
    )
    manager.remember_execution_binding("web", "s", bound.binding)
    assert await manager.cleanup_session_runtime(channel_id="web", session_id="s") is False
    assert manager.execution_bindings._bindings == {}


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
