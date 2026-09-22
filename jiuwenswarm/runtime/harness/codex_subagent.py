# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Codex child executions bound to one admitted External parent."""

from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import Awaitable, Callable

from openjiuwen.harness.subagent_runtime.ports import (
    ChunkCallback,
    ParentExecutionContext,
    ResultCallback,
    SubagentBuildRequest,
    SubagentExecution,
    SubagentTurnRequest,
    SubagentTurnResult,
)
from openjiuwen.harness.subagent_runtime.stream_output import TurnOutputAggregator
from openjiuwen.harness_protocol import HarnessInput, TurnEventKind

from jiuwenswarm.runtime.harness.bridge import prepare_execution_session
from jiuwenswarm.runtime.harness.config_source import ExecutionConfigSource
from jiuwenswarm.runtime.harness.context_bridge import build_external_context
from jiuwenswarm.runtime.harness.execution_session import ExecutionSession
from jiuwenswarm.runtime.harness.request_binding import AdmittedExecutionRoute


_CHILD_SUBJECT_PREFIX = "subagent:"


class CodexSubagentExecution:
    """Drive one independently bound Codex child through the shared session path."""

    def __init__(
        self,
        session: ExecutionSession,
        *,
        parent_session_id: str,
        release: Callable[["CodexSubagentExecution"], Awaitable[None]],
    ) -> None:
        self._session = session
        self._parent_session_id = parent_session_id
        self._release = release
        self._close_lock = asyncio.Lock()
        self._closed = False

    @property
    def binding(self):
        """Expose the immutable child identity for host lifecycle checks."""
        return self._session.binding

    @property
    def closed(self) -> bool:
        return self._closed

    async def run_turn(
        self,
        request: SubagentTurnRequest,
        *,
        on_chunk: ChunkCallback | None = None,
        on_result: ResultCallback,
    ) -> None:
        if self._closed:
            raise RuntimeError("Codex subagent execution is closed")
        receipt = await self._session.send(
            HarnessInput(
                content=request.query,
                metadata={
                    "task_id": request.task_id,
                    "parent_session_id": self._parent_session_id,
                    "subagent_id": self.binding.host_session_id,
                },
            )
        )
        aggregator = TurnOutputAggregator()
        terminal: TurnEventKind | None = None
        try:
            async for item in self._session.outputs(receipt.turn_id):
                if item.chunk is not None:
                    aggregator.consume(item.chunk)
                    if on_chunk is not None:
                        await on_chunk(item.chunk)
                if item.terminal is not None:
                    terminal = item.terminal
        except asyncio.CancelledError:
            abort_task = asyncio.create_task(self._session.abort(immediate=True))
            try:
                await asyncio.shield(abort_task)
            except asyncio.CancelledError:
                await abort_task
            raise

        if terminal is None:
            raise RuntimeError("Codex subagent turn ended without a terminal event")
        failed = terminal is not TurnEventKind.FINISHED or aggregator.is_error()
        error_code = None
        if terminal is TurnEventKind.FAILED:
            error_code = "PROVIDER_TURN_FAILED"
        elif terminal is TurnEventKind.ABORTED:
            error_code = "PROVIDER_TURN_ABORTED"
        await on_result(
            SubagentTurnResult(
                output=(
                    aggregator.output()
                    or ("Codex subagent turn failed" if failed else "")
                ),
                reasoning=aggregator.reasoning_text(),
                is_error=failed,
                error_code=error_code,
            )
        )

    async def close(self, reason: str) -> None:
        del reason
        async with self._close_lock:
            if self._closed:
                return
            self._closed = True
            try:
                await self._session.stop()
            finally:
                await self._release(self)


class CodexSubagentExecutionFactory:
    """Create Codex children from an exact parent route snapshot.

    The child gets a distinct subject, Binding and Provider session.  It uses
    the parent's already admitted Provider configuration and task paths; there
    is deliberately no child Provider/config/cwd override surface.
    """

    def __init__(self, parent_route: AdmittedExecutionRoute) -> None:
        binding = parent_route.bound.binding
        if binding.provider_id != "codex" or parent_route.provider_id != "codex":
            raise ValueError("Codex subagent factory requires a Codex parent")
        if binding.workspace != str(
            parent_route.runtime_paths.runtime_workspace_root.resolve()
        ):
            raise ValueError("Codex parent workspace does not match runtime paths")
        try:
            parent_route.runtime_paths.cwd.resolve().relative_to(
                parent_route.runtime_paths.runtime_workspace_root.resolve()
            )
        except ValueError as exc:
            raise ValueError("Codex parent cwd is outside the admitted workspace") from exc
        binding.validate_spec(parent_route.bound.spec)
        self._parent_route = parent_route
        self._parent_binding = binding
        self._parent_spec = parent_route.bound.spec
        self._child_source = ExecutionConfigSource(explicit=self._parent_spec)
        self._live: dict[str, CodexSubagentExecution] = {}
        self._reserved: set[str] = set()
        self._lock = asyncio.Lock()

    @property
    def parent_binding(self):
        return self._parent_binding

    async def create(
        self,
        request: SubagentBuildRequest,
        context: ParentExecutionContext,
    ) -> SubagentExecution:
        self._validate_parent_context(context)
        self._validate_build_request(request, context)
        async with self._lock:
            existing = self._live.get(request.subagent_id)
            if (
                request.subagent_id in self._reserved
                or existing is not None
                and not existing.closed
            ):
                raise RuntimeError(
                    f"Codex subagent execution already exists: {request.subagent_id}"
                )
            self._reserved.add(request.subagent_id)

        session: ExecutionSession | None = None
        try:
            child_subject_id = f"{_CHILD_SUBJECT_PREFIX}{request.subagent_id}"
            session = prepare_execution_session(
                self._child_source,
                bindings=self._parent_route.bindings,
                subject_id=child_subject_id,
                host_session_id=request.subagent_id,
                runtime_paths=self._parent_route.runtime_paths,
            )
            child_binding = session.binding
            if (
                child_binding is self._parent_binding
                or child_binding.subject_id != child_subject_id
                or child_binding.host_session_id != request.subagent_id
                or child_binding.workspace != self._parent_binding.workspace
                or child_binding.provider_id != self._parent_binding.provider_id
                or child_binding.config_revision != self._parent_binding.config_revision
                or child_binding.fingerprint != self._parent_binding.fingerprint
            ):
                raise RuntimeError("Codex child binding did not inherit the parent scope")

            context_value = build_external_context(
                paths=self._parent_route.runtime_paths,
                host_session_id=request.subagent_id,
                channel_id=self._parent_route.channel_id,
                provider_id="codex",
            )
            child_prompt = (
                f"{context_value.system_prompt}\n"
                "You are a product subagent. Work only on the delegated task; "
                "do not act as the parent execution.\n"
                f"Subagent type: {request.subagent_type}.\n"
                f"Display name: {request.display_name}.\n"
                f"Role: {request.role}."
            )
            context_value = dataclasses.replace(
                context_value,
                agent_name=request.display_name,
                agent_id=child_subject_id,
                system_prompt=child_prompt,
                metadata={
                    **dict(context_value.metadata),
                    "parent_session_id": context.parent_session_id,
                    "parent_subject_id": context.parent_subject_id,
                    "subagent_id": request.subagent_id,
                    "subagent_type": request.subagent_type,
                },
            )
            await session.start(context_value)
            execution = CodexSubagentExecution(
                session,
                parent_session_id=context.parent_session_id,
                release=self._release,
            )
            async with self._lock:
                self._live[request.subagent_id] = execution
            return execution
        except BaseException:
            if session is not None:
                try:
                    await session.stop()
                finally:
                    self._parent_route.bindings.release(session.binding)
            raise
        finally:
            async with self._lock:
                self._reserved.discard(request.subagent_id)

    async def can_restore(
        self,
        request: SubagentBuildRequest,
        context: ParentExecutionContext,
    ) -> bool:
        self._validate_parent_context(context)
        self._validate_build_request(request, context)
        # Durable child checkpoint lookup belongs to R1-04.  Never guess from
        # a parent checkpoint or rebuild through another Provider.
        return False

    def _validate_parent_context(self, context: ParentExecutionContext) -> None:
        if context.parent_session_id != self._parent_binding.host_session_id:
            raise ValueError("Codex child parent Session does not match the binding")
        if context.parent_subject_id != self._parent_binding.subject_id:
            raise ValueError("Codex child parent subject does not match the binding")

    @staticmethod
    def _validate_build_request(
        request: SubagentBuildRequest,
        context: ParentExecutionContext,
    ) -> None:
        expected_prefix = f"{context.parent_session_id}_sub_"
        if not request.subagent_id.startswith(expected_prefix):
            raise ValueError("Codex child identity does not belong to the parent Session")

    async def _release(self, execution: CodexSubagentExecution) -> None:
        binding = execution.binding
        async with self._lock:
            current = self._live.get(binding.host_session_id)
            if current is execution:
                del self._live[binding.host_session_id]
        self._parent_route.bindings.release(binding)


__all__ = ["CodexSubagentExecution", "CodexSubagentExecutionFactory"]
