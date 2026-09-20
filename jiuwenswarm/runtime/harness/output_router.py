# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""One session-level reader for turn-scoped projected harness output."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field

from openjiuwen.harness_protocol import DeliveryMode, SendReceipt
from openjiuwen.harness_providers.io_adapter import HarnessIOAdapter, ProjectedOutput


@dataclass(slots=True)
class _Mailbox:
    queue: asyncio.Queue[ProjectedOutput]
    closed: asyncio.Event = field(default_factory=asyncio.Event)
    idle: asyncio.Event = field(default_factory=asyncio.Event)
    drained: asyncio.Event = field(default_factory=asyncio.Event)
    drain_task: asyncio.Task[None] | None = None

    def __post_init__(self) -> None:
        self.idle.set()
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
    ) -> None:
        self._io = io
        self._queue_size = max(1, queue_size)
        self._detached_output = detached_output
        self._mailboxes: dict[str, _Mailbox] = {}
        self._closing_mailboxes: dict[str, _Mailbox] = {}
        self._drain_tasks: set[asyncio.Task[None]] = set()
        self._submitting = 0
        self._unclaimed: dict[str, list[ProjectedOutput]] = {}
        self._lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None
        self._closed = False

    def start(self) -> None:
        if self._task is not None or self._closed:
            raise RuntimeError("turn output router already started or closed")
        self._task = asyncio.create_task(self._pump(), name="native_turn_output_router")

    async def submit(self, send: Callable[[], Awaitable[SendReceipt]]) -> SendReceipt:
        if self._task is None or self._closed:
            raise RuntimeError("turn output router is not running")
        if self._task.done():
            self._task.result()
            raise RuntimeError("turn output reader stopped")
        async with self._lock:
            self._submitting += 1
        receipt: SendReceipt | None = None
        detached: list[ProjectedOutput] = []
        try:
            # A STEER may wait for GoalManager while the previous Turn reaches
            # EOF. Never hold the router lock across provider admission.
            receipt = await send()
            return receipt
        finally:
            async with self._lock:
                if (
                    receipt is not None
                    and receipt.accepted_mode is not DeliveryMode.STEER
                    and receipt.turn_id not in self._mailboxes
                ):
                    buffered = self._unclaimed.pop(receipt.turn_id, [])
                    mailbox = _Mailbox(
                        asyncio.Queue(maxsize=max(self._queue_size, len(buffered)))
                    )
                    for item in buffered:
                        mailbox.queue.put_nowait(item)
                    self._mailboxes[receipt.turn_id] = mailbox
                self._submitting -= 1
                if self._submitting == 0 and self._unclaimed:
                    detached = [
                        item for items in self._unclaimed.values() for item in items
                    ]
                    self._unclaimed.clear()
            for item in detached:
                if self._detached_output is not None:
                    await self._detached_output(item)

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
            while not mailbox.queue.empty():
                item = mailbox.queue.get_nowait()
                if self._detached_output is not None:
                    await self._detached_output(item)
        finally:
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
        self._unclaimed.clear()
        if self._drain_tasks:
            await asyncio.gather(*self._drain_tasks)
        close_detached = getattr(self._detached_output, "close", None)
        if callable(close_detached):
            await close_detached()

    async def _pump(self) -> None:
        try:
            async for item in self._io.output_envelopes():
                async with self._lock:
                    mailbox = self._mailboxes.get(item.turn_id)
                    if (mailbox is None or mailbox.closed.is_set()) and self._submitting:
                        self._unclaimed.setdefault(item.turn_id or "", []).append(item)
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
                            await self._detached_output(item)
                elif self._detached_output is not None:
                    closing = self._closing_mailboxes.get(item.turn_id or "")
                    if closing is not None:
                        await closing.drained.wait()
                    await self._detached_output(item)
        finally:
            for mailbox in self._mailboxes.values():
                mailbox.closed.set()

    @staticmethod
    async def _put_or_closed(mailbox: _Mailbox, item: ProjectedOutput) -> bool:
        put = asyncio.create_task(mailbox.queue.put(item))
        closed = asyncio.create_task(mailbox.closed.wait())
        try:
            await asyncio.wait({put, closed}, return_when=asyncio.FIRST_COMPLETED)
            return put.done() and not put.cancelled()
        finally:
            for task in (put, closed):
                if not task.done():
                    task.cancel()
            await asyncio.gather(put, closed, return_exceptions=True)

    @staticmethod
    async def _next(mailbox: _Mailbox) -> ProjectedOutput:
        if not mailbox.queue.empty():
            return mailbox.queue.get_nowait()
        if mailbox.closed.is_set():
            raise RuntimeError("turn output ended before a terminal event")
        get = asyncio.create_task(mailbox.queue.get())
        closed = asyncio.create_task(mailbox.closed.wait())
        try:
            await asyncio.wait({get, closed}, return_when=asyncio.FIRST_COMPLETED)
            if get.done():
                return get.result()
            raise RuntimeError("turn output ended before a terminal event")
        finally:
            for task in (get, closed):
                if not task.done():
                    task.cancel()
            await asyncio.gather(get, closed, return_exceptions=True)
