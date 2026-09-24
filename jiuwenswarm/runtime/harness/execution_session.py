# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Provider-neutral External execution lifecycle owned by one host binding."""

from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import AsyncIterator, Awaitable, Callable
from enum import Enum
from pathlib import Path

from openjiuwen.harness.engine import HarnessEngine
from openjiuwen.harness_protocol import (
    HarnessEvent,
    HarnessCapability,
    HarnessContext,
    HarnessInput,
    HostCapability,
    SendReceipt,
    ToolGateway,
    UnsupportedHarnessCapabilityError,
    TurnEventKind,
    TurnLifecycleEvent,
)
from openjiuwen.core.session.interaction.interactive_input import InteractiveInput
from openjiuwen.harness_providers.io_adapter import HarnessIOAdapter, ProjectedOutput

from jiuwenswarm.common.runtime_workspace import RuntimeWorkspacePaths
from jiuwenswarm.runtime.harness.output_router import TurnOutputRouter
from jiuwenswarm.runtime.harness.recovery_store import SessionExecutionRecovery
from jiuwenswarm.runtime.harness.tool_gateway import ProductToolGateway
from jiuwenswarm.runtime.harness.tool_transport import (
    ManagedProductToolTransport,
    PRODUCT_MCP_SERVER_NAME,
)

RESOURCE_STOP_TIMEOUT_S = 10.0


class ExecutionExitState(str, Enum):
    """Host knowledge about one Session's owned runtime exit."""

    NOT_STARTED = "not_started"
    RUNNING = "running"
    STOP_REQUESTED = "stop_requested"
    EXIT_CONFIRMED = "exit_confirmed"
    EXIT_UNCONFIRMED = "exit_unconfirmed"


class ExecutionExitUnconfirmedError(RuntimeError):
    """One or more Session-owned resources did not confirm exit."""

    code = "EXECUTION_EXIT_UNCONFIRMED"

    def __init__(self, failures: list[tuple[str, Exception]]) -> None:
        self.failures = tuple(failures)
        resources = ", ".join(name for name, _ in failures)
        super().__init__(f"execution exit could not be confirmed: {resources}")


class ExecutionSession:
    """Compose one External engine, IO pump and turn router.

    The Provider remains the only Turn state machine and ``HarnessIOAdapter``
    remains the only event consumer. Product projection and interaction
    bridges are injected by the host and reuse the existing history/UI paths.
    """

    def __init__(
        self,
        engine: HarnessEngine,
        runtime_paths: RuntimeWorkspacePaths,
        *,
        event_observer=None,
        detached_output: Callable[[ProjectedOutput], Awaitable[None]] | None = None,
        tool_gateway: ToolGateway | None = None,
        queue_size: int = 128,
        recovery: SessionExecutionRecovery | None = None,
        auto_approve_tools: bool = False,
    ) -> None:
        binding = engine.binding
        if str(runtime_paths.runtime_workspace_root.resolve()) != binding.workspace:
            raise ValueError("External runtime workspace does not match the binding")
        self.engine = engine
        self.runtime_paths = runtime_paths
        self._event_observer = event_observer
        self._provider_started_turns: set[str] = set()
        self.io = HarnessIOAdapter(
            engine.harness,
            auto_approve_tools=auto_approve_tools,
            event_observer=self._observe_event,
        )
        self._detached_output = detached_output
        self._tool_gateway = tool_gateway
        self._tool_transport: ManagedProductToolTransport | None = None
        self._queue_size = max(1, queue_size)
        self._recovery = recovery
        self._output_router: TurnOutputRouter | None = None
        self._lifecycle_lock = asyncio.Lock()
        self._started = False
        self._closed = False
        self._exit_state = ExecutionExitState.NOT_STARTED

    @property
    def binding(self):
        return self.engine.binding

    @property
    def started(self) -> bool:
        return self._started

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def exit_state(self) -> ExecutionExitState:
        return self._exit_state

    async def start(self, context: HarnessContext) -> None:
        """Start exactly one Provider cycle for the bound host Session."""
        async with self._lifecycle_lock:
            if self._closed:
                raise RuntimeError("External execution session has been closed")
            if self._exit_state in {
                ExecutionExitState.STOP_REQUESTED,
                ExecutionExitState.EXIT_UNCONFIRMED,
            }:
                raise RuntimeError("External execution session cleanup is pending")
            if self._started:
                raise RuntimeError("External execution session is already started")
            binding = self.binding
            if context.host_session_id != binding.host_session_id:
                raise ValueError("External context does not match the bound session")
            if context.cwd is None:
                raise ValueError("External context cwd is required")
            context_cwd = Path(context.cwd).expanduser().resolve()
            if context_cwd != self.runtime_paths.cwd.resolve():
                raise ValueError("External context cwd does not match runtime paths")
            try:
                context_cwd.relative_to(Path(binding.workspace))
            except ValueError as exc:
                raise ValueError("External context cwd is outside the binding") from exc
            try:
                context = self._prepare_recovery_context(context)
                prepared_context = await self._prepare_tool_context(context)
                await self.io.start(prepared_context)
            except BaseException as start_error:
                try:
                    await self._stop_owned_resources(router=None)
                except ExecutionExitUnconfirmedError as cleanup_error:
                    raise cleanup_error from start_error
                raise
            router = TurnOutputRouter(
                self.io,
                queue_size=self._queue_size,
                detached_output=self._detached_output,
                output_observer=self._observe_projected_output,
            )
            try:
                router.start()
            except BaseException as router_error:
                try:
                    await self._stop_owned_resources(router=router)
                except ExecutionExitUnconfirmedError as cleanup_error:
                    raise cleanup_error from router_error
                raise
            self._output_router = router
            self._started = True
            self._exit_state = ExecutionExitState.RUNNING

    async def send(self, content: HarnessInput, *, immediate: bool = False) -> SendReceipt:
        router = self._require_router()
        receipt = await router.submit(
            lambda: self.io.send(content, immediate=immediate)
        )
        if receipt is None:
            raise RuntimeError("External text input did not produce a Turn receipt")
        return receipt

    async def answer(self, content: InteractiveInput) -> bool:
        """Resolve a pending Provider interaction without creating a new Turn."""

        if not self.io.is_pending_interrupt_resume_valid(content):
            return False
        receipt = await self.io.send(content)
        if receipt is not None:
            raise RuntimeError("External interaction answer unexpectedly created a Turn")
        return True

    def provider_started(self, turn_id: str) -> bool:
        """Return exact observation-plane evidence that the Provider began a Turn."""

        return turn_id in self._provider_started_turns

    def forget_submission(self, turn_id: str) -> None:
        self._provider_started_turns.discard(turn_id)

    def outputs(self, turn_id: str) -> AsyncIterator[ProjectedOutput]:
        return self._require_router().outputs(turn_id)

    def abandon_output(self, turn_id: str) -> None:
        self._require_router().abandon(turn_id)

    async def abort(self, *, immediate: bool = False) -> None:
        if (
            self._started
            and not self._closed
            and self._exit_state is ExecutionExitState.RUNNING
        ):
            await self.io.abort(immediate=immediate)

    async def stop(self) -> None:
        """Idempotently release only resources owned by this Session."""
        async with self._lifecycle_lock:
            if self._closed:
                return
            self._exit_state = ExecutionExitState.STOP_REQUESTED
            router = self._output_router
            await self._stop_owned_resources(router=router)
            if self._recovery is not None:
                await self._recovery.clear_pending_interactions()
            self._output_router = None
            self._provider_started_turns.clear()
            self._started = False
            self._closed = True
            self._exit_state = ExecutionExitState.EXIT_CONFIRMED

    async def _stop_owned_resources(
        self,
        *,
        router: TurnOutputRouter | None,
    ) -> None:
        failures: list[tuple[str, Exception]] = []

        async def stop_one(name: str, operation: Callable[[], Awaitable[None]]) -> None:
            try:
                await asyncio.wait_for(
                    operation(),
                    timeout=RESOURCE_STOP_TIMEOUT_S,
                )
            except Exception as exc:
                failures.append((name, exc))

        if router is not None:
            await stop_one("output_router", router.stop)
        await stop_one("provider", self.io.stop)
        if self._tool_transport is not None:
            await stop_one("product_mcp", self._tool_transport.stop)
        if failures:
            self._exit_state = ExecutionExitState.EXIT_UNCONFIRMED
            raise ExecutionExitUnconfirmedError(failures)

    async def _observe_event(self, envelope: HarnessEvent) -> None:
        event = envelope.event
        if (
            envelope.turn_id
            and isinstance(event, TurnLifecycleEvent)
            and event.kind is TurnEventKind.STARTED
        ):
            self._provider_started_turns.add(envelope.turn_id)
        if self._event_observer is not None:
            await self._event_observer(envelope)

    async def _observe_projected_output(self, item: ProjectedOutput) -> None:
        recovery = self._recovery
        if recovery is None:
            return
        chunk = item.chunk
        if chunk is not None and chunk.type == "__interaction__":
            payload = chunk.payload
            request_id = (
                payload.get("id")
                if isinstance(payload, dict)
                else getattr(payload, "id", None)
            )
            if not isinstance(request_id, str) or not request_id:
                raise ValueError("External interaction output has no request id")
            await recovery.mark_pending_interaction(
                request_id,
                turn_id=item.turn_id,
            )
        if item.terminal is not None:
            await recovery.clear_pending_interactions()

    async def _prepare_tool_context(self, context: HarnessContext) -> HarnessContext:
        gateway = self._tool_gateway
        if gateway is None:
            return context
        self._validate_gateway_scope(gateway)
        card = self.engine.harness.card
        capabilities = set(context.host_capabilities)
        if card.supports(HarnessCapability.NATIVE_TOOLS):
            if context.tools is not None and context.tools is not gateway:
                raise ValueError("External context already has a different ToolGateway")
            capabilities.add(HostCapability.NATIVE_TOOL_GATEWAY)
            return dataclasses.replace(
                context,
                tools=gateway,
                host_capabilities=frozenset(capabilities),
            )
        if not card.supports(HarnessCapability.MCP_TOOLS):
            raise UnsupportedHarnessCapabilityError(
                f"{card.name} cannot expose product tools"
            )
        normalized_names = {
            server.name.replace("-", "_") for server in context.mcp_servers
        }
        if PRODUCT_MCP_SERVER_NAME in normalized_names:
            raise ValueError("External context already defines the product MCP namespace")
        transport = ManagedProductToolTransport(
            gateway,
            host_session_id=self.binding.host_session_id,
        )
        self._tool_transport = transport
        await transport.start()
        capabilities.add(HostCapability.MCP_SERVERS)
        return dataclasses.replace(
            context,
            mcp_servers=(*context.mcp_servers, transport.server_config()),
            host_capabilities=frozenset(capabilities),
        )

    def _prepare_recovery_context(self, context: HarnessContext) -> HarnessContext:
        recovery = self._recovery
        if recovery is None:
            return context
        plan = recovery.prepare(self.engine.harness.card, agent_id=context.agent_id)
        capabilities = set(context.host_capabilities)
        capabilities.add(HostCapability.CHECKPOINT_SINK)
        return dataclasses.replace(
            context,
            host_capabilities=frozenset(capabilities),
            resume_policy=plan.resume_policy,
            checkpoint=plan.checkpoint,
            checkpoint_sink=plan.checkpoint_sink,
        )

    def _validate_gateway_scope(self, gateway: ToolGateway) -> None:
        if not isinstance(gateway, ProductToolGateway):
            return
        scope = gateway.scope
        binding = self.binding
        if (
            scope.subject_id != binding.subject_id
            or scope.host_session_id != binding.host_session_id
            or scope.workspace != binding.workspace
        ):
            raise ValueError("Product ToolGateway scope does not match the binding")

    def _require_router(self) -> TurnOutputRouter:
        router = self._output_router
        if (
            not self._started
            or self._closed
            or self._exit_state is not ExecutionExitState.RUNNING
            or router is None
        ):
            raise RuntimeError("External execution session is not running")
        return router


__all__ = [
    "ExecutionExitState",
    "ExecutionExitUnconfirmedError",
    "ExecutionSession",
    "RESOURCE_STOP_TIMEOUT_S",
]
