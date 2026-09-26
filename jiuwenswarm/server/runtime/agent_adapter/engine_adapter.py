# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Shared product adapter for every non-Native harness Provider."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

from openjiuwen.core.session.interaction.interactive_input import InteractiveInput

from openjiuwen.harness_providers.output_buffer import OutputBudgetExceeded, OutputText

from jiuwenswarm.agents.harness.code.rails.heartbeat.tools import HeartbeatRuntimeBridge
from jiuwenswarm.common.schema.agent import (
    AgentRequest,
    AgentResponse,
    AgentResponseChunk,
)
from jiuwenswarm.runtime.context import get_current_runtime
from jiuwenswarm.runtime.harness.bridge import prepare_execution_session
from jiuwenswarm.runtime.harness.context_bridge import (
    build_external_context,
    build_external_input,
    cleanup_staged_inputs,
)
from jiuwenswarm.runtime.harness.event_projection import ExternalEventProjection
from jiuwenswarm.runtime.harness.execution_session import ExecutionExitState, ExecutionSession
from jiuwenswarm.runtime.harness.external_subagents import ExternalSubagentRuntime
from jiuwenswarm.runtime.harness.output_router import TurnOutputIncompleteError
from jiuwenswarm.runtime.harness.request_binding import AdmittedExecutionRoute
from jiuwenswarm.runtime.terminal_outcome import unknown_terminal_payload


logger = logging.getLogger(__name__)


class EngineAgentAdapter:
    """Bind an admitted External execution without constructing DeepAgent.

    Construction stays Provider-neutral; product context, interaction and
    event projection are composed here without constructing a DeepAgent.
    """

    def __init__(self, route: AdmittedExecutionRoute, *, tool_gateway=None) -> None:
        if route.provider_id == "native":
            raise ValueError("EngineAgentAdapter requires an External provider")
        self._route = route
        self._tool_gateway = tool_gateway
        self._owns_tool_gateway = tool_gateway is None
        self._heartbeat_bridge = HeartbeatRuntimeBridge()
        self._subagent_runtime: ExternalSubagentRuntime | None = None
        self._session: ExecutionSession | None = None
        self._heartbeat_stopped_session: ExecutionSession | None = None
        self._projection = ExternalEventProjection(route.bound.binding.host_session_id)
        self._start_lock = asyncio.Lock()
        self._personal_context_runtime_enabled = False

    @property
    def route(self) -> AdmittedExecutionRoute:
        return self._route

    @property
    def execution_session(self) -> ExecutionSession | None:
        return self._session

    def bind_route(self, route: AdmittedExecutionRoute) -> None:
        if (
            route.cache_identity != self._route.cache_identity
            or route.bound.binding is not self._route.bound.binding
        ):
            raise RuntimeError("External adapter route changed")

    async def create_instance(
        self,
        config: dict[str, Any] | None = None,
        *,
        mode: str = "agent",
        sub_mode: str | None = None,
    ) -> None:
        del config, mode, sub_mode
        if self._session is not None:
            raise RuntimeError("External execution instance already exists")
        self._session = self._build_session()

    def _build_session(self) -> ExecutionSession:
        binding = self._route.bound.binding
        if self._tool_gateway is None and self._route.provider_id in {
            "codex",
            "opencode",
        }:
            self._subagent_runtime = ExternalSubagentRuntime(
                self._route,
                write_output=self._projection.project_product_chunk,
                additional_tools=self._heartbeat_bridge.build_tools(
                    context=SimpleNamespace(
                        channel_id=self._route.channel_id,
                        session_id=binding.host_session_id,
                        user_id=(
                            "" if binding.subject_id == (
                                f"{self._route.channel_id}:{binding.host_session_id}"
                            ) else binding.subject_id
                        ),
                        metadata={},
                    )
                ),
            )
            self._tool_gateway = self._subagent_runtime.gateway
        session = prepare_execution_session(
            self._route.source,
            bindings=self._route.bindings,
            subject_id=binding.subject_id,
            host_session_id=binding.host_session_id,
            runtime_paths=self._route.runtime_paths,
            event_observer=self._projection.observe,
            detached_output=self._projection,
            tool_gateway=self._tool_gateway,
            recovery=self._route.recovery,
        )
        if session.binding is not binding:
            raise RuntimeError(
                "External construction did not retain its admitted binding"
            )
        return session

    def select_execution_for_request(self, request: AgentRequest) -> None:
        route = getattr(request, "_execution_route", None)
        if not isinstance(route, AdmittedExecutionRoute):
            raise RuntimeError("External request has no admitted execution route")
        self.bind_route(route)
        self._require_session()

    async def reload_agent_config(
        self,
        config_base: dict[str, Any] | None = None,
        env_overrides: dict[str, Any] | None = None,
        target_session_id: str | None = None,
        reload_scopes: set[str] | None = None,
    ) -> None:
        del config_base, env_overrides, target_session_id, reload_scopes
        # The effective Provider snapshot is immutable for this binding.
        return None

    async def process_message_impl(
        self, request: AgentRequest, inputs: dict[str, Any]
    ) -> AgentResponse:
        content = OutputText()
        terminal_error = None
        terminal_event = None
        try:
            async for chunk in self.process_message_stream_impl(request, inputs):
                payload = chunk.payload if isinstance(chunk.payload, dict) else {}
                if payload.get("event_type") == "chat.error" and terminal_error is None:
                    terminal_error = payload
                if payload.get("terminal_status"):
                    terminal_event = payload
                if payload.get("event_type") in {"chat.delta", "chat.final"}:
                    value = payload.get("content")
                    if isinstance(value, str):
                        if payload.get("event_type") == "chat.final" and value:
                            content.replace(value)
                        else:
                            content.append(value)
            complete_content = content.read()
        finally:
            content.close()
        error = (
            str(terminal_error.get("error") or "External execution failed")
            if terminal_error is not None
            else None
        )
        terminal_fields = (
            {
                key: terminal_event[key]
                for key in ("code", "terminal_status")
                if key in terminal_event
            }
            if terminal_event is not None
            else {}
        )
        return AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=error is None,
            payload={
                "content": complete_content,
                **({"error": error} if error else {}),
                **terminal_fields,
            },
            metadata=request.metadata,
        )

    async def process_message_stream_impl(
        self, request: AgentRequest, inputs: dict[str, Any]
    ) -> AsyncIterator[AgentResponseChunk]:
        session = self._require_session()
        runtime = get_current_runtime()
        owns_heartbeat = bool(
            runtime is not None
            and runtime.owns_heartbeat_execution(
                session.binding.host_session_id, request.request_id,
            )
        )
        completed = False
        stream = self._process_message_stream_impl(request, inputs)
        try:
            async for chunk in stream:
                if chunk.runtime_completion == "completed":
                    completed = True
                yield chunk
        finally:
            try:
                await stream.aclose()
            finally:
                if owns_heartbeat and not completed:
                    # A detached Web reader is intentionally different from a
                    # cancelled Runtime-owned Heartbeat. Neither reader EOF nor
                    # abort/ABORTED proves that its Provider resources exited.
                    await self._stop_heartbeat_execution(session)

    async def _stop_heartbeat_execution(self, session: ExecutionSession) -> None:
        async def stop_owned() -> None:
            # Close the parent and its product transport before clearing the
            # child registry: a live parent could otherwise spawn another child
            # after that registry was released.
            await session.stop()
            if session.exit_state is not ExecutionExitState.EXIT_CONFIRMED:
                raise RuntimeError("Heartbeat stop did not confirm execution exit")
            await self.release_subagent_runtime_for_session(
                session.binding.host_session_id, reason="parent_ended",
            )

        while True:
            stop = asyncio.create_task(stop_owned())
            try:
                while True:
                    try:
                        await asyncio.shield(stop)
                        break
                    except asyncio.CancelledError:
                        # Repeated user/delete/timeout cancellation must not
                        # release the Runtime owner before the shared stop ends.
                        if stop.cancelled():
                            raise RuntimeError("Heartbeat execution stop cancelled")
            except Exception:
                logger.exception("Heartbeat execution exit unconfirmed; retain owner")
                # The original admission timeout reports failure. Keep this
                # exact execution alive; another explicit cancel retries stop.
                try:
                    await asyncio.Future()
                except asyncio.CancelledError:
                    continue
            else:
                # Only this confirmed retired instance may be replaced. The
                # immutable binding, checkpoint recovery and tool scope survive.
                if self._owns_tool_gateway:
                    self._tool_gateway = None
                self._heartbeat_stopped_session = session
                return

    async def _process_message_stream_impl(
        self, request: AgentRequest, inputs: dict[str, Any]
    ) -> AsyncIterator[AgentResponseChunk]:
        session = self._require_session()
        await self._ensure_started(session)
        params = request.params if isinstance(request.params, dict) else {}
        external_input = await build_external_input(
            query=inputs.get("query", ""),
            request_id=request.request_id,
            session_id=session.binding.host_session_id,
            params=params,
            paths=self._route.runtime_paths,
            include_personal_context=self._personal_context_runtime_enabled,
        )
        if isinstance(external_input, InteractiveInput):
            if not await session.answer(external_input):
                raise ValueError("External interaction answer is stale or unknown")
            return

        immediate = str(params.get("input_mode") or "").strip().lower() == "steer"
        receipt = await session.send(external_input, immediate=immediate)
        try:
            self._projection.register_turn(
                receipt.turn_id,
                request_id=request.request_id,
                channel_id=request.channel_id,
                mode=str(params.get("mode") or "unknown"),
            )
        except OutputBudgetExceeded:
            session.abandon_output(receipt.turn_id)
            await asyncio.wait_for(session.abort(), timeout=5)
            raise
        receipt_fields = {
            "provider_message_id": receipt.message_id,
            "provider_turn_id": receipt.turn_id,
        }
        yield AgentResponseChunk(
            request_id=request.request_id,
            channel_id=request.channel_id,
            payload={
                "event_type": "runtime.accepted",
                "request_id": request.request_id,
                "submission_status": "harness_accepted",
                **receipt_fields,
            },
            is_complete=False,
            metadata=dict(request.metadata or {}),
        )
        terminal_seen = False
        budget_failure = None
        provider_acceptance_emitted = False
        try:
            try:
                output = session.outputs(receipt.turn_id)
                async for item in output:
                    provider_started = getattr(session, "provider_started", None)
                    if not provider_acceptance_emitted and (
                        not callable(provider_started)
                        or provider_started(receipt.turn_id)
                    ):
                        provider_acceptance_emitted = True
                        yield AgentResponseChunk(
                            request_id=request.request_id,
                            channel_id=request.channel_id,
                            payload={
                                "event_type": "runtime.accepted",
                                "request_id": request.request_id,
                                "submission_status": "provider_accepted",
                                **receipt_fields,
                            },
                            is_complete=False,
                            metadata=dict(request.metadata or {}),
                        )
                    payload = self._projection.owned_payload(item)
                    if payload is not None:
                        status = str(payload.get("terminal_status") or "")
                        yield AgentResponseChunk(
                            request_id=request.request_id,
                            channel_id=request.channel_id,
                            payload=payload,
                            is_complete=item.terminal is not None,
                            metadata=dict(request.metadata or {}),
                            runtime_completion=(
                                status if item.terminal is not None else None
                            ),
                        )
                    if item.terminal is not None:
                        terminal_seen = True
            except OutputBudgetExceeded as exc:
                budget_failure = exc
                try:
                    await asyncio.wait_for(session.abort(), timeout=5)
                except Exception:
                    pass  # report delivery failure even when abort is unconfirmed
            except TurnOutputIncompleteError:
                pass
            if not terminal_seen:
                provider_started = getattr(session, "provider_started", None)
                if (
                    not provider_acceptance_emitted
                    and callable(provider_started)
                    and provider_started(receipt.turn_id)
                ):
                    provider_acceptance_emitted = True
                    yield AgentResponseChunk(
                        request_id=request.request_id,
                        channel_id=request.channel_id,
                        payload={
                            "event_type": "runtime.accepted",
                            "request_id": request.request_id,
                            "submission_status": "provider_accepted",
                            **receipt_fields,
                        },
                        is_complete=False,
                        metadata=dict(request.metadata or {}),
                    )
                payload = {
                    **unknown_terminal_payload(),
                    **(
                        {"code": budget_failure.code, "error": str(budget_failure)}
                        if budget_failure
                        else {}
                    ),
                    "submission_status": (
                        "provider_accepted"
                        if provider_acceptance_emitted
                        else "unknown"
                    ),
                    **receipt_fields,
                }
                yield AgentResponseChunk(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    payload=payload,
                    is_complete=True,
                    metadata=dict(request.metadata or {}),
                    runtime_completion="unknown",
                )
        finally:
            if not terminal_seen:
                session.abandon_output(receipt.turn_id)
            forget_submission = getattr(session, "forget_submission", None)
            if callable(forget_submission):
                forget_submission(receipt.turn_id)

    async def process_interrupt(self, request: AgentRequest) -> AgentResponse:
        session = self._require_session()
        params = request.params if isinstance(request.params, dict) else {}
        intent = str(params.get("intent") or "cancel").strip().lower()
        success = True
        message = "任务已取消"
        try:
            if intent == "pause":
                await session.io.pause()
                message = "任务已暂停"
            elif intent == "resume":
                await session.io.resume()
                message = "任务已恢复"
            else:
                await session.abort(immediate=True)
                message = "任务已切换" if intent == "supplement" else "任务已取消"
        except Exception as exc:
            success = False
            message = str(exc)
        payload: dict[str, Any] = {
            "event_type": "chat.interrupt_result",
            "intent": intent,
            "success": success,
            "message": message,
        }
        if params.get("new_input"):
            payload["new_input"] = params["new_input"]
        return AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=success,
            payload=payload,
            metadata=request.metadata,
        )

    async def handle_user_answer(self, request: AgentRequest) -> AgentResponse:
        params = request.params if isinstance(request.params, dict) else {}
        interaction = self._interaction_answer(params)
        resolved = bool(
            interaction and await self._require_session().answer(interaction)
        )
        return AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=True,
            payload={"accepted": True, "resolved": resolved},
            metadata=request.metadata,
        )

    async def handle_swarmflow_reply(self, request: AgentRequest) -> AgentResponse:
        del request
        raise RuntimeError("External Single does not route Swarmflow replies")

    async def handle_heartbeat(self, request: AgentRequest) -> AgentResponse | None:
        del request
        return None

    async def abort_on_gateway_disconnect(
        self, *, exclude_session_ids: set[str] | None = None
    ) -> None:
        del exclude_session_ids
        # Releasing a Web request transfers unread output to the detached
        # projection. Connection loss is not a user cancellation.
        return None

    async def cleanup_session_adapter(self, session_id: str) -> bool:
        session = self._session
        if self._route.bound.binding.host_session_id != session_id:
            return False
        if session is None and self._subagent_runtime is None:
            return False
        failures: list[Exception] = []
        try:
            await self.release_subagent_runtime_for_session(
                session_id,
                reason="parent_ended",
            )
        except Exception as exc:
            failures.append(exc)
        if session is not None:
            try:
                await session.stop()
            except Exception as exc:
                failures.append(exc)
            else:
                self._session = None
        if failures:
            raise ExceptionGroup(
                "External Session exits could not be confirmed", failures
            )
        cleanup_staged_inputs(
            self._route.runtime_paths,
            session_id=session_id,
        )
        return True

    async def release_subagent_runtime_for_session(
        self,
        session_id: str | None,
        *,
        reason: str = "parent_ended",
    ) -> None:
        runtime = self._subagent_runtime
        if runtime is None:
            return
        if session_id is not None and (
            str(session_id) != self._route.bound.binding.host_session_id
        ):
            return
        await runtime.close(reason)
        self._subagent_runtime = None

    def has_session_runtime(self, session_id: str | None = None) -> bool:
        session = self._session
        owns_session = session is not None and not session.closed
        owns_subagents = self._subagent_runtime is not None
        if not owns_session and not owns_subagents:
            return False
        return (
            session_id is None
            or self._route.bound.binding.host_session_id == session_id
        )

    async def cleanup(self) -> None:
        session = self._session
        failures: list[Exception] = []
        try:
            await self.release_subagent_runtime_for_session(
                self._route.bound.binding.host_session_id,
                reason="parent_ended",
            )
        except Exception as exc:
            failures.append(exc)
        if session is not None:
            try:
                await session.stop()
            except Exception as exc:
                failures.append(exc)
            else:
                self._session = None
        if failures:
            raise ExceptionGroup(
                "External Session exits could not be confirmed", failures
            )
        cleanup_staged_inputs(
            self._route.runtime_paths,
            session_id=self._route.bound.binding.host_session_id,
        )

    def set_heartbeat_service(self, service: Any | None) -> None:
        """Reuse the AgentServer service; tools retain the admitted parent scope."""
        self._heartbeat_bridge.set_service(service)

    def set_personal_context_runtime_enabled(self, enabled: bool) -> None:
        self._personal_context_runtime_enabled = bool(enabled)

    async def refresh_personal_context_rail(self) -> None:
        # The External bridge reads the fixed publication for each Turn.
        return None

    def _require_session(self) -> ExecutionSession:
        session = self._session
        if session is not None and session is self._heartbeat_stopped_session:
            # Construct lazily through the original bridge. Failed construction
            # retains the retired instance so the next request can retry safely.
            replacement = self._build_session()
            self._session = session = replacement
            self._heartbeat_stopped_session = None
        if session is None or session.closed:
            raise RuntimeError("External execution session is unavailable")
        if getattr(session, "exit_state", None) in {
            ExecutionExitState.STOP_REQUESTED, ExecutionExitState.EXIT_UNCONFIRMED,
        }:
            raise RuntimeError("External execution session cleanup is pending")
        return session

    async def _ensure_started(self, session: ExecutionSession) -> None:
        if session.started:
            return
        async with self._start_lock:
            if session.started:
                return
            binding = session.binding
            await session.start(
                build_external_context(
                    paths=self._route.runtime_paths,
                    host_session_id=binding.host_session_id,
                    channel_id=self._route.channel_id,
                    provider_id=binding.provider_id,
                )
            )

    @staticmethod
    def _interaction_answer(params: dict[str, Any]) -> InteractiveInput | None:
        request_id = str(params.get("request_id") or "").strip()
        answers = params.get("answers")
        if not request_id or not isinstance(answers, list):
            return None
        source = str(params.get("source") or "").strip()
        interactive = InteractiveInput()
        if source == "ask_user_interrupt":
            values: dict[str, Any] = {}
            for raw in answers:
                if not isinstance(raw, dict):
                    continue
                question = str(raw.get("question") or "").strip()
                selected = raw.get("selected_options")
                custom = str(raw.get("custom_input") or "").strip()
                value: Any = custom
                if isinstance(selected, list):
                    cleaned = [
                        str(item).strip()
                        for item in selected
                        if str(item).strip() and str(item).strip() != "Other"
                    ]
                    value = (
                        [*cleaned, custom]
                        if cleaned and custom
                        else cleaned[0]
                        if len(cleaned) == 1
                        else cleaned or custom
                    )
                if question and value:
                    values[question] = value
            interactive.update(request_id, {"answers": values})
            return interactive

        answer = answers[0] if answers and isinstance(answers[0], dict) else {}
        selected = answer.get("selected_options") if isinstance(answer, dict) else []
        value = (
            str(selected[0] if isinstance(selected, list) and selected else "")
            .strip()
            .lower()
        )
        custom = (
            str(answer.get("custom_input") or "").strip()
            if isinstance(answer, dict)
            else ""
        )
        allowed = value in {
            "allow_once",
            "allow",
            "approve",
            "approved",
            "本次允许",
            "allow once",
            "session_allow",
            "always_allow",
            "总是允许",
            "always allow",
        }
        interactive.update(
            request_id,
            {
                "approved": allowed,
                "auto_confirm": value
                in {"session_allow", "always_allow", "总是允许", "always allow"},
                "feedback": custom or ("" if allowed else "用户拒绝"),
            },
        )
        return interactive


__all__ = ["EngineAgentAdapter"]
