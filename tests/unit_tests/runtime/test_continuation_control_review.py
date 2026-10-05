"""Real Native IO and original Runtime admission; all model output is synthetic."""

import asyncio
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from tests.unit_tests.runtime import test_continuation_control as controls
from tests.unit_tests.runtime import test_continuation_execution as execution_tests
from tests.unit_tests.runtime.harness.test_native_session import _answer
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.runtime.continuation_execution import capture_continuation_execution
from jiuwenswarm.runtime.continuation_control import (
    capture_continuation_control,
    continuation_control,
)
from jiuwenswarm.runtime.session import SessionWorkKind
from openjiuwen.core.common.constants.constant import INTERACTION
from openjiuwen.core.session.stream.base import OutputSchema
from openjiuwen.core.session.interaction.interactive_input import InteractiveInput
from openjiuwen.harness.schema.interaction import SendInputRequest

from unittest.mock import AsyncMock, MagicMock
from jiuwenswarm.runtime.harness.binding_store import ExecutionBindingStore
from jiuwenswarm.runtime.harness.config_source import ExecutionConfigSource
from jiuwenswarm.runtime.harness.native_session import NativeExecutionSession
from openjiuwen.harness.deep_agent import DeepAgent
from openjiuwen.harness_protocol import (
    AgentExecutionSpec,
    HarnessContext,
    TurnLifecycleEvent,
    TurnEventKind,
)
from tests.unit_tests.runtime.harness.test_native_session import _Stream


def _setup_actual(tmp_path, rounds, *, sid, goal=None, gate=None, source=None):
    agent = MagicMock(spec=DeepAgent)
    agent.card = SimpleNamespace(id="a")
    agent.ensure_initialized = AsyncMock()
    agent.start = AsyncMock()
    agent.stop = AsyncMock()
    agent.send_input = AsyncMock()
    agent.cancel_round = AsyncMock()
    agent.attach_output = AsyncMock(side_effect=[_Stream(c, gate) for c in rounds])
    session = SimpleNamespace(
        get_session_id=lambda: sid, pre_run=AsyncMock(), post_run=AsyncMock()
    )
    bound = ExecutionBindingStore().bind(
        source or ExecutionConfigSource(explicit=AgentExecutionSpec("native", "r1")),
        subject_id="alice",
        host_session_id=sid,
        workspace=str(tmp_path),
    )
    context = HarnessContext(
        agent_name="native",
        agent_id="a",
        host_session_id=sid,
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


setup, transaction = controls.setup, controls.transaction


@pytest.mark.asyncio
async def test_real_native_three_detached_approvals_keep_original_request(transaction):
    a = await controls.arranged(transaction)
    interrupts = [
        OutputSchema(
            type=INTERACTION,
            index=i,
            payload={"id": f"question-{i + 1}", "value": "Continue?"},
        )
        for i in range(3)
    ]
    native, agent, session, ctx, terminal, events = _setup_actual(
        transaction.setup.tmp_path,
        [[item] for item in interrupts] + [[_answer()]],
        sid=a.sid,
    )
    await native.start(ctx)
    await native.send_request(
        SendInputRequest(request_id=a.original.request_id, inputs={"query": "hi"})
    )
    a.child._native_execution = native
    a.adapter._native_session_routes[a.sid] = (
        None,
        None,
        SimpleNamespace(binding=native.engine.binding),
        a.context,
    )
    outputs = native.io.outputs()
    try:
        for i in range(3):
            prompt = await asyncio.wait_for(anext(outputs), 3)
            assert prompt.payload.id == f"question-{i + 1}"
            await a.tx.runtime.observe_detached_native_turn(
                a.sid,
                a.tx.owned_execution.execution_id,
                {
                    "event_type": "chat.ask_user_question",
                    "request_id": prompt.payload.id,
                },
            )
            a.request = AgentRequest(
                request_id=f"answer-{i}",
                session_id=a.sid,
                channel_id="web",
                req_method=ReqMethod.CHAT_SEND,
                params={
                    "source": "permission_interrupt",
                    "request_id": prompt.payload.id,
                },
            )

            async def operation():
                a.request._continuation_control = capture_continuation_control(
                    a.tx.runtime, a.request, a.facade
                )
                continuation_control(a.request, child=a.child)
                query = InteractiveInput()
                query.update(prompt.payload.id, "yes")
                assert await native.answer_request(
                    SendInputRequest(
                        request_id=a.request.request_id, inputs={"query": query}
                    )
                )
                assert (
                    native.request_id_for_turn(native._native.active_turn.turn_id)
                    == a.original.request_id
                )
                return []

            await a.coordinator.deliver_control(a.sid, prompt.payload.id, operation)
            if i < 2:
                a.policy.check()
        await asyncio.wait_for(terminal.wait(), 3)
        assert agent.send_input.await_count == 4
    finally:
        await native.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize("same_batch", [False, True])
async def test_real_native_normal_three_approvals_keep_original_context(
    transaction, streamed, same_batch
):
    tx = transaction
    result = await execution_tests.transactions.create(tx)
    original = SimpleNamespace(
        session_id=result.session_id, request_id="normal-original", params={}
    )
    state = {}
    interrupts = [
        OutputSchema(
            type=INTERACTION, index=i, payload={"id": f"q{i + 1}", "value": "Continue?"}
        )
        for i in range(3)
    ]
    native, agent, _, ctx, terminal, _ = _setup_actual(
        tx.setup.tmp_path,
        ([interrupts] if same_batch else [[item] for item in interrupts])
        + [[_answer()]],
        sid=result.session_id,
    )
    await native.start(ctx)
    outputs = native.io.outputs()
    coordinator = tx.runtime._session_coordinator

    def question(prompt):
        return SimpleNamespace(
            event_type="chat.ask_user_question",
            payload={"request_id": prompt.payload.id},
        )

    async def initial():
        state["policy"] = capture_continuation_execution(tx.runtime, original)
        state["context"] = await state["policy"].make_context()
        await native.send_request(
            SendInputRequest(request_id=original.request_id, inputs={"query": "hi"})
        )
        yield question(await asyncio.wait_for(anext(outputs), 3))

    try:
        assert (
            len(
                [
                    e
                    async for e in coordinator.run_stream(
                        result.session_id,
                        original.request_id,
                        SessionWorkKind.CHAT_STREAM,
                        initial,
                        suspension_key=tx.runtime._waiting_control_id,
                    )
                ]
            )
            == 1
        )
        child = SimpleNamespace(_native_execution=native)
        adapter = SimpleNamespace(
            _native_session_routes={
                result.session_id: (
                    None,
                    None,
                    SimpleNamespace(binding=native.engine.binding),
                    state["context"],
                )
            },
            _get_cached_session_adapter=Mock(return_value=child),
        )
        facade = SimpleNamespace(_adapter=adapter)
        tx.manager.get_agent_for_session_nowait = Mock(return_value=facade)
        for i in range(3):
            qid = f"q{i + 1}"
            request = AgentRequest(
                request_id=f"answer-{i}",
                session_id=result.session_id,
                channel_id="web",
                req_method=ReqMethod.CHAT_SEND,
                params={"source": "permission_interrupt", "request_id": qid},
            )

            async def resumed():
                request._continuation_control = capture_continuation_control(
                    tx.runtime, request, facade
                )
                continuation_control(request, child=child)
                query = InteractiveInput()
                query.update(qid, "yes")
                assert await native.answer_request(
                    SendInputRequest(
                        request_id=request.request_id, inputs={"query": query}
                    )
                )
                if i < 2:
                    prompt = await asyncio.wait_for(anext(outputs), 3)
                    assert prompt.payload.id == f"q{i + 2}"
                    assert (
                        native.request_id_for_turn(native._native.active_turn.turn_id)
                        == original.request_id
                    )
                    yield question(prompt)
                else:
                    await asyncio.wait_for(terminal.wait(), 3)

            if streamed:
                events = [
                    e
                    async for e in coordinator.deliver_control_stream(
                        result.session_id,
                        qid,
                        resumed,
                        suspension_key=tx.runtime._waiting_control_id,
                    )
                ]
            else:

                async def unary():
                    return [e async for e in resumed()]

                events = await coordinator.deliver_control(
                    result.session_id,
                    qid,
                    unary,
                    suspension_key=tx.runtime._waiting_control_id,
                )
            if i < 2:
                state["policy"].check()
                assert events[0].payload["request_id"] == f"q{i + 2}"
        assert agent.send_input.await_count == (2 if same_batch else 4)
    finally:
        await native.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize("owner_open", [False, True])
@pytest.mark.parametrize("delivery_outcome", ["ack", "raise", "cancel"])
async def test_ack_only_control_preserves_still_running_original(
    transaction, streamed, owner_open, delivery_outcome
):
    tx = transaction
    result = await execution_tests.transactions.create(tx)
    original = SimpleNamespace(
        session_id=result.session_id, request_id="normal-live", params={}
    )
    state = {}
    release_final = asyncio.Event()
    release_owner = asyncio.Event()
    prompt = OutputSchema(
        type=INTERACTION, index=0, payload={"id": "q1", "value": "Continue?"}
    )
    native, agent, _, ctx, terminal, _ = _setup_actual(
        tx.setup.tmp_path, [], sid=result.session_id
    )
    agent.attach_output.side_effect = [
        _Stream([prompt]),
        _Stream([_answer()], release_final),
    ]
    await native.start(ctx)
    outputs = native.io.outputs()
    coordinator = tx.runtime._session_coordinator

    async def initial():
        state["policy"] = capture_continuation_execution(tx.runtime, original)
        state["context"] = await state["policy"].make_context()
        await native.send_request(
            SendInputRequest(request_id=original.request_id, inputs={"query": "hi"})
        )
        got = await asyncio.wait_for(anext(outputs), 3)
        yield SimpleNamespace(
            event_type="chat.ask_user_question", payload={"request_id": got.payload.id}
        )
        if owner_open:
            await release_owner.wait()

    owner = coordinator.run_stream(
        result.session_id,
        original.request_id,
        SessionWorkKind.CHAT_STREAM,
        initial,
        suspension_key=tx.runtime._waiting_control_id,
    )
    try:
        assert (await asyncio.wait_for(anext(owner), 3)).payload["request_id"] == "q1"
        if not owner_open:
            with pytest.raises(StopAsyncIteration):
                await asyncio.wait_for(anext(owner), 3)
        child = SimpleNamespace(_native_execution=native)
        adapter = SimpleNamespace(
            _native_session_routes={
                result.session_id: (
                    None,
                    None,
                    SimpleNamespace(binding=native.engine.binding),
                    state["context"],
                )
            },
            _get_cached_session_adapter=Mock(return_value=child),
        )
        facade = SimpleNamespace(_adapter=adapter)
        tx.manager.get_agent_for_session_nowait = Mock(return_value=facade)
        request = AgentRequest(
            request_id="ack-only",
            session_id=result.session_id,
            channel_id="web",
            req_method=ReqMethod.CHAT_SEND,
            params={"source": "permission_interrupt", "request_id": "q1"},
        )

        async def resumed():
            request._continuation_control = capture_continuation_control(
                tx.runtime, request, facade
            )
            query = InteractiveInput()
            query.update("q1", "yes")
            assert await native.answer_request(
                SendInputRequest(request_id=request.request_id, inputs={"query": query})
            )
            if delivery_outcome != "ack":
                release_final.set()
                await asyncio.wait_for(terminal.wait(), 3)
                if delivery_outcome == "cancel":
                    raise asyncio.CancelledError("consumer cancelled after terminal")
                raise RuntimeError("consumer failed after terminal")
            yield SimpleNamespace(
                event_type="runtime.accepted",
                payload={"request_id": request.request_id},
            )

        async def deliver():
            if streamed:
                events = [
                    e
                    async for e in coordinator.deliver_control_stream(
                        result.session_id,
                        "q1",
                        resumed,
                        suspension_key=tx.runtime._waiting_control_id,
                    )
                ]
            else:

                async def unary():
                    return [e async for e in resumed()]

                events = await coordinator.deliver_control(
                    result.session_id,
                    "q1",
                    unary,
                    suspension_key=tx.runtime._waiting_control_id,
                )
            return events

        if delivery_outcome == "ack":
            events = await deliver()
            assert events[0].event_type == "runtime.accepted"
            assert not terminal.is_set()
            state["policy"].check()
            release_final.set()
            await asyncio.wait_for(terminal.wait(), 3)
        else:
            error = (
                asyncio.CancelledError if delivery_outcome == "cancel" else RuntimeError
            )
            with pytest.raises(error, match="consumer .+ after terminal"):
                await deliver()
        original_handle = [
            item
            for item in coordinator.snapshot_session(result.session_id).executions
            if item.request_id == original.request_id
        ][0]
        assert original_handle.state.terminal
        release_owner.set()
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(anext(owner), 3)
    finally:
        release_final.set()
        release_owner.set()
        await owner.aclose()
        await native.stop()
