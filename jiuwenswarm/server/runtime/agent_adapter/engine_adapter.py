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
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.runtime.context import get_current_runtime
from jiuwenswarm.runtime.harness.bridge import prepare_execution_session
from jiuwenswarm.runtime.harness.context_bridge import (
    build_external_context,
    build_external_input,
    cleanup_staged_inputs,
)
from jiuwenswarm.runtime.harness.event_projection import ExternalEventProjection
from jiuwenswarm.runtime.harness.execution_session import (
    ExecutionExitState,
    ExecutionSession,
)
from jiuwenswarm.runtime.harness.external_subagents import (
    ExternalSubagentParentSession,
    ExternalSubagentRuntime,
)
from jiuwenswarm.runtime.harness.external_goal import ExternalGoalRuntime
from jiuwenswarm.runtime.harness.tool_gateway import ProductToolScope
from jiuwenswarm.server.runtime.agent_adapter.goal_control import (
    structured_goal_operation,
    structured_goal_control_kwargs,
    wants_attach_goal,
    tui_goal_operation,
)
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
        self._projection = ExternalEventProjection(
            route.bound.binding.host_session_id,
            on_detached_terminal=self.complete_detached_turn,
        )
        self._start_lock = asyncio.Lock()
        self._personal_context_runtime_enabled = False
        self._parent_session = None
        self._goal_runtime = None
        self._goal_assessor_factory = None
        self._ordinary_owner = None
        self._ordinary_runtime = None
        self._ordinary_turn = None
        self._ordinary_terminal = set()
        self._ordinary_detached = False
        self._ordinary_send_attempted = False
        self._ordinary_request = None
        self._history_release = {}

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
            if self._parent_session is None:
                self._parent_session = ExternalSubagentParentSession(
                    binding.host_session_id,
                    write_output=self._projection.project_product_chunk,
                    recovery=self._route.recovery,
                )
                self._goal_runtime = ExternalGoalRuntime(
                    self,
                    self._parent_session,
                    ProductToolScope(
                        subject_id=binding.subject_id,
                        host_session_id=binding.host_session_id,
                        workspace=binding.workspace,
                    ),
                )
                self._goal_runtime.assessor_factory = self._goal_assessor_factory
            self._subagent_runtime = ExternalSubagentRuntime(
                self._route,
                write_output=self._projection.project_product_chunk,
                parent_session=self._parent_session,
                additional_tools=[
                    *self._goal_runtime.tools(),
                    *self._heartbeat_bridge.build_tools(
                        context=SimpleNamespace(
                            channel_id=self._route.channel_id,
                            session_id=binding.host_session_id,
                            user_id=(
                                ""
                                if binding.subject_id
                                == (
                                    f"{self._route.channel_id}:{binding.host_session_id}"
                                )
                                else binding.subject_id
                            ),
                            metadata={},
                        )
                    ),
                ],
            )
            self._tool_gateway = self._subagent_runtime.gateway

        async def observe(envelope):
            await self._observe(envelope, source_session=session)

        session = prepare_execution_session(
            self._route.source,
            bindings=self._route.bindings,
            subject_id=binding.subject_id,
            host_session_id=binding.host_session_id,
            runtime_paths=self._route.runtime_paths,
            event_observer=observe,
            detached_output=self._projection,
            tool_gateway=self._tool_gateway,
            recovery=self._route.recovery,
        )
        if session.binding is not binding:
            raise RuntimeError(
                "External construction did not retain its admitted binding"
            )
        return session

    def _release_external_owner(self, runtime, owner, request, *, goal=None) -> None:
        if (getattr(request, "_defer_execution_until_history", False)
                and not getattr(request, "_execution_history_complete", False)
                and runtime.holds_external_execution(owner)):
            previous = self._history_release.get(request.request_id)
            self._history_release[request.request_id] = (
                runtime, owner, goal or (previous[2] if previous else None),
            )
            return
        runtime.release_external_execution(owner)
        if goal is not None:
            goal.release_owner(owner)

    async def complete_request_history(self, request) -> None:
        """Release only this request's exited owner after Facade history flush."""
        pending = self._history_release.get(request.request_id)
        if pending is not None:
            from jiuwenswarm.server.runtime.agent_adapter.goal_history import flush_goal_set
            await flush_goal_set(pending[1].session_id)
            self._history_release.pop(request.request_id, None)
            runtime, owner, goal = pending
            runtime.release_external_execution(owner)
            if self._ordinary_owner is owner:
                self._ordinary_owner = self._ordinary_runtime = self._ordinary_turn = None
            if goal is not None:
                goal.release_owner(owner)
        request._execution_history_complete = True

    async def complete_detached_turn(self, turn_id: str) -> None:
        """Called by the original projection only after durable terminal output."""
        if (self._ordinary_owner is None or turn_id != self._ordinary_turn
                or turn_id not in self._ordinary_terminal):
            return
        runtime, owner, request = self._ordinary_runtime, self._ordinary_owner, self._ordinary_request
        if (getattr(request, "_defer_execution_until_history", False)
                and not getattr(request, "_execution_history_complete", False)):
            self._release_external_owner(runtime, owner, request)
            return
        from jiuwenswarm.server.runtime.agent_adapter.goal_history import flush_goal_set
        await flush_goal_set(owner.session_id)
        runtime.release_external_execution(owner)
        if self._ordinary_owner is owner:
            self._ordinary_owner = self._ordinary_runtime = self._ordinary_turn = None

    def needs_goal_assessor(self, request: AgentRequest) -> bool:
        from openjiuwen.harness.goal import GoalStatus

        goal = self._goal_runtime
        if (
            goal is None
            or goal.cold_unconfirmed
            or goal.accounting_unknown
            or goal.parent_session.state_write_failed
            or goal.owner is not None
        ):
            return False
        record = goal.manager.peek()
        return (
            wants_attach_goal(request.params)
            and record is not None
            and record.status is GoalStatus.ACTIVE
        )

    def set_goal_assessor_factory(self, factory, *, request=None) -> None:
        if request is not None:
            request._goal_assessor_factory = factory
            return
        self._goal_assessor_factory = factory
        if self._goal_runtime is not None:
            self._goal_runtime.assessor_factory = factory

    async def _observe(self, envelope, *, source_session=None) -> None:
        from openjiuwen.harness_protocol import TurnLifecycleEvent, TurnEventKind

        if source_session is not self._session:
            return
        if self._goal_runtime is not None:
            self._goal_runtime.observe(envelope, source_session=source_session)
        await self._projection.observe(envelope)
        event = envelope.event
        if self._ordinary_owner is None or not isinstance(event, TurnLifecycleEvent):
            return
        if (
            self._ordinary_turn is None
            and self._ordinary_send_attempted
            and event.kind is TurnEventKind.STARTED
        ):
            self._ordinary_turn = envelope.turn_id
            request = self._ordinary_request
            self._projection.register_turn(
                envelope.turn_id,
                request_id=request.request_id,
                channel_id=request.channel_id,
                mode=str((request.params or {}).get("mode") or "unknown"),
            )
        # FAILED/ABORTED (including synthesized aborts) are not proof that the
        # old Provider resources exited. Retain its permit until explicit stop.
        if (
            event.kind is TurnEventKind.FINISHED
            and self._ordinary_turn == envelope.turn_id
        ):
            self._ordinary_terminal.add(envelope.turn_id)

    async def handle_goal_command_structured(self, params, session_id):
        if session_id != self._route.bound.binding.host_session_id:
            raise ValueError("Goal control Session does not match admitted binding")
        if self._goal_runtime is None:
            return {
                "result_type": "goal_error",
                "error_code": "goal_provider_unsupported",
                "error": "Goal execution requires the admitted product gateway.",
            }
        params = params if isinstance(params, dict) else {}
        operation = structured_goal_control_kwargs(params)
        return await self._goal_runtime.control(operation)

    async def _record_goal_set_history_if_needed(
        self, request, *, action, result_type, goal_payload
    ):
        from jiuwenswarm.server.runtime.agent_adapter.goal_history import (
            record_goal_set,
        )

        await record_goal_set(
            request,
            action=action,
            result_type=result_type,
            goal_payload=goal_payload,
            defer=bool(
                self._ordinary_owner
                or (self._goal_runtime and self._goal_runtime.owner)
            ),
        )

    async def dispatch_goal_control(self, **operation):
        if self._goal_runtime is None:
            raise RuntimeError("External Goal runtime is unavailable")
        return await self._goal_runtime.control(operation)

    def select_execution_for_request(self, request: AgentRequest) -> None:
        route = getattr(request, "_execution_route", None)
        if not isinstance(route, AdmittedExecutionRoute):
            raise RuntimeError("External request has no admitted execution route")
        self.bind_route(route)
        raw_params = getattr(request, "params", None)
        params = raw_params if isinstance(raw_params, dict) else {}
        goal_control = getattr(
            request, "req_method", None
        ) == ReqMethod.COMMAND_GOAL or wants_attach_goal(params)
        if getattr(request, "req_method", None) == ReqMethod.CHAT_SEND:
            goal_control = goal_control or tui_goal_operation(request) is not None
        if not goal_control:
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
        runtime = get_current_runtime()
        params = request.params if isinstance(request.params, dict) else {}
        operation = (
            structured_goal_operation(request)
            if hasattr(request, "req_method")
            else None
        )
        attach = wants_attach_goal(params)
        if (
            operation is None
            and not attach
            and getattr(request, "req_method", None) == ReqMethod.CHAT_SEND
        ):
            operation = tui_goal_operation(request)
        if isinstance(inputs.get("query"), InteractiveInput):
            async for chunk in self._process_message_stream_impl(request, inputs):
                yield chunk
            return
        owner = (
            runtime.external_execution_owner(
                self._route.bound.binding.host_session_id, request.request_id
            )
            if runtime
            else None
        )
        if operation is not None or attach:
            if self._goal_runtime is None or runtime is None or owner is None:
                raise RuntimeError(
                    "External Goal requires its admitted Runtime producer"
                )
            stream = self._goal_runtime.stream(
                request, inputs, runtime=runtime, owner=owner, operation=operation
            )
            try:
                async for chunk in stream:
                    yield chunk
            finally:
                await stream.aclose()
            return
        session = self._require_session()
        if owner is not None:
            await runtime.acquire_external_execution(owner, goal=False)
            self._ordinary_owner, self._ordinary_runtime = owner, runtime
            self._ordinary_turn = None
            self._ordinary_terminal.clear()
            self._ordinary_detached = False
            self._ordinary_send_attempted = False
            self._ordinary_request = request
        owns_heartbeat = bool(
            runtime is not None
            and runtime.owns_heartbeat_execution(
                session.binding.host_session_id,
                request.request_id,
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
                self._ordinary_detached = True
                if owner is not None and (
                    completed
                    or owns_heartbeat
                    or not self._ordinary_send_attempted
                    or self._ordinary_turn in self._ordinary_terminal
                ):
                    self._release_external_owner(runtime, owner, request)
                    if self._ordinary_owner is owner:
                        self._ordinary_owner = self._ordinary_runtime = (
                            self._ordinary_turn
                        ) = None

    async def _stop_owned_execution_once(self, session: ExecutionSession) -> None:
        await session.stop()
        if session.exit_state is not ExecutionExitState.EXIT_CONFIRMED:
            raise RuntimeError("External stop did not confirm execution exit")
        await self.release_subagent_runtime_for_session(
            session.binding.host_session_id, reason="parent_ended"
        )
        if self._ordinary_owner is not None and session is self._session:
            self._release_external_owner(self._ordinary_runtime, self._ordinary_owner, self._ordinary_request)
            self._ordinary_owner = self._ordinary_runtime = self._ordinary_turn = None
        if self._owns_tool_gateway:
            self._tool_gateway = None
        self._heartbeat_stopped_session = session

    async def _stop_heartbeat_execution(self, session: ExecutionSession) -> None:
        while True:
            stop = asyncio.create_task(self._stop_owned_execution_once(session))
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
        self,
        request: AgentRequest,
        inputs: dict[str, Any],
        *,
        goal_attempt=None,
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
        if goal_attempt is None and self._ordinary_owner is not None:
            self._ordinary_send_attempted = True
        receipt = await session.send(external_input, immediate=immediate)
        if goal_attempt is not None:
            goal_attempt.bind_turn(receipt.turn_id)
        elif self._ordinary_owner is not None:
            self._ordinary_turn = receipt.turn_id
        try:
            self._projection.register_turn(
                receipt.turn_id,
                request_id=request.request_id,
                channel_id=request.channel_id,
                mode=str(params.get("mode") or "unknown"),
                goal_attempt=goal_attempt is not None,
                goal_id=goal_attempt.attempt.identity.goal_id if goal_attempt else None,
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
                if goal_attempt is not None:
                    raise RuntimeError(
                        "Goal output ended without a confirmed Provider terminal"
                    )
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
        params = request.params if isinstance(request.params, dict) else {}
        intent = str(params.get("intent") or "cancel").strip().lower()
        success = True
        message = "任务已取消"
        try:
            goal = self._goal_runtime
            if self._ordinary_owner is not None and intent not in {"pause", "resume"}:
                await self._stop_owned_execution_once(self._session)
                if goal is not None and goal.owner is not None:
                    await goal.runtime.request_external_execution_cancel(goal.owner)
            elif (
                goal is not None
                and goal.owner is not None
                and intent not in {"pause", "resume"}
            ):
                if goal.attempt is not None:
                    await goal.cancel_attempt(
                        goal_id=goal.attempt.identity.goal_id, reason="user_cancel"
                    )
                else:
                    await goal.runtime.request_external_execution_cancel(goal.owner)
            elif intent == "pause":
                session = self._require_session()
                await session.io.pause()
                message = "任务已暂停"
            elif intent == "resume":
                await self._require_session().io.resume()
                message = "任务已恢复"
            else:
                await self._require_session().abort(immediate=True)
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
        if session is not None:
            await self._stop_owned_execution_once(session)
            self._session = None
        else:
            await self.release_subagent_runtime_for_session(
                session_id, reason="parent_ended"
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
        if session is not None:
            await self._stop_owned_execution_once(session)
            self._session = None
        else:
            await self.release_subagent_runtime_for_session(
                self._route.bound.binding.host_session_id, reason="parent_ended"
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

    def _external_context(self):
        binding = self._route.bound.binding
        return build_external_context(
            paths=self._route.runtime_paths,
            host_session_id=binding.host_session_id,
            channel_id=self._route.channel_id,
            provider_id=binding.provider_id,
        )

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
