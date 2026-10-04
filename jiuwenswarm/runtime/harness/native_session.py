# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Native Single host boundary over the shared provider lifecycle.

Only transient Python request references live here. Configuration, event
serialization and durable history never contain permission handoff objects.
"""

from __future__ import annotations

import asyncio
import uuid
from collections import deque
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, AsyncIterator, Awaitable, Callable

from openjiuwen.core.session.interaction.interactive_input import InteractiveInput
from openjiuwen.harness.engine import HarnessEngine
from openjiuwen.harness.schema.interaction import InputDispatchMode, SendInputRequest
from openjiuwen.harness_protocol import (
    DeliveryMode,
    HarnessContext,
    HarnessEvent,
    HarnessInput,
    HarnessStateError,
    SendReceipt,
    TurnEventKind,
    TurnLifecycleEvent,
    UnsupportedHarnessCapabilityError,
)
from openjiuwen.harness_providers.io_adapter import HarnessIOAdapter, ProjectedOutput
from openjiuwen.harness_providers.native import (
    AgentFactory,
    DeepAgentHarness,
    NativeHostHooks,
)

from jiuwenswarm.runtime.harness.binding_store import BoundExecution
from jiuwenswarm.runtime.harness.execution_session import (
    ExecutionExitState,
    ExecutionExitUnconfirmedError,
    RESOURCE_STOP_TIMEOUT_S,
)
from jiuwenswarm.runtime.harness.output_router import TurnOutputRouter

if TYPE_CHECKING:
    from openjiuwen.harness.deep_agent import DeepAgent

_REQUEST_KEY = "native.host_request"
_TERMINAL = {TurnEventKind.FINISHED, TurnEventKind.FAILED, TurnEventKind.ABORTED}


@dataclass(slots=True)
class _HostRequest:
    request: SendInputRequest | None = field(default=None, repr=False)
    goal: Callable[[Any], Awaitable[dict[str, Any]]] | None = field(
        default=None, repr=False
    )
    attach_goal: bool = False
    result: asyncio.Future | None = field(default=None, repr=False)
    resumes: list[SendInputRequest] = field(default_factory=list, repr=False)
    answered: set[str] = field(default_factory=set, repr=False)
    authority: Any = field(default=None, repr=False)
    guarded_authority: Any = field(default=None, repr=False)
    model_authority: Any = field(default=None, repr=False)
    guarded_model_authority: Any = field(default=None, repr=False)
    mcp_authority: Any = field(default=None, repr=False)
    guarded_mcp_authority: Any = field(default=None, repr=False)
    control_owner: Any = field(default=None, repr=False)
    control_terminal: Callable | None = field(default=None, repr=False)


class NativeExecutionSession:
    """Own a prepared Native instance through one protocol harness and IO pump.

    The supplied factory returns an unstarted, fully assembled Native agent.
    Session setup and dispatch guards remain supplied by the existing host.
    No running legacy loop or output lease may be adopted.
    """

    def __init__(
        self,
        bound: BoundExecution,
        *,
        agent_factory: AgentFactory,
        session_factory: Callable[[HarnessContext, DeepAgent], Awaitable[Any]],
        before_start: Callable[[DeepAgent, Any], Awaitable[None]] | None = None,
        after_stop: Callable[[], None] | None = None,
        dispatch_guard: Callable[..., Awaitable[Any]] | None = None,
        goal_dispatcher: Callable[..., Awaitable[dict[str, Any]]] | None = None,
        event_observer: Callable[[HarnessEvent], Awaitable[None]] | None = None,
    ) -> None:
        bound.binding.validate_spec(bound.spec)
        if bound.spec.provider_id != "native":
            raise ValueError("Native host assembly requires provider_id=native")
        # This host route already assembles the ordinary Single agent. An
        # explicit normal profile selects that same behavior; retain the
        # original spec/fingerprint instead of rewriting the user's binding.
        if bound.spec.provider_config or bound.spec.requested_mode not in (None, "normal"):
            raise ValueError(
                "Native host assembly uses its existing config; provider overrides are not supported"
            )
        if bound.spec.authorization is not None:
            raise UnsupportedHarnessCapabilityError(
                "Native host assembly does not support explicit execution authorization"
            )
        self._after_stop = after_stop
        self._dispatch_guard = dispatch_guard
        self._goal_dispatcher = goal_dispatcher
        self._observer = event_observer
        self._requests: dict[str, _HostRequest] = {}
        self._turn_requests: dict[str, str] = {}
        self._goal_handoffs: set[str] = set()
        self._terminal_turns: deque[str] = deque(maxlen=16)
        self._closing = False
        self._closed = False
        self._exit_state = ExecutionExitState.NOT_STARTED
        self._lifecycle_lock = asyncio.Lock()
        self._tool_owner = None
        self._resource_governed = False
        self._model_resource_governed = False
        self._mcp_resource_governed = False

        async def register_owner(instance, session):
            if self._tool_owner is not None:
                raise RuntimeError("Native tool owner already registered")
            if before_start is not None:
                await before_start(instance, session)
            inner = instance.react_agent
            if inner is None or session.get_session_id() != bound.binding.host_session_id:
                raise RuntimeError("Native tool owner does not match the Binding")
            self._tool_owner = (instance, inner, session, bound.binding)

        hooks = NativeHostHooks(
            create_session=session_factory,
            before_start=register_owner,
            dispatch_input=self._dispatch,
        )
        harness = DeepAgentHarness(
            agent_factory,
            session_id=bound.binding.host_session_id,
            host_hooks=hooks,
            observe_tools=False,
            preserve_output_chunks=True,
        )
        self._native = harness
        self.engine = HarnessEngine(bound.binding, harness)
        self.io = HarnessIOAdapter(
            harness,
            preserve_native_chunks=True,
            auto_approve_tools=False,
            event_observer=self._observe,
        )
        self._output_router: TurnOutputRouter | None = None

    async def start(self, context: HarnessContext) -> None:
        async with self._lifecycle_lock:
            if self._closing or self._closed:
                raise RuntimeError("Native execution session has been closed")
            if self._exit_state in {
                ExecutionExitState.STOP_REQUESTED,
                ExecutionExitState.EXIT_UNCONFIRMED,
            }:
                raise RuntimeError("Native execution session cleanup is pending")
            binding = self.engine.binding
            if (
                context.host_session_id != binding.host_session_id
                or context.cwd is None
            ):
                raise ValueError(
                    "Native context must match the bound session and workspace"
                )
            if str(Path(context.cwd).resolve()) != binding.workspace:
                raise ValueError("Native context workspace does not match the binding")
            from jiuwenswarm.governance.tool_context import current_tool_authorizer, native_authority_source_scope, submitted_model_authorizer
            self._model_resource_governed = self._model_resource_governed or submitted_model_authorizer() is not None
            self._resource_governed = self._resource_governed or current_tool_authorizer() is not None
            try:
                with native_authority_source_scope(
                    self._current_resource_authority, model_source=self._current_model_authority,
                    slice_source=self._execution_slice_for,
                ):
                    await self.io.start(context)
            except BaseException as start_error:
                try:
                    await asyncio.wait_for(
                        self.io.stop(), timeout=RESOURCE_STOP_TIMEOUT_S
                    )
                    if self._after_stop is not None:
                        self._after_stop()
                except Exception as cleanup_error:
                    self._exit_state = ExecutionExitState.EXIT_UNCONFIRMED
                    raise ExecutionExitUnconfirmedError(
                        [("provider", cleanup_error)]
                    ) from start_error
                self._tool_owner = None
                raise
            self._exit_state = ExecutionExitState.RUNNING

    def owns_tool_session(self, execution, agent, session) -> bool:
        """Exact objects owned by this live Native Binding, never an ID prefix."""
        owner = self._tool_owner
        if owner is None or self._closing or self._closed or self._exit_state is not ExecutionExitState.RUNNING:
            return False
        outer, inner, actual_session, binding = owner
        return (
            agent is inner and session is actual_session
            and outer.react_agent is inner and self.engine.binding is binding
            and execution.provider_id == "native"
            and execution.session_id == binding.host_session_id
            and execution.identity.subject_id == binding.subject_id
            and str(Path(execution.workspace).resolve()) == binding.workspace
        )

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def exit_state(self) -> ExecutionExitState:
        return self._exit_state

    async def stop(self) -> None:
        async with self._lifecycle_lock:
            if self._closed:
                return
            self._closing = True
            self._exit_state = ExecutionExitState.STOP_REQUESTED
            failures: list[tuple[str, Exception]] = []

            async def stop_one(
                name: str, operation: Callable[[], Awaitable[None]]
            ) -> None:
                try:
                    await asyncio.wait_for(
                        operation(), timeout=RESOURCE_STOP_TIMEOUT_S
                    )
                except Exception as exc:
                    failures.append((name, exc))

            if self._output_router is not None:
                await stop_one("output_router", self._output_router.stop)
            await stop_one("provider", self.io.stop)
            if failures:
                self._exit_state = ExecutionExitState.EXIT_UNCONFIRMED
                raise ExecutionExitUnconfirmedError(failures)
            try:
                if self._after_stop is not None:
                    self._after_stop()
            except Exception as exc:
                self._exit_state = ExecutionExitState.EXIT_UNCONFIRMED
                raise ExecutionExitUnconfirmedError([("host_resources", exc)]) from exc
            for entry in self._requests.values():
                if entry.result is not None and not entry.result.done():
                    entry.result.cancel()
            self._requests.clear()
            self._turn_requests.clear()
            self._goal_handoffs.clear()
            self._terminal_turns.clear()
            self._tool_owner = None
            self._closed = True
            self._exit_state = ExecutionExitState.EXIT_CONFIRMED

    def enable_turn_outputs(
        self,
        *,
        queue_size: int = 128,
        detached_output: Callable[[ProjectedOutput], Awaitable[None]] | None = None,
    ) -> None:
        """Select one session-level output reader before sending the first Turn."""
        if self._closing or self._output_router is not None:
            raise RuntimeError("Native turn output route is already selected or closed")
        if self._turn_requests or self._terminal_turns:
            raise RuntimeError("Native turn output route must be selected before input")
        self._output_router = TurnOutputRouter(
            self.io, queue_size=queue_size, detached_output=detached_output
        )
        self._output_router.start()

    def has_turn_output_owner(self, turn_id: str) -> bool:
        return self._output_router is not None and self._output_router.has_owner(turn_id)

    def request_id_for_turn(self, turn_id: str) -> str | None:
        """Return the original host request ID while a Native Turn is active."""
        token = self._turn_requests.get(turn_id)
        entry = self._requests.get(token)
        return entry.request.request_id if entry and entry.request else None

    def bind_turn_control_owner(self, turn_id, request_id, owner, on_terminal):
        """Observe the original Turn through its existing sole event reader."""
        active = self._native.active_turn
        token = self._turn_requests.get(turn_id)
        entry = self._requests.get(token)
        if (self._closing or self._closed or active is None or active.turn_id != turn_id
                or active.abort_requested or entry is None or entry.request is None
                or entry.request.request_id != request_id
                or active.content.metadata.get(_REQUEST_KEY) != token
                or not callable(on_terminal)):
            raise RuntimeError("original Native control Turn unavailable")
        if entry.control_owner is not None:
            if entry.control_owner is not owner:
                raise RuntimeError("Native control owner cannot be replaced")
            return
        entry.control_owner = owner
        entry.control_terminal = on_terminal

    def turn_outputs(self, turn_id: str) -> AsyncIterator[ProjectedOutput]:
        """Read one finite Turn; closing this iterator keeps the session alive."""
        if self._output_router is None:
            raise RuntimeError("Native turn output route is not selected")
        return self._output_router.outputs(turn_id)

    def abandon_turn_output(self, turn_id: str) -> None:
        """Release a request mailbox after admission or transport failure."""
        if self._output_router is not None:
            self._output_router.abandon(turn_id)

    async def _send(self, content: HarnessInput, *, immediate: bool = False) -> SendReceipt:
        if self._output_router is None:
            return await self.io.send(content, immediate=immediate)
        return await self._output_router.submit(
            lambda: self.io.send(content, immediate=immediate)
        )

    def _register_host_request(self, **kwargs):
        from jiuwenswarm.governance.tool_context import (
            submitted_tool_authorizer, submitted_model_authorizer, submitted_mcp_authorizer,
        )
        token = uuid.uuid4().hex
        authority = submitted_tool_authorizer()
        model_authority = submitted_model_authorizer()
        mcp_authority = submitted_mcp_authorizer()
        self._mcp_resource_governed = self._mcp_resource_governed or mcp_authority is not None
        self._model_resource_governed = self._model_resource_governed or model_authority is not None
        self._resource_governed = self._resource_governed or authority is not None
        entry = _HostRequest(**kwargs, authority=authority, model_authority=model_authority,
                             mcp_authority=mcp_authority)

        async def guarded(operation):
            active = self._native.active_turn
            def current():
                return (
                    not self._closing and not self._closed and active is not None
                    and self._native.active_turn is active and not active.abort_requested
                    and active.content.metadata.get(_REQUEST_KEY) == token
                    and self._requests.get(token) is entry
                )
            if authority is None or not current():
                return False
            return await authority(operation) is True and current()

        async def guarded_model(binding, target, *, model_entry_fingerprint=None):
            from jiuwenswarm.governance.resources import ResourceAccessDenied
            active = self._native.active_turn
            def current():
                return (
                    not self._closing and not self._closed and active is not None
                    and self._native.active_turn is active and not active.abort_requested
                    and active.content.metadata.get(_REQUEST_KEY) == token
                    and self._requests.get(token) is entry
                )
            if model_authority is None or not current():
                raise ResourceAccessDenied("model execution authority unavailable")
            kwargs = ({'model_entry_fingerprint': model_entry_fingerprint}
                      if model_entry_fingerprint is not None else {})
            headers = await model_authority(binding, target, native_session=self, **kwargs)
            if not current():
                raise ResourceAccessDenied("model execution authority changed")
            return headers

        def guarded_mcp(binding, *, executor_binding, actual_operation, source_execution, execution_slice, native_session):
            from jiuwenswarm.governance.resources import ResourceAccessDenied
            active = self._native.active_turn
            def current():
                return (
                    not self._closing and not self._closed and active is not None
                    and self._native.active_turn is active and not active.abort_requested
                    and active.content.metadata.get(_REQUEST_KEY) == token
                    and self._requests.get(token) is entry
                    and native_session is self
                    and execution_slice.owner is self and execution_slice.active
                    and execution_slice.mcp_authorizer is guarded_mcp
                    and self._exit_state is ExecutionExitState.RUNNING
                    and execution_slice._task is not None
                    and not execution_slice._task.done() and not execution_slice._task.cancelling()
                )
            if mcp_authority is None or not current():
                raise ResourceAccessDenied('MCP execution authority unavailable')
            result = mcp_authority(binding, executor_binding=executor_binding,
                actual_operation=actual_operation, source_execution=source_execution,
                execution_slice=execution_slice, native_session=self,
                is_current_host_request=current)
            if not current():
                raise ResourceAccessDenied('MCP execution authority changed')
            return result

        entry.guarded_authority = guarded
        entry.guarded_model_authority = guarded_model
        entry.guarded_mcp_authority = guarded_mcp
        self._requests[token] = entry
        return token

    def _current_resource_authority(self):
        from jiuwenswarm.governance.tool_context import _deny_unknown_provider, current_native_execution_slice
        from openjiuwen.harness.execution_subject import current_execution_subject
        bound = current_native_execution_slice()
        if (bound is None or bound.owner is not self or not bound.active
                or bound.subject != current_execution_subject()):
            return _deny_unknown_provider if self._resource_governed else None
        active = self._native.active_turn
        token = active.content.metadata.get(_REQUEST_KEY) if active is not None else None
        entry = self._requests.get(token)
        if self._closing or self._closed or entry is None or active.abort_requested:
            return _deny_unknown_provider if self._resource_governed else None
        if entry.authority is None:
            return _deny_unknown_provider if self._resource_governed else None
        return bound.tool_authorizer or _deny_unknown_provider

    def _current_model_authority(self):
        from jiuwenswarm.governance.tool_context import deny_model_consumption, current_native_execution_slice
        from openjiuwen.harness.execution_subject import current_execution_subject
        bound = current_native_execution_slice()
        if (bound is None or bound.owner is not self or not bound.active
                or bound.subject != current_execution_subject()):
            return deny_model_consumption if self._model_resource_governed else None
        active = self._native.active_turn
        token = active.content.metadata.get(_REQUEST_KEY) if active is not None else None
        entry = self._requests.get(token)
        if (self._closing or self._closed or entry is None or active.abort_requested
                or entry.model_authority is None):
            return deny_model_consumption if self._model_resource_governed else None
        # A late task keeps the original callback, even before its first Model call.
        return bound.model_authorizer or deny_model_consumption

    def _execution_slice_for(self, ctx):
        from jiuwenswarm.governance.tool_context import NativeExecutionSlice
        from jiuwenswarm.governance.resources import ResourceAccessDenied
        from openjiuwen.harness.execution_subject import current_execution_subject
        if not (self._resource_governed or self._model_resource_governed or self._mcp_resource_governed):
            return None
        owner = self._tool_owner
        if owner is None or ctx.agent is not owner[0] or ctx.session is not owner[2]:
            raise ResourceAccessDenied('Native execution slice owner unavailable')
        context = getattr(ctx.inputs, 'run_context', None)
        extra = context.get('extra', context) if isinstance(context, dict) else getattr(context, 'extra', {})
        token = extra.get(_REQUEST_KEY) if isinstance(extra, dict) else None
        active = self._native.active_turn
        entry = self._requests.get(token)
        subject = current_execution_subject()
        if (self._closing or self._closed or self._exit_state is not ExecutionExitState.RUNNING
                or active is None or active.abort_requested
                or active.content.metadata.get(_REQUEST_KEY) != token or entry is None
                or (subject is not None and subject.kind == 'subagent')):
            raise ResourceAccessDenied('Native execution slice request unavailable')
        return NativeExecutionSlice(self, entry.guarded_authority, entry.guarded_model_authority, subject,
                                    mcp_authorizer=entry.guarded_mcp_authority)

    async def send_request(self, request: SendInputRequest) -> SendReceipt:
        """Accept a host request without serializing its permission/context objects."""
        if isinstance(request.inputs.get("query"), InteractiveInput):
            raise ValueError("interrupt answers must use answer_request")
        query = request.inputs.get("query")
        if not isinstance(query, str) or not query.strip():
            raise ValueError("Native user input must be non-empty text")
        token = self._register_host_request(request=request)
        content = HarnessInput(content=query, metadata={_REQUEST_KEY: token})
        try:
            receipt = await self._send(
                content, immediate=request.mode is InputDispatchMode.STEER
            )
        except BaseException:
            self._requests.pop(token, None)
            raise
        # STEER is dispatched immediately and owns no additional Turn entry.
        if token in self._requests:
            self._remember_turn(receipt, token)
        return receipt

    async def answer_request(self, request: SendInputRequest) -> bool:
        """Resume one pending interaction; reject stale/duplicate answers, never resend."""
        query = request.inputs.get("query")
        if not isinstance(
            query, InteractiveInput
        ) or not self.io.is_pending_interrupt_resume_valid(query):
            return False
        active = self._native.active_turn
        token = (
            active.content.metadata.get(_REQUEST_KEY) if active is not None else None
        )
        entry = self._requests.get(token)
        pending = set(self.io.pending_interrupt_ids)
        ids = pending if query.raw_inputs is not None else set(query.user_inputs)
        if (
            entry is None
            or not ids
            or not ids <= pending
            or entry.answered.intersection(ids)
        ):
            return False
        entry.resumes.append(request)
        entry.answered.update(ids)
        try:
            await self.io.send(query)
        except BaseException:
            entry.resumes.remove(request)
            entry.answered.difference_update(ids)
            raise
        return True

    async def submit_goal(
        self, action: str, **kwargs: Any
    ) -> tuple[SendReceipt, asyncio.Future]:
        """Dispatch Goal work in the current Turn, or start one while idle.

        An active Native output lease continues after an in-flight set/resume;
        a fresh Turn attaches its own lease before calling GoalManager. The
        returned future contains the original host control result.
        """
        if action not in {"set", "resume"} or self._goal_dispatcher is None:
            raise ValueError("submit_goal requires a configured set/resume dispatcher")

        async def operation(agent):
            return await self._goal_dispatcher(action=action, **kwargs)

        result = asyncio.get_running_loop().create_future()
        token = self._register_host_request(goal=operation, result=result)
        try:
            receipt = await self._send(
                HarnessInput(content="", metadata={_REQUEST_KEY: token}),
                immediate=True,
            )
        except BaseException:
            self._requests.pop(token, None)
            result.cancel()
            raise
        if self._output_router is not None and receipt.accepted_mode is not DeliveryMode.STEER:
            def release_if_no_stream(done: asyncio.Future) -> None:
                if done.cancelled():
                    self.abandon_turn_output(receipt.turn_id)
                    return
                try:
                    control = done.result()
                except BaseException:
                    self.abandon_turn_output(receipt.turn_id)
                    return
                if not isinstance(control, dict) or control.get("result_type") != "goal_stream":
                    self.abandon_turn_output(receipt.turn_id)

            result.add_done_callback(release_if_no_stream)
        if receipt.accepted_mode is DeliveryMode.STEER:
            self._requests.pop(token, None)
            if result.done() and not result.cancelled() and result.result().get("result_type") == "goal_stream":
                if receipt.turn_id in self._terminal_turns:
                    await self._attach_active_goal_after_eof()
                else:
                    self._goal_handoffs.add(receipt.turn_id)
        else:
            self._remember_turn(receipt, token)
        return receipt, result

    def _remember_turn(self, receipt: SendReceipt, token: str) -> None:
        # A fast terminal event can be observed before send() returns its receipt.
        if receipt.turn_id in self._terminal_turns:
            entry = self._requests.pop(token, None)
            if entry is not None and entry.result is not None and not entry.result.done():
                entry.result.cancel()
            return
        self._turn_requests[receipt.turn_id] = token

    async def control_goal(self, action: str, **kwargs: Any) -> dict[str, Any]:
        """Run get/pause/clear against the original manager, without starting work.

        These controls cannot queue behind the Goal they need to stop. Native
        GoalManager remains their locking/validation authority. In particular,
        get retains its existing nonblocking peek behavior.
        """
        if action not in {"get", "pause", "clear"} or self._goal_dispatcher is None:
            raise ValueError("control_goal only accepts get/pause/clear")
        if self._native.agent is None:
            raise RuntimeError("Native session has not started")
        return await self._goal_dispatcher(action=action, **kwargs)

    async def attach_goal(self) -> SendReceipt:
        """Attach the existing active Goal through the same Turn output route."""
        token = self._register_host_request(attach_goal=True)
        try:
            receipt = await self._send(
                HarnessInput(content="", metadata={_REQUEST_KEY: token})
            )
        except BaseException:
            self._requests.pop(token, None)
            raise
        self._remember_turn(receipt, token)
        return receipt

    async def _dispatch(
        self,
        agent: DeepAgent,
        default_request: SendInputRequest,
        content: HarnessInput,
        resuming: bool,
    ) -> bool:
        token = content.metadata.get(_REQUEST_KEY)
        entry = self._requests.get(token)
        if entry is None:
            raise ValueError(
                "Native host request is missing or belongs to another execution"
            )
        if entry.attach_goal:
            return True
        if entry.goal is not None and not resuming:
            try:
                result = await entry.goal(agent)
            except BaseException:
                entry.result.cancel()
                raise
            if not entry.result.done():
                entry.result.set_result(result)
            return result.get("result_type") == "goal_stream"
        if resuming:
            if not entry.resumes:
                raise ValueError(
                    "Native continuation requires a host-owned resume request"
                )
            request = _combined_resume(entry.resumes, default_request)
            entry.resumes.clear()
            entry.answered.clear()
        else:
            request = entry.request
        if request is None:
            raise ValueError("Native request was already dispatched")

        async def send_if_current(host_request):
            active = self._native.active_turn
            if active is None or active.abort_requested:
                raise RuntimeError(
                    "Native turn was cancelled before host input dispatch"
                )
            if self._resource_governed or self._model_resource_governed or self._mcp_resource_governed:
                inputs = dict(host_request.inputs)
                run = dict(inputs.get('run') or {})
                context = dict(run.get('context') or {})
                context.pop(_REQUEST_KEY, None)
                extra = dict(context.get('extra') or {})
                extra[_REQUEST_KEY] = token
                context['extra'] = extra
                run['context'] = context
                inputs['run'] = run
                host_request = replace(host_request, inputs=inputs)
            await agent.send_input(host_request)

        if self._dispatch_guard is None:
            await send_if_current(request)
        else:
            await self._dispatch_guard(request, send=send_if_current)
        if default_request.mode is InputDispatchMode.STEER:
            self._requests.pop(token, None)
        return True

    async def _observe(self, event: HarnessEvent) -> None:
        if (
            isinstance(event.event, TurnLifecycleEvent)
            and event.event.kind in _TERMINAL
        ):
            self._terminal_turns.append(event.turn_id)
            token = self._turn_requests.pop(event.turn_id, None)
            entry = self._requests.pop(token, None)
            if entry is not None and entry.control_terminal is not None:
                entry.control_terminal(event.event.kind)
            if (
                entry is not None
                and entry.result is not None
                and not entry.result.done()
            ):
                entry.result.cancel()
            if event.turn_id in self._goal_handoffs:
                self._goal_handoffs.discard(event.turn_id)
                if event.event.kind is TurnEventKind.FINISHED:
                    await self._attach_active_goal_after_eof()
        if self._observer is not None:
            await self._observer(event)

    async def _attach_active_goal_after_eof(self) -> None:
        """Keep a replacement Goal running if the previous lease reached EOF."""
        if self._closing or self._goal_dispatcher is None:
            return
        current = await self._goal_dispatcher(action="get")
        if self._closing:
            return
        goal = current.get("goal") if isinstance(current, dict) else None
        if not isinstance(goal, dict) or goal.get("status") != "active":
            return
        token = self._register_host_request(attach_goal=True)
        try:
            receipt = await self.io.send(
                HarnessInput(content="", metadata={_REQUEST_KEY: token})
            )
        except HarnessStateError:
            self._requests.pop(token, None)
            if not self._closing:
                raise
            return
        except BaseException:
            self._requests.pop(token, None)
            raise
        self._remember_turn(receipt, token)


def _combined_resume(
    requests: list[SendInputRequest], default: SendInputRequest
) -> SendInputRequest:
    if len(requests) == 1:
        return requests[0]
    inputs = {}
    query = InteractiveInput()
    for request in requests:
        original = request.inputs["query"]
        if original.raw_inputs is not None:
            # The provider has already associated raw answers with interrupt ids.
            for key, value in default.inputs["query"].user_inputs.items():
                query.update(key, value)
        else:
            for key, value in original.user_inputs.items():
                query.update(key, value)
        for key, value in request.inputs.items():
            if key == "query":
                continue
            if key in inputs and inputs[key] is not value and inputs[key] != value:
                raise ValueError("conflicting host metadata across interrupt answers")
            inputs[key] = value
    inputs["query"] = query
    return SendInputRequest(request_id=requests[-1].request_id, inputs=inputs)
