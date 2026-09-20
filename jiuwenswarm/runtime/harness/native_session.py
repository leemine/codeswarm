# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Native Single host boundary over the shared provider lifecycle.

Only transient Python request references live here. Configuration, event
serialization and durable history never contain permission handoff objects.
"""

from __future__ import annotations

import asyncio
import uuid
from collections import deque
from dataclasses import dataclass, field
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
)
from openjiuwen.harness_providers.io_adapter import HarnessIOAdapter, ProjectedOutput
from openjiuwen.harness_providers.native import (
    AgentFactory,
    DeepAgentHarness,
    NativeHostHooks,
)

from jiuwenswarm.runtime.harness.binding_store import BoundExecution
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
        dispatch_guard: Callable[..., Awaitable[Any]] | None = None,
        goal_dispatcher: Callable[..., Awaitable[dict[str, Any]]] | None = None,
        event_observer: Callable[[HarnessEvent], Awaitable[None]] | None = None,
    ) -> None:
        bound.binding.validate_spec(bound.spec)
        if bound.spec.provider_id != "native":
            raise ValueError("Native host assembly requires provider_id=native")
        if bound.spec.provider_config or bound.spec.requested_mode is not None:
            raise ValueError(
                "Native host assembly uses its existing config; provider overrides are not supported"
            )
        self._dispatch_guard = dispatch_guard
        self._goal_dispatcher = goal_dispatcher
        self._observer = event_observer
        self._requests: dict[str, _HostRequest] = {}
        self._turn_requests: dict[str, str] = {}
        self._goal_handoffs: set[str] = set()
        self._terminal_turns: deque[str] = deque(maxlen=16)
        self._closing = False
        hooks = NativeHostHooks(
            create_session=session_factory,
            before_start=before_start,
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
        if self._closing:
            raise RuntimeError("Native execution session has been closed")
        binding = self.engine.binding
        if context.host_session_id != binding.host_session_id or context.cwd is None:
            raise ValueError(
                "Native context must match the bound session and workspace"
            )
        if str(Path(context.cwd).resolve()) != binding.workspace:
            raise ValueError("Native context workspace does not match the binding")
        await self.io.start(context)

    async def stop(self) -> None:
        self._closing = True
        if self._output_router is not None:
            await self._output_router.stop()
        await self.io.stop()
        for entry in self._requests.values():
            if entry.result is not None and not entry.result.done():
                entry.result.cancel()
        self._requests.clear()
        self._turn_requests.clear()
        self._goal_handoffs.clear()
        self._terminal_turns.clear()

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

    async def send_request(self, request: SendInputRequest) -> SendReceipt:
        """Accept a host request without serializing its permission/context objects."""
        if isinstance(request.inputs.get("query"), InteractiveInput):
            raise ValueError("interrupt answers must use answer_request")
        query = request.inputs.get("query")
        if not isinstance(query, str) or not query.strip():
            raise ValueError("Native user input must be non-empty text")
        token = uuid.uuid4().hex
        self._requests[token] = _HostRequest(request=request)
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

        token = uuid.uuid4().hex
        result = asyncio.get_running_loop().create_future()
        self._requests[token] = _HostRequest(goal=operation, result=result)
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
        token = uuid.uuid4().hex
        self._requests[token] = _HostRequest(attach_goal=True)
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
        token = uuid.uuid4().hex
        self._requests[token] = _HostRequest(attach_goal=True)
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
