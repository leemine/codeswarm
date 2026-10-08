# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Provider-neutral External execution lifecycle owned by one host binding."""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from enum import Enum
from pathlib import Path

from openjiuwen.harness.engine import HarnessEngine
from openjiuwen.harness_protocol import (
    HarnessEvent,
    HarnessCapability,
    HarnessContext,
    HarnessInput,
    HarnessState,
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
logger = logging.getLogger(__name__)


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
        self._native_preflight_bound = False
        self._model_gateway_binding = None
        self._model_authority_for_turn = None
        self._queue_size = max(1, queue_size)
        self._recovery = recovery
        self._output_router: TurnOutputRouter | None = None
        self._lifecycle_lock = asyncio.Lock()
        self._authorization_unconfirmed = False
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

    def owns_governed_provider_session(self, provider_session_id: str) -> bool:
        """Prove this live binding owns the preflight-protected native Session."""
        transport = self._tool_transport
        return bool(
            self.binding.provider_id == "opencode"
            and self._native_preflight_bound
            and self._started and not self._closed
            and self._exit_state is ExecutionExitState.RUNNING
            and transport is not None and transport.started
            and provider_session_id
            and self.engine.harness.provider_session_id == provider_session_id
        )

    def bind_model_gateway(self, binding, authority_for_turn) -> None:
        """Bind a host-selected model once before this original Provider cycle."""
        from jiuwenswarm.governance.model_credentials import ModelCredentialBinding
        if (type(binding) is not ModelCredentialBinding or not callable(authority_for_turn)
                or self.binding.provider_id != 'opencode' or self._model_gateway_binding is not None
                or self._started or self._closed or self._tool_transport is not None):
            raise ValueError('model gateway binding is unavailable')
        self._model_gateway_binding = binding
        self._model_authority_for_turn = authority_for_turn

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

    async def update_authorization(self, authorization, *, runtime_policy=None) -> None:
        from openjiuwen.harness_protocol import WorkspaceAccess

        harness = self.engine.harness
        if not harness.card.supports(HarnessCapability.RUNTIME_AUTHORIZATION):
            raise UnsupportedHarnessCapabilityError("runtime authorization is unsupported")
        if not self._started or self._closed:
            raise RuntimeError("runtime authorization requires a live session")
        self._authorization_unconfirmed = True
        auto_approve = (
            runtime_policy.workspace_access is WorkspaceAccess.FULL_ACCESS
            if runtime_policy is not None
            else authorization.full_access
        )
        self.io.set_tool_auto_approval(auto_approve)
        try:
            await harness.update_authorization(authorization, runtime_policy=runtime_policy)
            if self.engine.harness is not harness or self._closed:
                raise RuntimeError("authorization owner changed")
        except BaseException:
            self.io.set_tool_auto_approval(False)
            raise
        self._authorization_unconfirmed = False

    async def send(
        self, content: HarnessInput, *, immediate: bool = False
    ) -> SendReceipt:
        if self._authorization_unconfirmed:
            raise RuntimeError("runtime authorization is unconfirmed")
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
            raise RuntimeError(
                "External interaction answer unexpectedly created a Turn"
            )
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

    async def stop(self, *, ownership_check: Callable[[], None] | None = None) -> None:
        """Idempotently release only resources owned by this Session."""
        engine, binding, io = self.engine, self.binding, self.io
        router, transport, gateway = self._output_router, self._tool_transport, self._tool_gateway
        recovery = self._recovery

        def check():
            if ownership_check is None:
                return
            if ownership_check() is not None:
                raise RuntimeError("External stop ownership checker must return None")
            if (self.engine is not engine or self.binding is not binding or self.io is not io
                    or self._output_router is not router or self._tool_transport is not transport
                    or self._tool_gateway is not gateway or self._recovery is not recovery):
                raise RuntimeError("External stop resources changed")

        async with self._lifecycle_lock:
            try:
                check()
                if self._closed:
                    if ownership_check is not None and self._exit_state is not ExecutionExitState.EXIT_CONFIRMED:
                        raise RuntimeError("External closed Session exit is unconfirmed")
                    return
                self._exit_state = ExecutionExitState.STOP_REQUESTED
                # Keep the sole event pump/router alive while the original
                # OpenCode abort is reconciled. An abort ACK is not native idle
                # and native idle is not proof that the checkpoint sink saved.
                provider_budget = RESOURCE_STOP_TIMEOUT_S
                cancelled = None
                if ownership_check is not None and binding.provider_id == "opencode" and self._started:
                    began = asyncio.get_running_loop().time()
                    try:
                        async with asyncio.timeout(min(5.0, provider_budget / 2)):
                            check()
                            if io.state is HarnessState.RUNNING:
                                await io.abort(immediate=False)
                                check()
                                while io.state is not HarnessState.IDLE:
                                    await asyncio.sleep(0.01)
                                    check()
                    except asyncio.CancelledError as exc:
                        # The original strict cleanup still owns these resources.
                        # Preserve cancellation after attempting that cleanup.
                        cancelled = exc
                    except Exception:
                        logger.warning("OpenCode abort did not confirm native idle before stop")
                    # In particular, never convert an ownership drift into a
                    # best-effort abort failure and clean a replacement owner.
                    check()
                    provider_budget = max(0.0, provider_budget - (asyncio.get_running_loop().time() - began))
                if ownership_check is None:
                    router, recovery = self._output_router, self._recovery
                    await self._stop_owned_resources(router=router)
                else:
                    await self._stop_owned_resources(
                        router=router, ownership_check=check, provider_timeout=provider_budget,
                    )
                check()
                if recovery is not None:
                    await recovery.clear_pending_interactions()
                    check()
                if cancelled is not None:
                    raise cancelled
            except BaseException:
                self._exit_state = ExecutionExitState.EXIT_UNCONFIRMED
                raise
            self._output_router = None
            self._provider_started_turns.clear()
            self._started = False
            self._closed = True
            self._exit_state = ExecutionExitState.EXIT_CONFIRMED

    async def _stop_owned_resources(
        self,
        *,
        router: TurnOutputRouter | None,
        ownership_check: Callable[[], None] | None = None,
        provider_timeout: float | None = None,
    ) -> None:
        failures: list[tuple[str, Exception]] = []
        # Capture before the first await. A replacement never becomes this stop's resource.
        io, transport, gateway = self.io, self._tool_transport, self._tool_gateway

        def check():
            if ownership_check is not None and ownership_check() is not None:
                raise RuntimeError("External stop ownership checker must return None")

        async def stop_one(name: str, operation: Callable[[], Awaitable[None]]) -> None:
            check()
            try:
                await asyncio.wait_for(
                    operation(),
                    timeout=(provider_timeout if name == "provider" and provider_timeout is not None
                             else RESOURCE_STOP_TIMEOUT_S),
                )
            except Exception as exc:
                failures.append((name, exc))
            check()

        if router is not None:
            await stop_one("output_router", router.stop)
        await stop_one("provider", io.stop)
        if transport is not None:
            await stop_one("product_mcp", transport.stop)
            if getattr(transport, "exit_confirmed", False) is not True:
                failures.append(("product_mcp", RuntimeError("product MCP transport exit is unconfirmed")))
        close_gateway = getattr(gateway, "close", None)
        if callable(close_gateway):
            await stop_one("tool_gateway", close_gateway)
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
        governed_opencode = (
            self.binding.provider_id == "opencode" and context.tool_authorizer is not None
        )
        if self._model_gateway_binding is not None and not governed_opencode:
            raise ValueError('model gateway requires mandatory native authority')
        if governed_opencode:
            return await self._prepare_opencode_preflight(context, gateway)
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
            raise ValueError(
                "External context already defines the product MCP namespace"
            )
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

    async def _prepare_opencode_preflight(self, context, gateway):
        # The endpoint is a live host object, never a persisted Provider option.
        # Import lazily so legacy providers retain their existing dependencies.
        from openjiuwen.harness_providers.opencode import (
            OpenCodeHarness, OpenCodePreflightEndpoint,
        )
        harness = self.engine.harness
        if type(harness) is not OpenCodeHarness:
            raise ValueError("governed OpenCode requires the admitted native harness")
        if context.tools is not None or context.mcp_servers:
            raise ValueError("governed OpenCode has unbound tool sources")
        names = ()
        if gateway is not None:
            if type(gateway) is not ProductToolGateway:
                raise ValueError("governed OpenCode requires a bound product executor")
            self._validate_gateway_scope(gateway)
            definitions = tuple(await gateway.definitions())
            names = tuple(item.name for item in definitions)
            if not callable(getattr(harness, 'consume_product_preflight', None)):
                raise ValueError("governed OpenCode product tools require native call correlation")
        transport = ManagedProductToolTransport(
            gateway, host_session_id=self.binding.host_session_id,
        )
        self._tool_transport = transport
        await transport.start()
        values = transport.bind_native_preflight(harness.authorize_preflight)
        model_gateway = None
        if self._model_gateway_binding is not None:
            original_binding = self.binding
            def source_current(source):
                return (self.binding is original_binding and self.engine.harness is harness
                        and self._tool_transport is transport
                        and self.owns_governed_provider_session(source.session_id)
                        and harness._is_model_source_current(source))
            model_gateway = transport.bind_model_gateway(
                self._model_gateway_binding, execution_binding=original_binding,
                capture_source=harness._capture_model_source, is_source_current=source_current,
                authority_for_turn=self._model_authority_for_turn,
            )
        harness.bind_preflight_endpoint(OpenCodePreflightEndpoint(
            **values, product_tool_names=names,
            **({"model_gateway": model_gateway} if model_gateway is not None else {}),
        ))
        self._native_preflight_bound = True
        if gateway is None:
            return context
        gateway.bind_required_authority(self)
        transport.bind_product_calls(self._product_consumer(harness, gateway, context, definitions))
        return dataclasses.replace(
            context, mcp_servers=(transport.server_config(),),
            host_capabilities=context.host_capabilities | {HostCapability.MCP_SERVERS},
        )

    def _product_consumer(self, harness, gateway, context, definitions):
        import jsonschema
        from openjiuwen.harness_protocol import ToolExecutionResult, ToolInvocation, json_value_to_builtin
        from jiuwenswarm.governance.product_executor import ProductExecutorProof, product_executor_scope
        schemas = {item.name: item.input_schema for item in definitions}
        scope = gateway.scope

        async def consume(name, wire_arguments):
            denied = ToolExecutionResult(content='Product execution authority unavailable', is_error=True)
            if name not in schemas:
                return denied
            operation = harness.consume_product_preflight(name, wire_arguments)
            if operation is None:
                return denied
            try:
                jsonschema.validate(json_value_to_builtin(operation.arguments),
                                    json_value_to_builtin(schemas[name]))
            except (jsonschema.ValidationError, jsonschema.SchemaError):
                return denied
            # Discard wire arguments, including the ticket, before tool dispatch.
            invocation = ToolInvocation(operation.call_id, name, operation.arguments)
            executor = gateway.executor_for(invocation)

            def current():
                return (
                    executor is not None and gateway.executor_for(invocation) is executor
                    and gateway.scope is scope
                    and self.owns_governed_provider_session(operation.provider_session_id)
                    and self.engine.harness is harness and self._tool_gateway is gateway
                    and operation.tool_name == PRODUCT_MCP_SERVER_NAME + '_' + name
                    and harness.is_product_preflight_current(operation)
                )

            if not current():
                return denied
            proof = ProductExecutorProof(
                self, gateway, executor, invocation, operation, current, context.tool_authorizer,
            )
            with product_executor_scope(proof):
                return await gateway.invoke(invocation)

        return consume

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
        if isinstance(gateway, ProductToolGateway):
            scope = gateway.scope
            binding = self.binding
            if (
                scope.subject_id != binding.subject_id
                or scope.host_session_id != binding.host_session_id
                or scope.workspace != binding.workspace
            ):
                raise ValueError("Product ToolGateway scope does not match the binding")
            return

        from openjiuwen.harness.tools.browser_move.playwright_runtime import (
            BrowserExecutionToolGateway,
        )
        from jiuwenswarm.runtime.harness.external_browser_artifacts import (
            ExternalBrowserArtifactGateway,
        )

        if isinstance(
            gateway,
            (BrowserExecutionToolGateway, ExternalBrowserArtifactGateway),
        ):
            identity = gateway.execution_identity
            binding = self.binding
            if (
                identity.instance.subagent_id != binding.host_session_id
                or identity.instance.workspace != binding.workspace
            ):
                raise ValueError("Browser ToolGateway scope does not match the binding")
            return

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
