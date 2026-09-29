"""Resident retry of durable Browser file history, independent of Turn startup.

The session history is the outbox. Only scheduling hints live in memory; a new
AgentServer discovers them again without executing any old Browser action.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from jiuwenswarm.runtime.host_services import (
    install_artifact_retry_handler,
    remove_artifact_retry_handler,
)
from jiuwenswarm.server.runtime.session.history_io import run_history_io

logger = logging.getLogger(__name__)


class ArtifactOutbox:
    def __init__(self, push: Callable[[dict[str, Any]], Awaitable[bool]], *, retry_seconds: float = 2) -> None:
        self._push = push
        self._retry_seconds = retry_seconds
        self._pending: dict[str, int] = {}
        self._version = 0
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._handler = self.enqueue
        self._discovered = False

    def start(self) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._run(), name="browser-artifact-outbox")
        install_artifact_retry_handler(self._handler)

    def enqueue(self, session_id: str) -> bool:
        if self._task is None or self._task.done():
            return False
        self._version += 1
        self._pending[session_id] = self._version
        self._wake.set()
        return True

    async def close(self) -> None:
        remove_artifact_retry_handler(self._handler)
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _discover(self) -> None:
        from jiuwenswarm.server.runtime.session.session_metadata import collect_all_sessions_metadata
        sessions = await run_history_io(collect_all_sessions_metadata)
        for session in sessions:
            if not session.get("execution_blocked") and not session.get("ephemeral"):
                self.enqueue(session["session_id"])
        self._discovered = True

    async def _replay(self, session_id: str, version: int, semaphore: asyncio.Semaphore) -> None:
        from jiuwenswarm.agents.harness.common.tools.send_file_to_user import SendFileToolkit
        from jiuwenswarm.server.runtime.session import lifecycle

        async with semaphore:
            try:
                await run_history_io(lifecycle.guard, session_id)
                toolkit = SendFileToolkit(request_id="", session_id=session_id, channel_id="")

                async def push(message: dict[str, Any]) -> bool:
                    # Recheck before each file: deletion/archive may have begun
                    # while another file was awaiting its Gateway receipt.
                    await run_history_io(lifecycle.guard, session_id)
                    return await self._push(message)

                await toolkit.replay_projected_artifacts(push=push, require_origin=True)
            except lifecycle.LifecycleError:
                # Archived/deleted sessions must not resume background delivery.
                pass
            except Exception:
                logger.debug("Browser Artifact remains pending for session %s", session_id, exc_info=True)
                return
            if self._pending.get(session_id) == version:
                self._pending.pop(session_id, None)

    async def _run(self) -> None:
        while True:
            self._wake.clear()
            try:
                if not self._discovered:
                    await self._discover()
                semaphore = asyncio.Semaphore(8)
                # Bound each sweep; rotate failed sessions so they cannot starve
                # later ones when a Gateway is down.
                batch = list(self._pending.items())[:128]
                for session_id, version in batch:
                    self._pending.pop(session_id)
                    self._pending[session_id] = version
                async with asyncio.TaskGroup() as group:
                    for session_id, version in batch:
                        group.create_task(self._replay(session_id, version, semaphore))
            except Exception:
                logger.exception("Browser Artifact recovery sweep failed")
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self._retry_seconds)
            except TimeoutError:
                pass
