"""Live Native admission retained by the original Runtime execution handle."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from openjiuwen.core.controller.schema.execution_origin import ExecutionOrigin

from jiuwenswarm.runtime.session.model import SessionExecutionEndedError


@dataclass(eq=False, repr=False)
class NativeExecutionAdmission:
    """Original references and completion receipts, never a second Turn queue."""

    coordinator: object
    record: object
    owner: object
    native: object
    check_authority: object
    producer: asyncio.Task
    source: ExecutionOrigin | None = None
    owned_turn: object | None = None
    admitted: asyncio.Event = field(default_factory=asyncio.Event)
    confirmed: asyncio.Event = field(default_factory=asyncio.Event)
    exit_task: asyncio.Task | None = None
    deferred_terminal: tuple | None = None
    producer_cancel_requested: bool = False

    def check_owner(self):
        c, r, h = self.coordinator, self.record, self.owner
        if (c._sessions.get(h.session_id) is not r
                or c._registry.get(h.execution_id) is not h
                or r.generation != h.generation
                or h._native_admission is not self):
            raise SessionExecutionEndedError('original Native admission was replaced')

    def check_current(self):
        self.check_owner()
        if (self.owner.state.terminal or self.owner.cancellation_requested
                or self.confirmed.is_set()):
            raise SessionExecutionEndedError('original Native admission ended')
        self.check_authority()

    def bind(self, owned):
        self.check_owner()
        if owned.source is not self.source or owned.source.host_value is not self.owner:
            raise SessionExecutionEndedError('Native Turn has a different original owner')
        if self.owned_turn is not None and self.owned_turn is not owned:
            raise SessionExecutionEndedError('original Native Turn cannot be replaced')
        self.owned_turn = owned
        self.admitted.set()

    def terminal(self, owned, _kind):
        self.bind(owned)
        self.confirmed.set()
        from openjiuwen.harness_protocol import TurnEventKind
        from jiuwenswarm.runtime.session.model import SessionExecutionState
        state = {TurnEventKind.FINISHED: SessionExecutionState.SUCCEEDED,
                 TurnEventKind.FAILED: SessionExecutionState.FAILED,
                 TurnEventKind.ABORTED: SessionExecutionState.CANCELLED}.get(_kind)
        if state is not None:
            self.owner.control_origin_terminal = (SessionExecutionState.CANCELLED
                if self.owner.cancellation_requested else state)
            self.coordinator._settle_native_control_origin(self.record, self.owner)
        self.settle_terminal()

    def not_admitted(self):
        self.check_owner()
        if self.owned_turn is not None:
            raise SessionExecutionEndedError('accepted Native Turn cannot be rejected')
        self.admitted.set()
        self.confirmed.set()
        self.settle_terminal()

    def terminal_ready(self):
        return self.confirmed.is_set() and self.producer.done()

    def settle_terminal(self, _task=None):
        if self.deferred_terminal is None or not self.terminal_ready():
            return
        self.check_owner()
        state, error = self.deferred_terminal
        self.deferred_terminal = None
        self.coordinator._registry.mark_terminal(self.owner, state, error=error)
        self.coordinator._refresh_session_state(self.record)
        self.coordinator._notify_execution_changed(self.record)

    async def finish(self):
        self.check_owner()
        await self.admitted.wait()
        self.check_owner()
        if not self.confirmed.is_set():
            await self.native.abort_owned_request_turn(self.owned_turn)
            if not self.confirmed.is_set():
                raise SessionExecutionEndedError('original Native exit has no observer receipt')

    def start_exit(self):
        self.check_owner()
        self.owner.cancellation_requested = True
        if self.exit_task is None or self.exit_task.done():
            if self.exit_task is not None and not self.exit_task.cancelled():
                self.exit_task.exception()
            self.exit_task = asyncio.create_task(self.finish())
            self.exit_task.add_done_callback(self.coordinator._consume_task)
        return self.exit_task

    def request_producer_cancel(self, task):
        """Send cancellation once to the original producer, then join its tail."""
        self.check_owner()
        if task is not self.producer:
            raise SessionExecutionEndedError('Native producer was replaced')
        if task.done() or task is asyncio.current_task():
            return
        if not self.producer_cancel_requested:
            self.producer_cancel_requested = True
            if not task.cancelling():
                task.cancel()
