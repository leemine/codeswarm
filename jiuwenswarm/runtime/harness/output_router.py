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


class TurnOutputRouter:
    """Consume one IO queue and hand finite Turn streams to request owners.

    ``submit`` installs a mailbox before the provider can publish output. An
    abandoned request closes only its mailbox; the session reader continues.
    The protocol event observer remains responsible for durable observations.
    """

    def __init__(self, io: HarnessIOAdapter, *, queue_size: int = 128) -> None:
        self._io = io
        self._queue_size = max(1, queue_size)
        self._mailboxes: dict[str, _Mailbox] = {}
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
            receipt = await send()
            # STEER joins an existing Turn. It must never resurrect a mailbox
            # abandoned by a disconnected request or claim a Goal-only Turn.
            if receipt.accepted_mode is not DeliveryMode.STEER and receipt.turn_id not in self._mailboxes:
                self._mailboxes[receipt.turn_id] = _Mailbox(
                    asyncio.Queue(maxsize=self._queue_size)
                )
            return receipt

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
            mailbox.closed.set()
            async with self._lock:
                if self._mailboxes.get(turn_id) is mailbox:
                    self._mailboxes.pop(turn_id)

    def abandon(self, turn_id: str) -> None:
        """Release a registered Turn even if its iterator was never started."""
        mailbox = self._mailboxes.pop(turn_id, None)
        if mailbox is not None:
            mailbox.closed.set()

    async def stop(self) -> None:
        if self._closed:
            return
        self._closed = True
        for mailbox in self._mailboxes.values():
            mailbox.closed.set()
        task = self._task
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._mailboxes.clear()

    async def _pump(self) -> None:
        try:
            async for item in self._io.output_envelopes():
                async with self._lock:
                    mailbox = self._mailboxes.get(item.turn_id)
                if mailbox is not None and not mailbox.closed.is_set():
                    await self._put_or_closed(mailbox, item)
        finally:
            for mailbox in self._mailboxes.values():
                mailbox.closed.set()

    @staticmethod
    async def _put_or_closed(mailbox: _Mailbox, item: ProjectedOutput) -> None:
        put = asyncio.create_task(mailbox.queue.put(item))
        closed = asyncio.create_task(mailbox.closed.wait())
        try:
            await asyncio.wait({put, closed}, return_when=asyncio.FIRST_COMPLETED)
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
