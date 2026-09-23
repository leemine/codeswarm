# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
import asyncio
from unittest.mock import AsyncMock

import pytest
from openjiuwen.core.session.stream.base import OutputSchema
from openjiuwen.harness_protocol import DeliveryMode, SendReceipt, TurnEventKind
from openjiuwen.harness_providers.io_adapter import ProjectedOutput
from openjiuwen.harness_providers.output_buffer import (
    OutputLimits,
    OutputBudgetExceeded,
)
from jiuwenswarm.runtime.harness.output_router import TurnOutputRouter


class IO:
    def __init__(self):
        self.queue = asyncio.Queue()
        self.abort = AsyncMock()

    async def output_envelopes(self):
        while True:
            item = await self.queue.get()
            if item is None:
                return
            yield item


def chunk(index, text="value"):
    return ProjectedOutput(
        "turn",
        chunk=OutputSchema(type="llm_output", index=index, payload={"content": text}),
    )


def receipt():
    return SendReceipt(
        message_id="m", turn_id="turn", accepted_mode=DeliveryMode.FOLLOW_UP
    )


@pytest.mark.asyncio
async def test_receipt_race_large_unclaimed_output_spools_then_transfers_without_duplication():
    io = IO()
    router = TurnOutputRouter(
        io, queue_size=1, output_limits=OutputLimits(memory_bytes=64)
    )
    router.start()
    items = [chunk(i, "完整" * 1000) for i in range(20)] + [
        ProjectedOutput("turn", terminal=TurnEventKind.FINISHED)
    ]

    async def send():
        for item in items:
            await io.queue.put(item)
        while io.queue.qsize():
            await asyncio.sleep(0)
        assert router._budget.items == len(items)
        assert router._budget.memory_bytes <= 64
        return receipt()

    try:
        await router.submit(send)
        assert router._mailboxes["turn"].queue.maxsize == 1
        actual = [item async for item in router.outputs("turn")]
        assert actual == items
        assert router._budget.items == router._budget.storage_bytes == 0
    finally:
        await router.stop()


@pytest.mark.asyncio
async def test_slow_owner_does_not_block_terminal_and_handoff_is_ordered_once():
    io = IO()
    detached = AsyncMock()
    router = TurnOutputRouter(io, queue_size=1, detached_output=detached)
    router.start()
    try:
        await router.submit(AsyncMock(return_value=receipt()))
        items = [chunk(i) for i in range(30)] + [
            ProjectedOutput("turn", terminal=TurnEventKind.ABORTED)
        ]
        for item in items:
            await io.queue.put(item)
        while io.queue.qsize():
            await asyncio.sleep(0)
        # Producer already reached its terminal while the owner never read.
        assert router._budget.items == len(items)
        await asyncio.wait_for(io.abort(), 1)
        router.abandon("turn")
        await asyncio.gather(*router._drain_tasks)
        assert [call.args[0] for call in detached.await_args_list] == items
        assert router._budget.items == router._budget.storage_bytes == 0
    finally:
        await router.stop()


@pytest.mark.asyncio
async def test_exhaustion_releases_owner_with_error_and_aborts_without_fake_terminal():
    io = IO()
    detached = AsyncMock()
    router = TurnOutputRouter(
        io, output_limits=OutputLimits(max_items=2), detached_output=detached
    )
    router.start()
    try:
        await router.submit(AsyncMock(return_value=receipt()))
        for i in range(3):
            await io.queue.put(chunk(i))
        await asyncio.wait_for(router._task, 1)
        outputs = router.outputs("turn")
        assert (await anext(outputs)).chunk.index == 0
        assert (await anext(outputs)).chunk.index == 1
        with pytest.raises(OutputBudgetExceeded):
            await anext(outputs)
        io.abort.assert_awaited_once()
        with pytest.raises(OutputBudgetExceeded):
            await router.submit(AsyncMock(return_value=receipt()))
    finally:
        await router.stop()


@pytest.mark.asyncio
async def test_eof_keeps_buffered_real_terminal_for_owner():
    io = IO()
    router = TurnOutputRouter(io)
    router.start()
    try:
        await router.submit(AsyncMock(return_value=receipt()))
        await io.queue.put(chunk(0))
        await io.queue.put(ProjectedOutput("turn", terminal=TurnEventKind.FINISHED))
        await io.queue.put(None)
        await router._task
        items = [item async for item in router.outputs("turn")]
        assert items[-1].terminal is TurnEventKind.FINISHED
    finally:
        await router.stop()


@pytest.mark.asyncio
async def test_output_observer_sees_interaction_and_terminal_once_before_delivery():
    io = IO()
    observed = []

    async def observe(item):
        observed.append(item)

    router = TurnOutputRouter(io, output_observer=observe)
    router.start()
    interaction = ProjectedOutput(
        "turn",
        chunk=OutputSchema(
            type="__interaction__",
            index=0,
            payload={"id": "question-1"},
        ),
    )
    terminal = ProjectedOutput("turn", terminal=TurnEventKind.FINISHED)
    try:
        await router.submit(AsyncMock(return_value=receipt()))
        await io.queue.put(interaction)
        await io.queue.put(terminal)
        actual = [item async for item in router.outputs("turn")]
        assert actual == [interaction, terminal]
        assert observed == [interaction, terminal]
    finally:
        await router.stop()


@pytest.mark.asyncio
async def test_output_observer_failure_aborts_before_product_delivery():
    io = IO()

    async def reject(_item):
        raise OSError("recovery archive unavailable")

    router = TurnOutputRouter(io, output_observer=reject)
    router.start()
    try:
        await router.submit(AsyncMock(return_value=receipt()))
        await io.queue.put(chunk(0))
        await router._task
        io.abort.assert_awaited_once()
        outputs = router.outputs("turn")
        with pytest.raises(OSError, match="recovery archive unavailable"):
            await anext(outputs)
    finally:
        await router.stop()


@pytest.mark.asyncio
async def test_external_large_owned_prefix_survives_detached_terminal_and_cleanup(
    monkeypatch,
):
    from jiuwenswarm.runtime.harness import event_projection as mod

    projection = mod.ExternalEventProjection("session")
    projection.register_turn("turn", request_id="r", channel_id="web", mode="agent")
    publish = AsyncMock()
    monkeypatch.setattr(projection, "_publish", publish)
    text = "完整结果" * 100000
    projection.owned_payload(chunk(0, text))
    state = projection._turns["turn"]
    assert state.text._file._rolled
    await projection(ProjectedOutput("turn", terminal=TurnEventKind.FINISHED))
    assert publish.await_args.args[1]["content"] == text
    assert state.text._file.closed
    assert projection._text_budget.storage_bytes == 0
    await projection.close()


@pytest.mark.asyncio
async def test_native_large_detached_text_preserved_and_budget_released(monkeypatch):
    from types import SimpleNamespace
    from jiuwenswarm.server.runtime.agent_adapter import (
        native_detached_projection as mod,
    )

    projection = mod.NativeDetachedProjection(
        "s", SimpleNamespace(_parse_stream_chunk=lambda *a, **k: None)
    )
    publish = AsyncMock()
    monkeypatch.setattr(projection, "_publish", publish)
    text = "结果" * 100000
    await projection(chunk(0, text))
    state = projection._turns["turn"]
    assert state.text._file._rolled
    await projection(ProjectedOutput("turn", terminal=TurnEventKind.FINISHED))
    assert publish.await_args.args[1]["content"] == text
    assert state.text._file.closed
    assert projection._text_budget.storage_bytes == 0


@pytest.mark.asyncio
async def test_projection_limit_is_explicit_without_success_final(monkeypatch):
    from jiuwenswarm.runtime.harness import event_projection as mod

    projection = mod.ExternalEventProjection("s")
    projection.register_turn("turn", request_id="r", channel_id="web", mode="agent")
    projection._turns["turn"].text.max_bytes = 5
    publish = AsyncMock()
    monkeypatch.setattr(projection, "_publish", publish)
    with pytest.raises(OutputBudgetExceeded):
        await projection(chunk(0, "sixxxx"))
    await projection(ProjectedOutput("turn", terminal=TurnEventKind.FINISHED))
    publish.assert_not_awaited()
    assert projection._text_budget.storage_bytes == 0
    await projection.close()


@pytest.mark.asyncio
async def test_cancelled_ready_reader_leaves_spooled_item_accounted_for_handoff():
    io = IO()
    router = TurnOutputRouter(io, output_limits=OutputLimits(memory_bytes=1))
    router.start()
    try:
        await router.submit(AsyncMock(return_value=receipt()))
        mailbox = router._mailboxes["turn"]
        waiter = asyncio.create_task(router._next(mailbox))
        await asyncio.sleep(0)
        mailbox.queue.put_nowait(chunk(0))
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert mailbox.recovered == __import__("collections").deque()
        assert router._budget.items == 1
        assert mailbox.queue.get_nowait() == chunk(0)
    finally:
        await router.stop()


@pytest.mark.asyncio
async def test_stop_during_receipt_wait_does_not_recreate_spooled_owner():
    from jiuwenswarm.runtime.harness.output_router import TurnOutputIncompleteError
    io = IO()
    detached = AsyncMock()
    router = TurnOutputRouter(io, detached_output=detached, output_limits=OutputLimits(memory_bytes=1))
    router.start()
    gate = asyncio.Event()

    async def send():
        await io.queue.put(chunk(0, "large" * 100))
        await gate.wait()
        return receipt()

    submit = asyncio.create_task(router.submit(send))
    while not router._unclaimed:
        await asyncio.sleep(0)
    await router.stop()
    gate.set()
    with pytest.raises(TurnOutputIncompleteError):
        await submit
    assert router._budget.items == router._budget.storage_bytes == 0
    assert not router._mailboxes and not router._unclaimed
    detached.assert_awaited_once_with(chunk(0, "large" * 100))
