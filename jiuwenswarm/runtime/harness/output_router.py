# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""One session-level reader for turn-scoped projected harness output."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field

from openjiuwen.harness_protocol import DeliveryMode, SendReceipt
from openjiuwen.harness_providers.io_adapter import HarnessIOAdapter, ProjectedOutput
from openjiuwen.harness_providers.output_buffer import (
    OutputBudget, OutputBudgetExceeded, OutputBuffer, OutputLimits,
)


class TurnOutputIncompleteError(RuntimeError):
    """A product Turn output owner closed without observing a terminal event."""


@dataclass(slots=True)
class _Mailbox:
    queue: OutputBuffer
    recovered: deque[ProjectedOutput] = field(default_factory=deque)
    closed: asyncio.Event = field(default_factory=asyncio.Event)
    idle: asyncio.Event = field(default_factory=asyncio.Event)
    reader_idle: asyncio.Event = field(default_factory=asyncio.Event)
    drained: asyncio.Event = field(default_factory=asyncio.Event)
    drain_task: asyncio.Task[None] | None = None

    def __post_init__(self) -> None:
        self.idle.set()
        self.reader_idle.set()
        self.drained.set()


class TurnOutputRouter:
    """Consume one IO queue and hand finite Turn streams to request owners.

    ``submit`` associates output with the receipt before returning. Closing a
    request transfers unread queued output to the detached product projection;
    the session still has exactly one protocol output reader.
    """

    def __init__(
        self,
        io: HarnessIOAdapter,
        *,
        queue_size: int = 128,
        detached_output: Callable[[ProjectedOutput], Awaitable[None]] | None = None,
        output_limits: OutputLimits | None = None,
        max_turns: int = 128,
    ) -> None:
        self._io = io
        self._budget = OutputBudget(output_limits)
        self._max_turns = max(1, max_turns)
        self._failure: OutputBudgetExceeded | None = None
        self._queue_size = max(1, queue_size)
        self._detached_output = detached_output
        self._mailboxes: dict[str, _Mailbox] = {}
        self._closing_mailboxes: dict[str, _Mailbox] = {}
        self._drain_tasks: set[asyncio.Task[None]] = set()
        self._submitting = 0
        self._unclaimed: dict[str, OutputBuffer] = {}
        self._lock = asyncio.Lock()
        self._detached_lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None
        self._closed = False

    def start(self) -> None:
        if self._task is not None or self._closed:
            raise RuntimeError("turn output router already started or closed")
        self._task = asyncio.create_task(self._pump(), name="native_turn_output_router")

    async def submit(self, send: Callable[[], Awaitable[SendReceipt]]) -> SendReceipt:
        if self._task is None or self._closed:
            raise RuntimeError("turn output router is not running")
        if self._failure is not None:
            raise OutputBudgetExceeded(str(self._failure))
        if self._task.done():
            self._task.result()
            raise RuntimeError("turn output reader stopped")
        async with self._lock:
            if self._submitting + len(self._mailboxes) + len(self._closing_mailboxes) >= self._max_turns:
                raise OutputBudgetExceeded("output owner count budget exhausted")
            self._submitting += 1
        receipt: SendReceipt | None = None
        detached: list[OutputBuffer] = []
        try:
            # A STEER may wait for GoalManager while the previous Turn reaches
            # EOF. Never hold the router lock across provider admission.
            receipt = await send()
            if self._closed:
                raise TurnOutputIncompleteError("turn output ended before a terminal event")
            return receipt
        finally:
            async with self._lock:
                if (
                    receipt is not None
                    and not self._closed
                    and receipt.accepted_mode is not DeliveryMode.STEER
                    and receipt.turn_id not in self._mailboxes
                ):
                    buffered = self._unclaimed.pop(receipt.turn_id, None)
                    mailbox = _Mailbox(buffered if buffered is not None else self._new_buffer())
                    if self._failure is not None:
                        mailbox.queue.fail(self._failure)
                    elif self._task is not None and self._task.done():
                        mailbox.queue.finish()
                    self._mailboxes[receipt.turn_id] = mailbox
                self._submitting -= 1
                if self._submitting == 0 and self._unclaimed:
                    detached = list(self._unclaimed.values())
                    self._unclaimed.clear()
            for buffer in detached:
                try:
                    while not buffer.empty():
                        async with self._detached_lock:
                            item = buffer.get_nowait()
                            if self._detached_output is not None:
                                await self._detached_output(item)
                finally:
                    buffer.close()

    def _new_buffer(self) -> OutputBuffer:
        return OutputBuffer(self._budget, memory_items=self._queue_size)

    async def outputs(self, turn_id: str) -> AsyncIterator[ProjectedOutput]:
        mailbox = self._mailboxes.get(turn_id)
        if mailbox is None:
            raise ValueError("turn output has no registered owner")
        try:
            while True:
                item = await self._next(mailbox)
                yield item
                if item.terminal is not None:
                    return
        finally:
            await self._release_mailbox(turn_id, mailbox)

    def abandon(self, turn_id: str) -> None:
        """Release a registered Turn even if its iterator was never started."""
        mailbox = self._mailboxes.get(turn_id)
        if mailbox is not None:
            self._schedule_release(turn_id, mailbox)

    def _schedule_release(self, turn_id: str, mailbox: _Mailbox) -> asyncio.Task[None]:
        if mailbox.drain_task is not None:
            return mailbox.drain_task
        mailbox.closed.set()
        mailbox.drained.clear()
        if self._mailboxes.get(turn_id) is mailbox:
            self._mailboxes.pop(turn_id)
        self._closing_mailboxes[turn_id] = mailbox
        task = asyncio.create_task(self._drain_mailbox(turn_id, mailbox))
        mailbox.drain_task = task
        self._drain_tasks.add(task)
        task.add_done_callback(self._drain_tasks.discard)
        return task

    async def _release_mailbox(self, turn_id: str, mailbox: _Mailbox) -> None:
        await self._schedule_release(turn_id, mailbox)

    async def _drain_mailbox(self, turn_id: str, mailbox: _Mailbox) -> None:
        try:
            await mailbox.idle.wait()
            await mailbox.reader_idle.wait()
            while mailbox.recovered or not mailbox.queue.empty():
                async with self._detached_lock:
                    item = (
                        mailbox.recovered.popleft()
                        if mailbox.recovered else mailbox.queue.get_nowait()
                    )
                    if self._detached_output is not None:
                        await self._detached_output(item)
        except OutputBudgetExceeded as exc:
            await self._fail_output(exc)
        finally:
            close = getattr(mailbox.queue, "close", None)
            if close is not None:
                close()
            mailbox.drained.set()
            if self._closing_mailboxes.get(turn_id) is mailbox:
                self._closing_mailboxes.pop(turn_id)

    def has_owner(self, turn_id: str) -> bool:
        mailbox = self._mailboxes.get(turn_id)
        return mailbox is not None and not mailbox.closed.is_set()

    async def stop(self) -> None:
        if self._closed:
            return
        self._closed = True
        for turn_id, mailbox in list(self._mailboxes.items()):
            self._schedule_release(turn_id, mailbox)
        task = self._task
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._mailboxes.clear()
        unclaimed = list(self._unclaimed.values())
        self._unclaimed.clear()
        for buffer in unclaimed:
            try:
                while not buffer.empty():
                    async with self._detached_lock:
                        item = buffer.get_nowait()
                        if self._detached_output is not None:
                            await self._detached_output(item)
            finally:
                buffer.close()
        self._unclaimed.clear()
        if self._drain_tasks:
            await asyncio.gather(*self._drain_tasks)
        close_detached = getattr(self._detached_output, "close", None)
        if callable(close_detached):
            await close_detached()

    async def _pump(self) -> None:
        try:
            async for item in self._io.output_envelopes():
                if item.turn_id is not None and len(item.turn_id.encode("utf-8")) > 1024:
                    raise OutputBudgetExceeded("Turn identifier byte budget exhausted")
                async with self._lock:
                    mailbox = self._mailboxes.get(item.turn_id)
                    if (mailbox is None or mailbox.closed.is_set()) and self._submitting:
                        key = item.turn_id or ""
                        if key not in self._unclaimed:
                            if len(self._unclaimed) + len(self._mailboxes) + len(self._closing_mailboxes) >= self._max_turns:
                                raise OutputBudgetExceeded("unclaimed Turn count budget exhausted")
                            self._unclaimed[key] = self._new_buffer()
                        self._unclaimed[key].put_nowait(item)
                        continue
                if mailbox is not None and not mailbox.closed.is_set():
                    mailbox.idle.clear()
                    try:
                        queued = await self._put_or_closed(mailbox, item)
                    finally:
                        mailbox.idle.set()
                    if not queued:
                        await mailbox.drained.wait()
                        if self._detached_output is not None:
                            async with self._detached_lock:
                                await self._detached_output(item)
                elif self._detached_output is not None:
                    closing = self._closing_mailboxes.get(item.turn_id or "")
                    if closing is not None:
                        await closing.drained.wait()
                    async with self._detached_lock:
                        await self._detached_output(item)
        except OutputBudgetExceeded as exc:
            await self._fail_output(exc)
        finally:
            if self._failure is None:
                for mailbox in self._mailboxes.values():
                    mailbox.queue.finish()

    async def _fail_output(self, error: OutputBudgetExceeded) -> None:
        if self._failure is not None:
            return
        self._failure = OutputBudgetExceeded(str(error))
        for mailbox in self._mailboxes.values():
            mailbox.queue.fail(self._failure)
        try:
            await asyncio.wait_for(self._io.abort(), timeout=5)
        except Exception:
            pass  # output error remains authoritative; never claim confirmed abort
        report = getattr(self._detached_output, "output_failed", None)
        if callable(report):
            await report(self._failure)

    @staticmethod
    async def _put_or_closed(mailbox: _Mailbox, item: ProjectedOutput) -> bool:
        if mailbox.closed.is_set():
            return False
        mailbox.queue.put_nowait(item)
        return True

    @staticmethod
    async def _next(mailbox: _Mailbox) -> ProjectedOutput:
        if mailbox.closed.is_set():
            raise TurnOutputIncompleteError(
                "turn output ended before a terminal event"
            )
        if mailbox.recovered:
            return mailbox.recovered.popleft()
        if not mailbox.queue.empty():
            return mailbox.queue.get_nowait()
        mailbox.reader_idle.clear()
        staged = isinstance(mailbox.queue, OutputBuffer)
        get = asyncio.create_task(mailbox.queue.wait_ready() if staged else mailbox.queue.get())
        closed = asyncio.create_task(mailbox.closed.wait())
        delivered = False
        try:
            await asyncio.wait({get, closed}, return_when=asyncio.FIRST_COMPLETED)
            if not mailbox.closed.is_set() and get.done():
                try:
                    item = mailbox.queue.get_nowait() if staged else get.result()
                except EOFError:
                    raise TurnOutputIncompleteError("turn output ended before a terminal event") from None
                delivered = True
                return item
            raise TurnOutputIncompleteError(
                "turn output ended before a terminal event"
            )
        finally:
            for task in (get, closed):
                if not task.done():
                    task.cancel()
            await asyncio.gather(get, closed, return_exceptions=True)
            if not staged and not delivered and get.done() and not get.cancelled() and get.exception() is None:
                mailbox.recovered.appendleft(get.result())
            mailbox.reader_idle.set()


__all__ = ["TurnOutputIncompleteError", "TurnOutputRouter"]
