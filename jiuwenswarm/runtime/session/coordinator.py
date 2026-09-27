"""Product-level Session lifecycle and execution coordinator."""

from __future__ import annotations

import asyncio
import inspect
import uuid
from contextlib import aclosing
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Awaitable, Callable, TypeVar

from jiuwenswarm.runtime.session.execution_registry import SessionExecutionRegistry
from jiuwenswarm.runtime.session.model import (
    CancelExecutionResult,
    CloseSessionResult,
    RuntimeSessionSnapshot,
    RuntimeSessionState,
    SessionCloseTimeoutError,
    SessionControlAlreadyDelivered,
    SessionControlConflictError,
    SessionExecutionEndedError,
    SessionExecutionHandle,
    SessionExecutionSnapshot,
    SessionExecutionState,
    SessionGenerationMismatchError,
    SessionPersistencePolicy,
    SessionRequestDuplicateError,
    SessionSubmissionState,
    SessionWorkKind,
)
from jiuwenswarm.runtime.session.work_scheduler import SessionWorkScheduler

T = TypeVar("T")


@dataclass(slots=True)
class _SessionRecord:
    session_id: str
    channel_id: str
    persistence_policy: SessionPersistencePolicy
    generation: int
    state: RuntimeSessionState = RuntimeSessionState.READY
    control_ready: asyncio.Event = field(default_factory=asyncio.Event)
    stream_control_claims: set[str] = field(default_factory=set)
    external_execution: bool = False
    external_owner: str | None = None
    execution_changed: asyncio.Event = field(default_factory=asyncio.Event)

    def __post_init__(self) -> None:
        self.control_ready.set()


@dataclass(slots=True)
class _StreamItem:
    value: Any = None
    error: BaseException | None = None
    done: bool = False


class RuntimeSessionCoordinator:
    """Owns ephemeral execution state, not durable Session metadata/history."""

    def __init__(
        self,
        *,
        registry: SessionExecutionRegistry | None = None,
        scheduler: SessionWorkScheduler | None = None,
        cancel_timeout: float = 5.0,
        stream_buffer_size: int = 64,
    ) -> None:
        self._registry = registry or SessionExecutionRegistry()
        self._scheduler = scheduler or SessionWorkScheduler()
        self._cancel_timeout = cancel_timeout
        self._stream_buffer_size = max(1, stream_buffer_size)
        self._sessions: dict[str, _SessionRecord] = {}
        self._generations: dict[str, int] = {}
        self._control_claims: set[tuple[str, int, str, str]] = set()
        self._accepting = True
        self._lock = asyncio.Lock()

    def external_execution_owner(self, session_id: str, request_id: str) -> SessionExecutionHandle:
        """Resolve authority from the original registry, never request metadata."""
        record = self._require_open_session(session_id)
        matches = self._registry.select(
            session_id=session_id, request_id=request_id,
            generation=record.generation, active_only=True,
        )
        if len(matches) != 1 or matches[0].cancellation_requested:
            raise SessionExecutionEndedError("External execution owner is unavailable")
        record.external_execution = True
        return matches[0]

    async def acquire_external_execution(self, owner: SessionExecutionHandle, *, goal: bool) -> None:
        """Grant one actual External send inside the existing Runtime producer.

        Native scheduling is unchanged. Goal controls and interaction answers
        never acquire this permit, so they can unblock its current holder.
        """
        while True:
            async with self._lock:
                record = self._require_open_session(owner.session_id)
                self._require_generation(record, owner.generation)
                if owner.state.terminal or owner.cancellation_requested:
                    raise SessionExecutionEndedError("External execution owner ended")
                active = self._registry.select(
                    session_id=owner.session_id, generation=owner.generation,
                    active_only=True,
                )
                user_waiting = goal and any(
                    other is not owner and other.work_kind in {
                        SessionWorkKind.CHAT_UNARY, SessionWorkKind.CHAT_STREAM,
                        SessionWorkKind.SESSION_MESSAGE, SessionWorkKind.HEARTBEAT,
                    } for other in active
                )
                if record.external_owner in (None, owner.execution_id) and not user_waiting:
                    record.external_execution = True
                    record.external_owner = owner.execution_id
                    return
                changed = record.execution_changed
            await changed.wait()

    async def request_external_execution_cancel(self, owner: SessionExecutionHandle) -> None:
        """Request the original producer's cancellation without joining it.

        GoalManager calls this under its control lock after Provider exit; the
        producer must be free to settle after that lock is released.
        """
        record = self._sessions.get(owner.session_id)
        if record is not None and record.generation == owner.generation and not owner.state.terminal:
            await self._request_direct_cancel([owner], wait_timeout=0)

    def holds_external_execution(self, owner: SessionExecutionHandle) -> bool:
        record = self._sessions.get(owner.session_id)
        return bool(record is not None and record.generation == owner.generation
                    and record.external_owner == owner.execution_id)

    def release_external_execution(self, owner: SessionExecutionHandle) -> None:
        record = self._sessions.get(owner.session_id)
        if (record is not None and record.generation == owner.generation
                and record.external_owner == owner.execution_id):
            record.external_owner = None
            self._notify_execution_changed(record)

    @staticmethod
    def _notify_execution_changed(record: _SessionRecord) -> None:
        record.execution_changed.set()
        record.execution_changed = asyncio.Event()

    async def register_session(
        self,
        session_id: str,
        channel_id: str,
        persistence_policy: SessionPersistencePolicy = SessionPersistencePolicy.PERSISTENT,
    ) -> RuntimeSessionSnapshot:
        normalized = str(session_id).strip()
        if not normalized:
            raise ValueError("session_id is required")
        async with self._lock:
            if not self._accepting:
                raise RuntimeError("session coordinator is closed")
            current = self._sessions.get(normalized)
            if current is None or current.state is RuntimeSessionState.CLOSED:
                generation = self._generations.get(normalized, 0) + 1
                self._generations[normalized] = generation
                current = _SessionRecord(
                    session_id=normalized,
                    channel_id=channel_id or "default",
                    persistence_policy=persistence_policy,
                    generation=generation,
                )
                self._sessions[normalized] = current
            else:
                current.channel_id = channel_id or current.channel_id
                current.persistence_policy = persistence_policy
            return self._snapshot(current)

    async def run_unary(
        self,
        session_id: str,
        request_id: str,
        work_kind: SessionWorkKind,
        operation: Callable[[], Awaitable[T]],
        *,
        suspension_key: Callable[[T], str | None] | None = None,
        wait_for_terminal: bool = False,
        timeout_scope: asyncio.Timeout | None = None,
        timeout_error: str = "execution deadline exceeded",
    ) -> T:
        record = self._require_open_session(session_id)
        handle = self._new_execution(record, request_id, work_kind)
        handle.retain_owner_task = wait_for_terminal
        return await self._run_unary(
            record,
            handle,
            operation,
            suspension_key,
            wait_for_terminal=wait_for_terminal,
            timeout_scope=timeout_scope,
            timeout_error=timeout_error,
        )

    def submit_unary(
        self,
        session_id: str,
        request_id: str,
        work_kind: SessionWorkKind,
        operation: Callable[[], Awaitable[T]],
        *,
        suspension_key: Callable[[T], str | None] | None = None,
    ) -> SessionExecutionSnapshot:
        """Queue owned work and return before the operation completes."""
        record = self._require_open_session(session_id)
        existing = self._registry.select(
            session_id=session_id,
            request_id=request_id,
            generation=record.generation,
        )
        if existing:
            if existing[-1].work_kind is not work_kind:
                raise ValueError(
                    f"request_id already belongs to {existing[-1].work_kind.value}"
                )
            return existing[-1].snapshot()
        handle = self._new_execution(record, request_id, work_kind)

        async def submitted() -> None:
            if work_kind is SessionWorkKind.SESSION_MESSAGE:
                await record.control_ready.wait()
            await self._run_unary(record, handle, operation, suspension_key)

        task = asyncio.create_task(submitted())
        handle.task = task
        task.add_done_callback(self._consume_task)
        return handle.snapshot()

    def begin_detached_turn(
        self, session_id: str, request_id: str
    ) -> SessionExecutionSnapshot:
        """Track an already-running provider Turn without acquiring another lane."""
        record = self._require_open_session(session_id)
        handle = self._new_execution(record, request_id, SessionWorkKind.GOAL_ATTACH)
        handle.retain_after_control = True
        self._registry.mark_running(handle)
        self._refresh_session_state(record)
        return handle.snapshot()

    def observe_detached_turn(
        self, session_id: str, execution_id: str, control_id: str | None
    ) -> bool:
        """Apply one projected control locator to the existing execution registry."""
        record = self._sessions.get(session_id)
        handle = self._registry.get(execution_id)
        if (
            record is None
            or record.state in {RuntimeSessionState.QUIESCING, RuntimeSessionState.CLOSED}
            or handle is None
            or handle.session_id != session_id
            or handle.generation != record.generation
            or handle.state.terminal
        ):
            return False
        if control_id:
            self._registry.mark_awaiting_control(handle, control_id)
            self._registry.mark_waiting(handle)
            self._refresh_control_gate(record)
        return True

    def finish_detached_turn(
        self, session_id: str, execution_id: str,
        state: SessionExecutionState,
    ) -> bool:
        """Settle the detached Turn only if it still owns this generation."""
        if not state.terminal:
            raise ValueError("detached Turn requires a terminal state")
        record = self._sessions.get(session_id)
        handle = self._registry.get(execution_id)
        if (
            record is None or handle is None
            or handle.session_id != session_id
            or handle.generation != record.generation
            or handle.state.terminal
        ):
            return False
        self._registry.mark_terminal(handle, state)
        self._refresh_session_state(record)
        return True

    async def _run_unary(
        self,
        record: _SessionRecord,
        handle: SessionExecutionHandle,
        operation: Callable[[], Awaitable[T]],
        suspension_key: Callable[[T], str | None] | None,
        *,
        wait_for_terminal: bool = False,
        timeout_scope: asyncio.Timeout | None = None,
        timeout_error: str = "execution deadline exceeded",
    ) -> T:
        async def tracked() -> T:
            self._registry.mark_running(handle)
            record.state = RuntimeSessionState.ACTIVE
            try:
                value = await operation()
            except asyncio.CancelledError as exc:
                self._mark_submission_interrupted(handle)
                timed_out = timeout_scope is not None and timeout_scope.expired()
                self._registry.mark_terminal(
                    handle,
                    (
                        SessionExecutionState.FAILED
                        if timed_out
                        else SessionExecutionState.CANCELLED
                    ),
                    error=timeout_error if timed_out else exc,
                )
                raise
            except BaseException as exc:
                self._mark_submission_failed(handle)
                self._registry.mark_terminal(
                    handle, SessionExecutionState.FAILED, error=exc
                )
                raise
            else:
                self._observe_submission_value(handle, value)
                if timeout_scope is not None and timeout_scope.expired():
                    self._registry.mark_terminal(
                        handle,
                        SessionExecutionState.FAILED,
                        error=timeout_error,
                    )
                    raise TimeoutError(timeout_error)
                control_id = (
                    suspension_key(value) if suspension_key is not None else None
                )
                if control_id:
                    self._registry.mark_awaiting_control(handle, control_id)
                if handle.waiting_control_id:
                    self._registry.mark_waiting(handle)
                else:
                    self._registry.mark_terminal(
                        handle, SessionExecutionState.SUCCEEDED
                    )
                return value
            finally:
                self._refresh_session_state(record)

        try:
            if handle.work_kind.scheduled:
                return await self._scheduler.submit_and_wait(handle, tracked)
            handle.task = asyncio.current_task()
            if handle.retain_owner_task and handle.task is not None:
                handle.task.add_done_callback(
                    lambda task: self._registry.release_owner_task(handle, task)
                )
            value = await tracked()
            if wait_for_terminal and not handle.state.terminal:
                handle.task = asyncio.current_task()
                await handle.terminal_event.wait()
                if handle.state is SessionExecutionState.CANCELLED:
                    raise asyncio.CancelledError(handle.error or "execution cancelled")
                if handle.state is SessionExecutionState.FAILED:
                    raise RuntimeError(handle.error or "execution failed")
            return value
        except asyncio.CancelledError:
            handle.cancellation_requested = True
            if handle.work_kind.scheduled:
                await self._scheduler.cancel_handles(
                    [handle], wait_timeout=self._cancel_timeout
                )
            if not handle.state.terminal:
                timed_out = timeout_scope is not None and timeout_scope.expired()
                self._registry.mark_terminal(
                    handle,
                    (
                        SessionExecutionState.FAILED
                        if timed_out
                        else SessionExecutionState.CANCELLED
                    ),
                    error=timeout_error if timed_out else None,
                )
            await self._cancel_descendants(handle)
            self._refresh_session_state(record)
            raise
        except BaseException:
            await self._cancel_descendants(handle)
            raise
        finally:
            if (
                not handle.work_kind.scheduled
                and not handle.retain_owner_task
                and handle.task is asyncio.current_task()
            ):
                handle.task = None

    def record_interaction(
        self, session_id: str, request_id: str, control_id: str
    ) -> bool:
        """Associate an interaction emitted inside an owned callback execution."""
        record = self._sessions.get(session_id)
        if record is None or record.state is not RuntimeSessionState.ACTIVE:
            return False
        for handle in self._registry.select(
            session_id=session_id,
            request_id=request_id,
            generation=record.generation,
            active_only=True,
        ):
            if handle.state is SessionExecutionState.RUNNING:
                self._registry.mark_awaiting_control(handle, control_id)
                self._refresh_control_gate(record)
                return True
        return False

    async def deliver_control(
        self,
        session_id: str,
        request_id: str,
        operation: Callable[[], Awaitable[T]],
        *,
        suspension_key: Callable[[T], str | None] | None = None,
        generation: int | None = None,
        control_fingerprint: str | None = None,
    ) -> T:
        """Deliver input to the running Session work without joining its lane."""
        record = self._require_open_session(session_id)
        self._require_generation(record, generation)
        delivered = self._completed_control(
            record, request_id, control_fingerprint=control_fingerprint
        )
        if delivered is not None:
            raise SessionControlAlreadyDelivered(
                f"interaction answer was already delivered: {request_id}"
            )
        if self._is_control_claimed(record, request_id):
            raise RuntimeError("control input is already being delivered")
        parent = self._control_parent(record, request_id)
        if parent is None:
            raise RuntimeError(f"session has no active execution: {session_id}")
        claim = (session_id, record.generation, parent.execution_id, request_id)
        self._control_claims.add(claim)
        # Everything below belongs to the finally: _new_execution can still
        # fail, and a leaked claim would make every later answer for this
        # request_id look already-in-flight. Resuming the parent is deferred
        # until the child exists, so a failure leaves the parent resumable.
        try:
            handle = self._new_execution(
                record,
                request_id,
                SessionWorkKind.CONTROL_INPUT,
                parent_execution_id=parent.execution_id,
                allow_duplicate_request=True,
            )
            handle.control_fingerprint = control_fingerprint
            parent_was_waiting = (
                parent.state is SessionExecutionState.WAITING_FOR_CONTROL
            )
            if parent_was_waiting:
                self._registry.resume_waiting(parent)
            handle.task = asyncio.current_task()
            self._registry.mark_running(handle)
            try:
                value = await operation()
            except asyncio.CancelledError as exc:
                self._mark_submission_interrupted(handle)
                self._registry.mark_terminal(
                    handle, SessionExecutionState.CANCELLED, error=exc
                )
                if parent_was_waiting and not parent.state.terminal:
                    self._registry.mark_awaiting_control(parent, request_id)
                    self._registry.mark_waiting(parent)
                raise
            except BaseException as exc:
                self._mark_submission_failed(handle)
                self._registry.mark_terminal(
                    handle, SessionExecutionState.FAILED, error=exc
                )
                if parent_was_waiting and not parent.state.terminal:
                    self._registry.mark_awaiting_control(parent, request_id)
                    self._registry.mark_waiting(parent)
                raise

            self._observe_submission_value(handle, value)
            handle.submission_state = SessionSubmissionState.PROVIDER_ACCEPTED

            if (
                parent.state.terminal
                or self._sessions.get(session_id) is not record
                or record.state in {
                    RuntimeSessionState.QUIESCING,
                    RuntimeSessionState.CLOSED,
                }
            ):
                self._registry.mark_terminal(
                    handle, SessionExecutionState.CANCELLED
                )
                raise SessionExecutionEndedError(
                    "control input arrived after its execution ended: "
                    f"session={session_id} request={request_id}"
                )

            handle.control_delivered = True

            control_id = suspension_key(value) if suspension_key is not None else None
            heartbeat_root = self._heartbeat_root(parent)
            if control_id and heartbeat_root is not None:
                self._registry.mark_awaiting_control(heartbeat_root, control_id)
                self._registry.mark_waiting(heartbeat_root)
                self._registry.mark_terminal(handle, SessionExecutionState.SUCCEEDED)
            else:
                parent_finished_while_delivering = (
                    parent.state is SessionExecutionState.WAITING_FOR_CONTROL
                    and parent.waiting_control_id == request_id
                )
                if (parent_was_waiting or parent_finished_while_delivering) and not parent.retain_after_control:
                    self._registry.mark_terminal(
                        parent, SessionExecutionState.SUCCEEDED
                    )
                elif parent.waiting_control_id == request_id:
                    parent.waiting_control_id = None
            if control_id and heartbeat_root is None:
                self._registry.mark_awaiting_control(handle, control_id)
                self._registry.mark_waiting(handle)
            elif not control_id:
                self._registry.mark_terminal(
                    handle, SessionExecutionState.SUCCEEDED
                )
            return value
        finally:
            self._control_claims.discard(claim)
            self._refresh_session_state(record)

    def has_control_target(self, session_id: str, request_id: str) -> bool:
        """Return whether control input can resume a live Session execution."""
        record = self._sessions.get(session_id)
        if record is None or record.state is RuntimeSessionState.CLOSED:
            return False
        return self._is_control_claimed(
            record, request_id
        ) or self._control_parent(record, request_id) is not None

    def control_target_work_kind(
        self, session_id: str, request_id: str
    ) -> SessionWorkKind | None:
        record = self._sessions.get(session_id)
        if record is None or record.state is RuntimeSessionState.CLOSED:
            return None
        parent = self._control_parent(record, request_id)
        if parent is None:
            parent = self._claimed_control_parent(record, request_id)
        root = None if parent is None else self._heartbeat_root(parent)
        return root.work_kind if root is not None else None

    async def deliver_control_stream(
        self,
        session_id: str,
        request_id: str,
        operation: Callable[[], AsyncIterator[T] | Awaitable[AsyncIterator[T]]],
        *,
        suspension_key: Callable[[T], str | None] | None = None,
        generation: int | None = None,
        control_fingerprint: str | None = None,
    ) -> AsyncIterator[T]:
        """Stream a matching control operation without joining the work lane.

        Unlike the compatibility unary control API, observations are visible
        before the operation ends, including a second question awaiting input.
        The output consumer owns this generator and must close it on exit.
        """
        record = self._require_open_session(session_id)
        self._require_generation(record, generation)
        delivered = self._completed_control(
            record, request_id, control_fingerprint=control_fingerprint
        )
        if delivered is not None:
            raise SessionControlAlreadyDelivered(
                f"interaction answer was already delivered: {request_id}"
            )
        parent = self._stream_control_parent(record, request_id)
        parent_was_waiting = parent.state is SessionExecutionState.WAITING_FOR_CONTROL
        record.stream_control_claims.add(request_id)
        if parent_was_waiting:
            self._registry.resume_waiting(parent)
        handle = self._new_execution(
            record,
            request_id,
            SessionWorkKind.CONTROL_INPUT,
            parent_execution_id=parent.execution_id,
            allow_duplicate_request=True,
        )
        handle.control_fingerprint = control_fingerprint
        handle.task = asyncio.current_task()
        self._registry.mark_running(handle)
        try:
            candidate = operation()
            stream = await candidate if inspect.isawaitable(candidate) else candidate
            try:
                async for item in stream:
                    self._observe_submission_value(handle, item)
                    control_id = suspension_key(item) if suspension_key else None
                    if control_id:
                        self._registry.mark_awaiting_control(handle, control_id)
                        self._refresh_control_gate(record)
                    yield item
            finally:
                close = getattr(stream, "aclose", None)
                if callable(close):
                    await close()
        except (asyncio.CancelledError, GeneratorExit) as exc:
            self._mark_submission_interrupted(handle)
            self._registry.mark_terminal(handle, SessionExecutionState.CANCELLED, error=exc)
            if parent_was_waiting:
                self._registry.mark_waiting(parent)
            raise
        except BaseException as exc:
            self._mark_submission_failed(handle)
            self._registry.mark_terminal(handle, SessionExecutionState.FAILED, error=exc)
            if parent_was_waiting:
                self._registry.mark_waiting(parent)
            raise
        else:
            handle.submission_state = SessionSubmissionState.PROVIDER_ACCEPTED
            handle.control_delivered = True
            if parent_was_waiting and not parent.retain_after_control:
                self._registry.mark_terminal(parent, SessionExecutionState.SUCCEEDED)
            elif parent.waiting_control_id == request_id:
                # The original producer may already have published another
                # question. Do not erase that newer control locator.
                parent.waiting_control_id = None
            if handle.waiting_control_id:
                self._registry.mark_waiting(handle)
            else:
                self._registry.mark_terminal(handle, SessionExecutionState.SUCCEEDED)
        finally:
            record.stream_control_claims.discard(request_id)
            if handle.task is asyncio.current_task():
                handle.task = None
            self._refresh_session_state(record)

    def _stream_control_parent(
        self, record: _SessionRecord, request_id: str
    ) -> SessionExecutionHandle:
        """Claim only an existing matching interaction, never a fresh turn."""
        if request_id in record.stream_control_claims:
            raise RuntimeError("interaction answer is already in progress")
        parents: list[SessionExecutionHandle] = []
        active = self._registry.select(
            session_id=record.session_id,
            generation=record.generation,
            active_only=True,
        )
        for handle in active:
            if handle.waiting_control_id != request_id:
                continue
            if handle.state in {
                SessionExecutionState.RUNNING,
                SessionExecutionState.WAITING_FOR_CONTROL,
            }:
                parents.append(handle)
        if not parents:
            raise RuntimeError(f"session has no active execution: {record.session_id}")
        return max(parents, key=lambda handle: handle.started_at or handle.created_at)

    async def stream_session_input(
        self,
        session_id: str,
        request_id: str,
        operation: Callable[[str], AsyncIterator[T]],
        idle_operation: Callable[[], AsyncIterator[T]],
        *,
        suspension_key: Callable[[T], str | None] | None = None,
    ) -> AsyncIterator[T]:
        """Track an input without acquiring the active task's scheduling lane.

        An input does not resume, complete, or replace a waiting interaction.
        The parent link exists for cancellation/close, not round binding: if
        the SDK has become idle it can give this input its own output stream.
        """
        record = self._require_open_session(session_id)
        active = self._registry.select(
            session_id=session_id, generation=record.generation, active_only=True,
        )
        if any(handle.waiting_control_id for handle in active):
            raise RuntimeError("session is waiting for an interaction answer; supplemental input was not sent")
        parents = []
        for handle in active:
            if handle.state is not SessionExecutionState.RUNNING:
                continue
            if handle.work_kind in {
                SessionWorkKind.SESSION_INPUT, SessionWorkKind.GOAL_CONTROL,
                SessionWorkKind.GOAL_ATTACH,
            }:
                continue
            parents.append(handle)
        if any(handle.cancellation_requested for handle in parents):
            raise RuntimeError("session execution is being cancelled")
        parent = parents[-1] if parents else None
        stream = self.run_stream(
            session_id, request_id,
            SessionWorkKind.SESSION_INPUT if parent else SessionWorkKind.CHAT_STREAM,
            (lambda: operation(record.channel_id)) if parent else idle_operation,
            suspension_key=suspension_key,
            parent_execution_id=parent.execution_id if parent else None,
        )
        async with aclosing(stream):
            async for item in stream:
                yield item

    async def run_stream(
        self,
        session_id: str,
        request_id: str,
        work_kind: SessionWorkKind,
        operation: Callable[[], AsyncIterator[T] | Awaitable[AsyncIterator[T]]],
        *,
        suspension_key: Callable[[T], str | None] | None = None,
        parent_execution_id: str | None = None,
    ) -> AsyncIterator[T]:
        record = self._require_open_session(session_id)
        handle = self._new_execution(
            record, request_id, work_kind, parent_execution_id=parent_execution_id,
        )
        queue: asyncio.Queue[_StreamItem] = asyncio.Queue(self._stream_buffer_size)

        async def produce() -> None:
            self._registry.mark_running(handle)
            record.state = RuntimeSessionState.ACTIVE
            stream: AsyncIterator[T] | None = None
            terminal_item: _StreamItem | None = None
            try:
                candidate = operation()
                stream = await candidate if inspect.isawaitable(candidate) else candidate
                async for item in stream:
                    self._observe_submission_value(handle, item)
                    control_id = (
                        suspension_key(item) if suspension_key is not None else None
                    )
                    if control_id:
                        self._registry.mark_awaiting_control(handle, control_id)
                        self._refresh_control_gate(record)
                    await queue.put(_StreamItem(value=item))
            except asyncio.CancelledError as exc:
                self._mark_submission_interrupted(handle)
                self._registry.mark_terminal(
                    handle, SessionExecutionState.CANCELLED, error=exc
                )
                terminal_item = _StreamItem(error=exc, done=True)
                raise
            except BaseException as exc:
                self._mark_submission_failed(handle)
                self._registry.mark_terminal(
                    handle, SessionExecutionState.FAILED, error=exc
                )
                terminal_item = _StreamItem(error=exc, done=True)
            else:
                if handle.waiting_control_id:
                    self._registry.mark_waiting(handle)
                else:
                    self._registry.mark_terminal(
                        handle, SessionExecutionState.SUCCEEDED
                    )
                terminal_item = _StreamItem(done=True)
            finally:
                if stream is not None:
                    close = getattr(stream, "aclose", None)
                    if callable(close):
                        await close()
                if handle.task is asyncio.current_task():
                    handle.task = None
                self._refresh_session_state(record)
                # A terminal marker must not be dropped when
                # the bounded buffer is full, otherwise the consumer can wait
                # forever after draining the final data item.
                if terminal_item is not None:
                    await queue.put(terminal_item)

        scheduled = asyncio.create_task(
            self._scheduler.submit_and_wait(handle, produce)
            if work_kind.scheduled
            else produce()
        )
        if not work_kind.scheduled:
            handle.task = scheduled
        completed_normally = False
        try:
            while True:
                item = await queue.get()
                if item.error is not None:
                    raise item.error
                if item.done:
                    completed_normally = True
                    break
                yield item.value
            await scheduled
        finally:
            if not completed_normally and not scheduled.done():
                await self.cancel_execution(
                    session_id,
                    execution_id=handle.execution_id,
                )
            if scheduled.done():
                try:
                    scheduled.result()
                except (asyncio.CancelledError, Exception):
                    pass

    async def cancel_execution(
        self,
        session_id: str,
        *,
        request_id: str | None = None,
        execution_id: str | None = None,
        generation: int | None = None,
        wait_timeout: float | None = None,
    ) -> CancelExecutionResult:
        handles = self._registry.select(
            session_id=session_id,
            request_id=request_id,
            execution_id=execution_id,
            generation=generation,
            active_only=True,
        )
        handles = self._with_descendants(handles)
        direct = [handle for handle in handles if self._requires_direct_cancel(handle)]
        work = [handle for handle in handles if handle.work_kind.scheduled]
        direct_ids = {handle.execution_id for handle in direct}
        for handle in handles:
            if handle.execution_id not in direct_ids:
                handle.cancellation_requested = True
        timed_out = await self._scheduler.cancel_handles(
            work,
            wait_timeout=self._cancel_timeout if wait_timeout is None else wait_timeout,
        )
        direct_timeouts = await self._request_direct_cancel(
            direct,
            wait_timeout=self._cancel_timeout if wait_timeout is None else wait_timeout,
        )
        timed_out = (*timed_out, *direct_timeouts)
        timed_out_set = set(timed_out)
        cancelled = 0
        for handle in handles:
            if handle.execution_id not in timed_out_set and not handle.state.terminal:
                self._registry.mark_terminal(handle, SessionExecutionState.CANCELLED)
            if handle.state is SessionExecutionState.CANCELLED:
                cancelled += 1
        record = self._sessions.get(session_id)
        if record is not None:
            self._refresh_session_state(record)
        return CancelExecutionResult(
            matched=len(handles), cancelled=cancelled, timed_out=timed_out
        )

    async def close_session(
        self,
        session_id: str,
        *,
        generation: int | None = None,
        wait_timeout: float | None = None,
    ) -> CloseSessionResult:
        record = self._sessions.get(session_id)
        if record is None or (generation is not None and record.generation != generation):
            return CloseSessionResult(session_id, generation, False)
        target_generation = record.generation
        record.state = RuntimeSessionState.QUIESCING
        executions = self._registry.select(
            session_id=session_id,
            generation=target_generation,
        )
        active = [handle for handle in executions if not handle.state.terminal]
        settling = []
        for handle in executions:
            if not handle.state.terminal or not handle.retain_owner_task:
                continue
            if handle.task is not None and not handle.task.done():
                settling.append(handle)
        direct = [handle for handle in active if self._requires_direct_cancel(handle)]
        direct_timeouts = await self._request_direct_cancel(
            direct,
            wait_timeout=self._cancel_timeout if wait_timeout is None else wait_timeout,
        )
        scheduler_existed, timed_out = await self._scheduler.close_session(
            session_id,
            generation=target_generation,
            wait_timeout=self._cancel_timeout if wait_timeout is None else wait_timeout,
        )
        if not scheduler_existed:
            for handle in active:
                if not handle.work_kind.scheduled or handle.task is None:
                    continue
                if not handle.task.done():
                    settling.append(handle)
        settling_timeouts = await self._wait_for_handles(
            settling,
            wait_timeout=self._cancel_timeout if wait_timeout is None else wait_timeout,
        )
        timed_out = (*timed_out, *direct_timeouts, *settling_timeouts)
        for handle in self._registry.select(
            session_id=session_id, generation=target_generation, active_only=True
        ):
            handle.cancellation_requested = True
            if handle.execution_id not in timed_out and not handle.state.terminal:
                self._registry.mark_terminal(handle, SessionExecutionState.CANCELLED)
        if timed_out:
            return CloseSessionResult(
                session_id,
                target_generation,
                True,
                tuple(dict.fromkeys(timed_out)),
            )
        if self._sessions.get(session_id) is record:
            record.state = RuntimeSessionState.CLOSED
        return CloseSessionResult(session_id, target_generation, True)

    def get_execution(self, execution_id: str) -> SessionExecutionSnapshot | None:
        handle = self._registry.get(execution_id)
        return None if handle is None else handle.snapshot()

    def snapshot_session(self, session_id: str) -> RuntimeSessionSnapshot | None:
        record = self._sessions.get(session_id)
        return None if record is None else self._snapshot(record)

    async def close(self) -> None:
        async with self._lock:
            self._accepting = False
            records = tuple(self._sessions.values())
        errors: list[BaseException] = []
        timed_out: list[str] = []
        for record in records:
            if record.state is RuntimeSessionState.CLOSED:
                continue
            try:
                result = await self.close_session(
                    record.session_id,
                    generation=record.generation,
                )
                timed_out.extend(result.timed_out)
            except BaseException as exc:
                errors.append(exc)
        if timed_out:
            raise SessionCloseTimeoutError(
                "runtime", tuple(dict.fromkeys(timed_out))
            )
        try:
            await self._scheduler.close(wait_timeout=self._cancel_timeout)
        except BaseException as exc:
            errors.append(exc)
        if errors:
            raise errors[0]

    def _require_open_session(self, session_id: str) -> _SessionRecord:
        from jiuwenswarm.server.runtime.session.lifecycle import guard
        guard(session_id)
        if not self._accepting:
            raise RuntimeError("session coordinator is closed")
        record = self._sessions.get(session_id)
        if record is None:
            raise RuntimeError(f"session is not registered: {session_id}")
        if record.state in {RuntimeSessionState.QUIESCING, RuntimeSessionState.CLOSED}:
            raise RuntimeError(f"session is not accepting work: {session_id}")
        return record

    def _new_execution(
        self,
        record: _SessionRecord,
        request_id: str,
        work_kind: SessionWorkKind,
        *,
        parent_execution_id: str | None = None,
        allow_duplicate_request: bool = False,
    ) -> SessionExecutionHandle:
        normalized_request_id = str(request_id or "")
        if not allow_duplicate_request and normalized_request_id:
            existing = self._registry.select(
                session_id=record.session_id,
                request_id=normalized_request_id,
                generation=record.generation,
            )
            if existing:
                raise SessionRequestDuplicateError(
                    "request was already accepted for this Session generation: "
                    f"{normalized_request_id}"
                )
        superseded_kinds: set[SessionWorkKind] = set()
        if work_kind in {SessionWorkKind.CHAT_UNARY, SessionWorkKind.CHAT_STREAM}:
            superseded_kinds = {
                SessionWorkKind.CHAT_UNARY,
                SessionWorkKind.CHAT_STREAM,
                SessionWorkKind.HEARTBEAT,
            }
        elif work_kind is SessionWorkKind.GOAL_STREAM and not record.external_execution:
            superseded_kinds = {
                SessionWorkKind.GOAL_STREAM,
                SessionWorkKind.GOAL_ATTACH,
            }
        if superseded_kinds:
            for previous in self._registry.select(
                session_id=record.session_id,
                generation=record.generation,
                active_only=True,
            ):
                if (
                    previous.work_kind in superseded_kinds
                    and previous.state is SessionExecutionState.WAITING_FOR_CONTROL
                ):
                    previous.cancellation_requested = True
                    self._registry.mark_terminal(
                        previous, SessionExecutionState.CANCELLED
                    )
        handle = SessionExecutionHandle(
            execution_id=uuid.uuid4().hex,
            session_id=record.session_id,
            request_id=normalized_request_id,
            generation=record.generation,
            work_kind=work_kind,
            parent_execution_id=parent_execution_id,
        )
        self._registry.register(handle)
        self._notify_execution_changed(record)
        record.state = RuntimeSessionState.ACTIVE
        return handle

    @staticmethod
    def _require_generation(
        record: _SessionRecord, generation: int | None
    ) -> None:
        if generation is not None and generation != record.generation:
            raise SessionGenerationMismatchError(
                "interaction answer targets a stale Session generation: "
                f"expected={record.generation} received={generation}"
            )

    def _completed_control(
        self,
        record: _SessionRecord,
        request_id: str,
        *,
        control_fingerprint: str | None,
    ) -> SessionExecutionHandle | None:
        completed = [
            handle
            for handle in self._registry.select(
                session_id=record.session_id,
                request_id=request_id,
                generation=record.generation,
            )
            if handle.work_kind is SessionWorkKind.CONTROL_INPUT
            and handle.control_delivered
        ]
        if not completed:
            return None
        handle = completed[-1]
        if (
            control_fingerprint is not None
            and handle.control_fingerprint is not None
            and control_fingerprint != handle.control_fingerprint
        ):
            raise SessionControlConflictError(
                f"interaction already has a different answer: {request_id}"
            )
        return handle

    def _observe_submission_value(
        self, handle: SessionExecutionHandle, value: object
    ) -> None:
        values = value if isinstance(value, (list, tuple)) else (value,)
        for item in values:
            payload = getattr(item, "payload", None)
            if not isinstance(payload, dict):
                continue
            event_type = str(payload.get("event_type") or "")
            stage = str(payload.get("submission_status") or "")
            message_id = str(payload.get("provider_message_id") or "").strip()
            turn_id = str(payload.get("provider_turn_id") or "").strip()
            if message_id:
                handle.provider_message_id = message_id
            if turn_id:
                handle.provider_turn_id = turn_id
            if stage == SessionSubmissionState.PROVIDER_ACCEPTED.value:
                handle.submission_state = SessionSubmissionState.PROVIDER_ACCEPTED
            elif (
                stage == SessionSubmissionState.HARNESS_ACCEPTED.value
                or event_type == "runtime.accepted"
            ) and handle.submission_state is SessionSubmissionState.HOST_ACCEPTED:
                handle.submission_state = SessionSubmissionState.HARNESS_ACCEPTED
            elif stage == SessionSubmissionState.UNKNOWN.value:
                if handle.submission_state is not SessionSubmissionState.PROVIDER_ACCEPTED:
                    handle.submission_state = SessionSubmissionState.UNKNOWN
            elif (
                not ((record := self._sessions.get(handle.session_id)) is not None and record.external_execution)
                and event_type.startswith(("chat.", "harness.", "goal."))
                and event_type not in {"chat.interrupt_result"}
                and payload.get("terminal_status") != "unknown"
            ):
                handle.submission_state = SessionSubmissionState.PROVIDER_ACCEPTED

    @staticmethod
    def _mark_submission_failed(handle: SessionExecutionHandle) -> None:
        if handle.submission_state is SessionSubmissionState.HOST_ACCEPTED:
            handle.submission_state = SessionSubmissionState.REJECTED
        elif handle.submission_state is SessionSubmissionState.HARNESS_ACCEPTED:
            handle.submission_state = SessionSubmissionState.UNKNOWN

    @staticmethod
    def _mark_submission_interrupted(handle: SessionExecutionHandle) -> None:
        if handle.submission_state is not SessionSubmissionState.PROVIDER_ACCEPTED:
            handle.submission_state = SessionSubmissionState.UNKNOWN

    @staticmethod
    def _requires_direct_cancel(handle: SessionExecutionHandle) -> bool:
        if handle.work_kind is SessionWorkKind.SESSION_MESSAGE:
            return True
        return not handle.work_kind.scheduled

    @staticmethod
    async def _cancel_direct_handles(
        handles: list[SessionExecutionHandle],
        *,
        wait_timeout: float | None,
    ) -> tuple[str, ...]:
        tasks: dict[str, asyncio.Task[Any]] = {}
        current = asyncio.current_task()
        for handle in handles:
            task = handle.task
            if task is None or task.done() or task is current:
                continue
            task.cancel()
            tasks[handle.execution_id] = task
        if not tasks:
            return ()
        _done, pending = await asyncio.wait(set(tasks.values()), timeout=wait_timeout)
        return tuple(
            execution_id
            for execution_id, task in tasks.items()
            if task in pending
        )

    def _control_parent(
        self, record: _SessionRecord, request_id: str
    ) -> SessionExecutionHandle | None:
        deliverable = {
            SessionExecutionState.RUNNING,
            SessionExecutionState.WAITING_FOR_CONTROL,
        }
        parents: list[SessionExecutionHandle] = []
        for handle in self._registry.select(
            session_id=record.session_id,
            generation=record.generation,
            active_only=True,
        ):
            if handle.waiting_control_id != request_id:
                continue
            if handle.state not in deliverable:
                continue
            parents.append(handle)
        if not parents:
            return None
        return max(parents, key=lambda handle: handle.started_at or handle.created_at)

    def _claimed_execution_id(
        self, record: _SessionRecord, request_id: str
    ) -> str | None:
        """Return the execution owning an in-flight control delivery, if any."""
        for (
            claimed_session,
            claimed_generation,
            claimed_execution,
            claimed_request,
        ) in self._control_claims:
            if (
                claimed_session == record.session_id
                and claimed_generation == record.generation
                and claimed_request == request_id
            ):
                return claimed_execution
        return None

    def _is_control_claimed(
        self, record: _SessionRecord, request_id: str
    ) -> bool:
        return self._claimed_execution_id(record, request_id) is not None

    def _claimed_control_parent(
        self, record: _SessionRecord, request_id: str
    ) -> SessionExecutionHandle | None:
        execution_id = self._claimed_execution_id(record, request_id)
        if execution_id is None:
            return None
        return self._registry.get(execution_id)

    def _heartbeat_root(
        self, handle: SessionExecutionHandle
    ) -> SessionExecutionHandle | None:
        current = handle
        seen: set[str] = set()
        while current.execution_id not in seen:
            seen.add(current.execution_id)
            if current.work_kind is SessionWorkKind.HEARTBEAT:
                return current
            if current.parent_execution_id is None:
                return None
            parent = self._registry.get(current.parent_execution_id)
            if (
                parent is None
                or parent.session_id != handle.session_id
                or parent.generation != handle.generation
            ):
                return None
            current = parent
        return None

    def _with_descendants(
        self, handles: list[SessionExecutionHandle]
    ) -> list[SessionExecutionHandle]:
        selected = {handle.execution_id: handle for handle in handles}
        frontier = set(selected)
        while frontier:
            child_ids: set[str] = set()
            scopes = {
                (handle.session_id, handle.generation)
                for handle in selected.values()
            }
            for session_id, generation in scopes:
                for handle in self._registry.select(
                    session_id=session_id,
                    generation=generation,
                    active_only=True,
                ):
                    if (
                        handle.parent_execution_id in frontier
                        and handle.execution_id not in selected
                    ):
                        selected[handle.execution_id] = handle
                        child_ids.add(handle.execution_id)
            frontier = child_ids
        return list(selected.values())

    async def _cancel_descendants(self, parent: SessionExecutionHandle) -> None:
        descendants = [
            handle
            for handle in self._with_descendants([parent])
            if handle.execution_id != parent.execution_id
        ]
        if not descendants:
            return
        timed_out = {handle.execution_id for handle in descendants}
        try:
            timed_out = set(
                await self._request_direct_cancel(
                    descendants,
                    wait_timeout=self._cancel_timeout,
                )
            )
        finally:
            for handle in descendants:
                if handle.execution_id not in timed_out and not handle.state.terminal:
                    self._registry.mark_terminal(
                        handle, SessionExecutionState.CANCELLED
                    )
        if timed_out:
            await self._wait_for_handles_uninterruptibly(descendants)

    @staticmethod
    async def _wait_for_handles(
        handles: list[SessionExecutionHandle],
        *,
        wait_timeout: float | None,
    ) -> tuple[str, ...]:
        tasks = {
            handle.execution_id: handle.task
            for handle in handles
            if handle.task is not None and not handle.task.done()
        }
        if not tasks:
            return ()
        _done, pending = await asyncio.wait(set(tasks.values()), timeout=wait_timeout)
        return tuple(
            execution_id
            for execution_id, task in tasks.items()
            if task in pending
        )

    async def _request_direct_cancel(
        self,
        handles: list[SessionExecutionHandle],
        *,
        wait_timeout: float | None,
    ) -> tuple[str, ...]:
        def explicit_external_retry(handle):
            record = self._sessions.get(handle.session_id)
            return record is not None and record.external_owner == handle.execution_id

        pending = [handle for handle in handles if handle.cancellation_requested and not explicit_external_retry(handle)]
        fresh = [handle for handle in handles if not handle.cancellation_requested or explicit_external_retry(handle)]
        for handle in fresh:
            handle.cancellation_requested = True
        cancelled_timeouts = await self._cancel_direct_handles(
            fresh, wait_timeout=wait_timeout
        )
        pending_timeouts = await self._wait_for_handles(
            pending, wait_timeout=wait_timeout
        )
        return (*cancelled_timeouts, *pending_timeouts)

    @staticmethod
    async def _wait_for_handles_uninterruptibly(
        handles: list[SessionExecutionHandle],
    ) -> None:
        pending = {
            handle.task
            for handle in handles
            if handle.task is not None and not handle.task.done()
        }
        while pending:
            try:
                _done, pending = await asyncio.wait(pending)
            except asyncio.CancelledError:
                continue

    def _refresh_session_state(self, record: _SessionRecord) -> None:
        self._notify_execution_changed(record)
        if self._sessions.get(record.session_id) is not record:
            return
        if record.state in {RuntimeSessionState.QUIESCING, RuntimeSessionState.CLOSED}:
            return
        active = self._registry.select(
            session_id=record.session_id,
            generation=record.generation,
            active_only=True,
        )
        self._refresh_control_gate(record, active)
        record.state = RuntimeSessionState.ACTIVE if active else RuntimeSessionState.READY

    def _refresh_control_gate(
        self,
        record: _SessionRecord,
        active: list[SessionExecutionHandle] | None = None,
    ) -> None:
        if active is None:
            active = self._registry.select(
                session_id=record.session_id,
                generation=record.generation,
                active_only=True,
            )
        if any(handle.waiting_control_id for handle in active):
            record.control_ready.clear()
        else:
            record.control_ready.set()

    @staticmethod
    def _consume_task(task: asyncio.Task[Any]) -> None:
        if not task.done():
            return
        try:
            task.result()
        except (asyncio.CancelledError, Exception):
            pass

    def _snapshot(self, record: _SessionRecord) -> RuntimeSessionSnapshot:
        return RuntimeSessionSnapshot(
            session_id=record.session_id,
            channel_id=record.channel_id,
            persistence_policy=record.persistence_policy,
            generation=record.generation,
            state=record.state,
            executions=self._registry.snapshots_for_session(
                record.session_id, generation=record.generation
            ),
        )
