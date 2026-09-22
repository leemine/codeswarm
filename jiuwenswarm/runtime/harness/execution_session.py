# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Provider-neutral External execution lifecycle owned by one host binding."""

from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path

from openjiuwen.harness.engine import HarnessEngine
from openjiuwen.harness_protocol import (
    HarnessCapability,
    HarnessContext,
    HarnessInput,
    HostCapability,
    SendReceipt,
    ToolGateway,
    UnsupportedHarnessCapabilityError,
)
from openjiuwen.core.session.interaction.interactive_input import InteractiveInput
from openjiuwen.harness_providers.io_adapter import HarnessIOAdapter, ProjectedOutput

from jiuwenswarm.common.runtime_workspace import RuntimeWorkspacePaths
from jiuwenswarm.runtime.harness.output_router import TurnOutputRouter
from jiuwenswarm.runtime.harness.tool_gateway import ProductToolGateway
from jiuwenswarm.runtime.harness.tool_transport import (
    ManagedProductToolTransport,
    PRODUCT_MCP_SERVER_NAME,
)


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
    ) -> None:
        binding = engine.binding
        if str(runtime_paths.runtime_workspace_root.resolve()) != binding.workspace:
            raise ValueError("External runtime workspace does not match the binding")
        self.engine = engine
        self.runtime_paths = runtime_paths
        self.io = HarnessIOAdapter(
            engine.harness,
            auto_approve_tools=False,
            event_observer=event_observer,
        )
        self._detached_output = detached_output
        self._tool_gateway = tool_gateway
        self._tool_transport: ManagedProductToolTransport | None = None
        self._queue_size = max(1, queue_size)
        self._output_router: TurnOutputRouter | None = None
        self._lifecycle_lock = asyncio.Lock()
        self._started = False
        self._closed = False

    @property
    def binding(self):
        return self.engine.binding

    @property
    def started(self) -> bool:
        return self._started

    @property
    def closed(self) -> bool:
        return self._closed

    async def start(self, context: HarnessContext) -> None:
        """Start exactly one Provider cycle for the bound host Session."""
        async with self._lifecycle_lock:
            if self._closed:
                raise RuntimeError("External execution session has been closed")
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
                prepared_context = await self._prepare_tool_context(context)
                await self.io.start(prepared_context)
            except BaseException:
                if self._tool_transport is not None:
                    await self._tool_transport.stop()
                raise
            router = TurnOutputRouter(
                self.io,
                queue_size=self._queue_size,
                detached_output=self._detached_output,
            )
            try:
                router.start()
            except BaseException:
                try:
                    await self.io.stop()
                finally:
                    if self._tool_transport is not None:
                        await self._tool_transport.stop()
                raise
            self._output_router = router
            self._started = True

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

    def outputs(self, turn_id: str) -> AsyncIterator[ProjectedOutput]:
        return self._require_router().outputs(turn_id)

    def abandon_output(self, turn_id: str) -> None:
        self._require_router().abandon(turn_id)

    async def abort(self, *, immediate: bool = False) -> None:
        if self._started and not self._closed:
            await self.io.abort(immediate=immediate)

    async def stop(self) -> None:
        """Idempotently release only resources owned by this Session."""
        async with self._lifecycle_lock:
            if self._closed:
                return
            self._closed = True
            router = self._output_router
            self._output_router = None
            try:
                if router is not None:
                    await router.stop()
            finally:
                try:
                    await self.io.stop()
                finally:
                    if self._tool_transport is not None:
                        await self._tool_transport.stop()
                    self._started = False

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
        if not self._started or self._closed or router is None:
            raise RuntimeError("External execution session is not running")
        return router


__all__ = ["ExecutionSession"]
