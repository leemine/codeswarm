# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Native Single host boundary over the shared provider lifecycle.

Only transient Python request references live here. Configuration, event
serialization and durable history never contain permission handoff objects.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from openjiuwen.core.session.interaction.interactive_input import InteractiveInput
from openjiuwen.harness.engine import HarnessEngine
from openjiuwen.harness.schema.interaction import InputDispatchMode, SendInputRequest
from openjiuwen.harness_protocol import (
    HarnessContext,
    HarnessEvent,
    HarnessInput,
    SendReceipt,
    TurnEventKind,
    TurnLifecycleEvent,
)
from openjiuwen.harness_providers.io_adapter import HarnessIOAdapter
from openjiuwen.harness_providers.native import (
    AgentFactory,
    DeepAgentHarness,
    NativeHostHooks,
)

from jiuwenswarm.runtime.harness.binding_store import BoundExecution

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

    async def start(self, context: HarnessContext) -> None:
        binding = self.engine.binding
        if context.host_session_id != binding.host_session_id or context.cwd is None:
            raise ValueError(
                "Native context must match the bound session and workspace"
            )
        if str(Path(context.cwd).resolve()) != binding.workspace:
            raise ValueError("Native context workspace does not match the binding")
        await self.io.start(context)

    async def stop(self) -> None:
        await self.io.stop()
        for entry in self._requests.values():
            if entry.result is not None and not entry.result.done():
                entry.result.cancel()
        self._requests.clear()
        self._turn_requests.clear()

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
            receipt = await self.io.send(
                content, immediate=request.mode is InputDispatchMode.STEER
            )
        except BaseException:
            self._requests.pop(token, None)
            raise
        # STEER is dispatched immediately and owns no additional Turn entry.
        if token in self._requests:
            self._turn_requests[receipt.turn_id] = token
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
        """Queue a work-producing Goal operation inside the existing Turn skeleton.

        Set/resume run only after RUNNING and output attachment. The returned
        future contains the original host control result; it is cancelled if
        the queued operation is stopped before dispatch.
        """
        if action not in {"set", "resume"} or self._goal_dispatcher is None:
            raise ValueError("submit_goal requires a configured set/resume dispatcher")

        async def operation(agent):
            return await self._goal_dispatcher(action=action, **kwargs)

        token = uuid.uuid4().hex
        result = asyncio.get_running_loop().create_future()
        self._requests[token] = _HostRequest(goal=operation, result=result)
        try:
            receipt = await self.io.send(
                HarnessInput(content="", metadata={_REQUEST_KEY: token})
            )
        except BaseException:
            self._requests.pop(token, None)
            result.cancel()
            raise
        self._turn_requests[receipt.turn_id] = token
        return receipt, result

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
            token = self._turn_requests.pop(event.turn_id, None)
            entry = self._requests.pop(token, None)
            if (
                entry is not None
                and entry.result is not None
                and not entry.result.done()
            ):
                entry.result.cancel()
        if self._observer is not None:
            await self._observer(event)


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
