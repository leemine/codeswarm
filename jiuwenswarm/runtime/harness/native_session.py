# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Native Single host boundary over the shared provider lifecycle.

Only transient Python request references live here. Configuration, event
serialization and durable history never contain permission handoff objects.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import uuid
from collections import deque
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, AsyncIterator, Awaitable, Callable

from openjiuwen.core.controller.schema.execution_origin import ExecutionOrigin
from openjiuwen.core.session.interaction.interactive_input import InteractiveInput
from openjiuwen.harness.engine import HarnessEngine
from openjiuwen.harness.schema.interaction import InputDispatchMode, SendInputRequest
from openjiuwen.harness_protocol import (
    AbortMode,
    DeliveryMode,
    HarnessContext,
    HarnessEvent,
    HarnessInput,
    HarnessStateError,
    SendReceipt,
    TurnEventKind,
    TurnLifecycleEvent,
    UnsupportedHarnessCapabilityError,
    freeze_json_object,
    json_value_to_builtin,
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

logger = logging.getLogger(__name__)

_REQUEST_KEY = "native.host_request"
_TERMINAL = {TurnEventKind.FINISHED, TurnEventKind.FAILED, TurnEventKind.ABORTED}


def _initial_goal_operation(action, *, session_id, **kwargs):
    """Private immutable admission arguments, shared with the actual dispatcher."""
    if (action != 'set' or not isinstance(session_id, str) or not session_id
            or set(kwargs) - {'objective', 'overwrite_confirmed', 'token_budget', 'max_attempts'}):
        raise PermissionError('Managed Native initial Goal arguments are unsupported')
    values = {'action': action, 'session_id': session_id, 'objective': kwargs.get('objective'),
              'overwrite_confirmed': kwargs.get('overwrite_confirmed', False),
              'token_budget': kwargs.get('token_budget'), 'max_attempts': kwargs.get('max_attempts')}
    if ((values['objective'] is not None and not isinstance(values['objective'], str))
            or type(values['overwrite_confirmed']) is not bool
            or any(values[key] is not None and type(values[key]) is not int
                   for key in ('token_budget', 'max_attempts'))):
        raise PermissionError('Managed Native initial Goal argument types are unsupported')
    return freeze_json_object(values)


@dataclass(frozen=True, eq=False, repr=False)
class NativeRequestLifecycle:
    """Host-only admission; source identity never comes from serialized input."""

    source: ExecutionOrigin
    on_bound: Callable[[NativeOwnedTurn], None]
    on_terminal: Callable[[NativeOwnedTurn, TurnEventKind], None]
    on_not_admitted: Callable[[], None] | None = None

    def __post_init__(self):
        if type(self.source) is not ExecutionOrigin:
            raise TypeError("Native lifecycle requires an original ExecutionOrigin")
        if not callable(self.on_bound) or not callable(self.on_terminal):
            raise TypeError("Native lifecycle callbacks must be callable")
        if self.on_not_admitted is not None and not callable(self.on_not_admitted):
            raise TypeError("Native rejection callback must be callable")

    def __copy__(self):
        return self

    def __deepcopy__(self, _memo):
        return self

    def __reduce__(self):
        raise TypeError("Native lifecycle references cannot be serialized")


@dataclass(frozen=True, eq=False, repr=False)
class NativeOwnedTurn:
    """Exact live references; not an execution state machine or wire receipt."""

    source: ExecutionOrigin
    request_id: str | None
    turn_id: str
    _native: NativeExecutionSession
    _entry: _HostRequest
    _pending: Any

    def __copy__(self):
        return self

    def __deepcopy__(self, _memo):
        return self

    def __reduce__(self):
        raise TypeError("Native lifecycle references cannot be serialized")


@dataclass(frozen=True, eq=False, repr=False)
class _HostGoalReadmission:
    """Fresh host admission; an old exit receipt confers no new authority."""

    native: Any
    binding: Any
    agent: Any
    session: Any
    tool_owner: Any
    selector: Any
    request: SendInputRequest
    action: str
    previous: NativeOwnedTurn | None
    previous_facts: Any
    checker: Callable[[], None]

    def check_previous(self):
        old = self.previous
        if old is None:
            return
        entry, pending, barrier, confirmed, cleanup = self.previous_facts
        if (old._native is not self.native or old._entry is not entry
                or old._pending is not pending or entry.owned is not old
                or entry.lifecycle is None or entry.lifecycle.source is not old.source
                or not entry.terminal_event.is_set() or not entry.terminal_notified
                or entry.terminal_kind not in _TERMINAL
                or pending._exit is not barrier or barrier is None
                or barrier.confirmed is not confirmed or barrier.cleanup is not cleanup
                or confirmed is None or not confirmed.done() or confirmed.cancelled()
                or cleanup is None or not cleanup.done() or cleanup.cancelled()
                or not pending._execution_done.is_set()):
            raise PermissionError("Goal readmission requires the original confirmed exit receipt")
        confirmed.result()
        cleanup.result()

    def check_static(self, entry, lifecycle):
        n = self.native
        if (n._closing or n._closed or n.engine.binding is not self.binding
                or n._native.agent is not self.agent
                or n._native._agent_session is not self.session
                or n._tool_owner is not self.tool_owner
                or self.agent.goal_manager is not self.selector.manager
                or entry.readmission is not self or entry.request is not self.request
                or entry.lifecycle is not lifecycle or lifecycle is None
                or (self.request.mode is not None and self.request.mode is not InputDispatchMode.FOLLOW_UP)
                or (self.previous is not None and (lifecycle.source is self.previous.source
                    or lifecycle.source.host_value is self.previous.source.host_value))):
            raise PermissionError("Goal readmission host admission changed")
        self.check_previous()

    def check(self, entry, lifecycle):
        self.check_static(entry, lifecycle)
        source = lifecycle.source
        _sync_callback(self.checker)
        source._check_current()
        if lifecycle.source is not source:
            raise PermissionError("Goal readmission producer source changed")
        # The two synchronous callbacks may reenter and replace host objects.
        self.check_static(entry, lifecycle)


@dataclass(frozen=True, eq=False, repr=False)
class NativeSteerControl:
    """One live supplemental-input capability for an exact original Round.

    This admits input only. It never replaces the original execution origin or
    any model/tool/MCP/artifact authority, and is not a resumable wire receipt.
    """

    _native: NativeExecutionSession
    _binding: Any
    _owned: NativeOwnedTurn
    _round: Any
    _request_id: str
    _checker: Callable[[], None]
    _attempted: bool = False
    _in_flight: bool = False

    def check_current(self, native, request_id):
        owned = self._owned
        entry = owned._entry
        pending = owned._pending
        token = pending.content.metadata.get(_REQUEST_KEY)
        if (native is not self._native or request_id != self._request_id
                or native.engine.binding is not self._binding
                or native._closing or native._closed
                or native._native.active_turn is not pending or pending.abort_requested
                or native._native._capture_owned_turn(owned.turn_id) is not pending
                or entry.owned is not owned or native._requests.get(token) is not entry
                or entry.lifecycle is None or entry.lifecycle.source is not owned.source
                or entry.terminal_kind is not None):
            raise PermissionError("Original Native steer target is unavailable")
        owned.source._check_current()
        agent = native._native.agent
        if agent is None or agent._capture_owned_round(pending._origin) is not self._round:
            raise PermissionError("Original Native steer Round is unavailable")
        agent._check_owned_round(self._round)
        if self._round.waiting_for_input:
            raise PermissionError("Native steer cannot answer a waiting interaction")
        _sync_callback(self._checker)

    def _claim(self, native, request_id):
        self.check_current(native, request_id)
        if self._attempted:
            raise PermissionError("Native steer control cannot be replayed")
        object.__setattr__(self, "_attempted", True)
        object.__setattr__(self, "_in_flight", True)

    def _check_delivery(self, native, request_id):
        if not self._in_flight:
            raise PermissionError("Native steer submission has ended")
        self.check_current(native, request_id)

    def __copy__(self):
        return self

    def __deepcopy__(self, _memo):
        return self

    def __reduce__(self):
        raise TypeError("Native steer references cannot be serialized")


def _sync_callback(callback, *args):
    value = callback(*args)
    if inspect.iscoroutine(value):
        value.close()
    if value is not None:
        raise TypeError("Native lifecycle callback must synchronously return None")


@dataclass(slots=True)
class _HostRequest:
    request: SendInputRequest | None = field(default=None, repr=False)
    goal: Callable[[Any], Awaitable[dict[str, Any]]] | None = field(
        default=None, repr=False
    )
    goal_operation: Any = field(default=None, repr=False)
    readmission: Any = field(default=None, repr=False)
    readmission_plan: Any = field(default=None, repr=False)
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
    artifact_issuer_factory: Any = field(default=None, repr=False)
    guarded_artifact_issuer: Any = field(default=None, repr=False)
    lifecycle: NativeRequestLifecycle | None = field(default=None, repr=False)
    owned: NativeOwnedTurn | None = field(default=None, repr=False)
    bound_notified: bool = False
    terminal_kind: TurnEventKind | None = None
    terminal_event: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    terminal_notified: bool = False
    receipt_seen: bool = False
    not_admitted: bool = False
    steer_control: NativeSteerControl | None = field(default=None, repr=False)
    steer_delivered: bool = False


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
        require_execution_origin: bool = False,
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
        if type(require_execution_origin) is not bool:
            raise TypeError("require_execution_origin must be bool")
        self._require_execution_origin = require_execution_origin
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
        self._artifact_resource_governed = False

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
            capture_execution_origin=self._capture_execution_origin if require_execution_origin else None,
            **({"prepare_goal_readmission": self._prepare_goal_readmission}
               if require_execution_origin else {}),
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

    def _capture_lifecycle(self, request):
        from jiuwenswarm.governance.tool_context import submitted_native_lifecycle_factory

        factory = submitted_native_lifecycle_factory()
        if not self._require_execution_origin:
            if factory is not None:
                raise PermissionError("Legacy Native session cannot accept managed admission")
            return None
        if factory is None:
            raise PermissionError("Native admission requires its original host source")
        lifecycle = factory(self, request)
        if inspect.iscoroutine(lifecycle):
            lifecycle.close()
        if type(lifecycle) is not NativeRequestLifecycle:
            raise TypeError("Native admission requires a synchronous lifecycle")
        try:
            lifecycle.source._check_current()
        except BaseException:
            try:
                if lifecycle.on_not_admitted is not None:
                    _sync_callback(lifecycle.on_not_admitted)
            except BaseException:
                logger.warning("Native non-admission notification failed; original submission remains unresolved")
            raise
        return lifecycle

    def _capture_execution_origin(self, content):
        token = content.metadata.get(_REQUEST_KEY)
        entry = self._requests.get(token) if isinstance(token, str) else None
        if self._closing or self._closed or entry is None or entry.lifecycle is None:
            raise PermissionError("Native original request source is unavailable")
        try:
            entry.lifecycle.source._check_current()
        except BaseException:
            self._not_admitted(entry)
            raise
        return entry.lifecycle.source

    @staticmethod
    def _not_admitted(entry):
        if entry.lifecycle is not None and not entry.not_admitted:
            try:
                if entry.lifecycle.on_not_admitted is not None:
                    _sync_callback(entry.lifecycle.on_not_admitted)
            except BaseException:
                logger.warning("Native non-admission notification failed; original submission remains unresolved")
                return
            entry.not_admitted = True

    def _failed_submission(self, token):
        entry = self._requests.get(token)
        # Arbitrary await/cancellation is not proof of non-admission. Preserve
        # a managed entry, including an already-bound Turn, for exact cleanup.
        if entry is not None and (entry.lifecycle is None or entry.not_admitted):
            self._requests.pop(token, None)

    def _check_same_origin(self, lifecycle, entry):
        if (lifecycle is None or entry is None or entry.lifecycle is None
                or lifecycle.source.host_value is not entry.lifecycle.source.host_value):
            raise PermissionError("Native control cannot replace the original source")
        entry.lifecycle.source._check_current()
        lifecycle.source._check_current()

    def _active_entry(self):
        active = self._native.active_turn
        token = active.content.metadata.get(_REQUEST_KEY) if active is not None else None
        return self._requests.get(token)

    def capture_owned_request_turn(self, *, token: str, turn_id: str) -> NativeOwnedTurn:
        entry = self._requests.get(token)
        if entry is None or entry.lifecycle is None:
            raise PermissionError("Native owned request is unavailable")
        owned = entry.owned
        if owned is not None:
            if owned.turn_id != turn_id or owned._native is not self:
                raise PermissionError("Native owned Turn was replaced")
        else:
            pending = self._native._capture_owned_turn(turn_id)
            if pending is None or pending.content.metadata.get(_REQUEST_KEY) != token:
                raise PermissionError("Native original Turn cannot be proven")
            owned = NativeOwnedTurn(entry.lifecycle.source,
                                    entry.request.request_id if entry.request is not None else None,
                                    turn_id, self, entry, pending)
            entry.owned = owned
        self._turn_requests[turn_id] = token
        self._notify_lifecycle_bound(entry)
        return owned

    def capture_steer_control(self, *, source: ExecutionOrigin, request_id: str,
                              check_current: Callable[[], None]) -> NativeSteerControl:
        """Capture only an exact parent source supplied by the Runtime owner."""
        if (not self._require_execution_origin or type(source) is not ExecutionOrigin
                or not isinstance(request_id, str) or not request_id.strip()
                or not callable(check_current)):
            raise PermissionError("Native steer requires an explicit host control")
        active = self._native.active_turn
        entry = self._active_entry()
        if (active is None or entry is None or entry.lifecycle is None
                or entry.lifecycle.source is not source or entry.owned is None
                or entry.owned._pending is not active):
            raise PermissionError("Native steer original owner does not match")
        agent = self._native.agent
        if agent is None or not callable(getattr(agent, "_send_owned_steer", None)):
            raise UnsupportedHarnessCapabilityError("Native exact Round steering is unavailable")
        target = agent._capture_owned_round(active._origin)
        if target is None:
            raise PermissionError("Native steer original Round is unavailable")
        control = NativeSteerControl(self, self.engine.binding, entry.owned,
                                     target, request_id, check_current)
        control.check_current(self, request_id)
        return control

    async def _send_managed_steer(self, request, control):
        if type(control) is not NativeSteerControl:
            raise UnsupportedHarnessCapabilityError("Managed Native STEER requires an original control owner selector")
        control._claim(self, request.request_id)
        token = uuid.uuid4().hex
        original = control._owned._entry
        entry = _HostRequest(
            request=request, steer_control=control,
            authority=original.authority, guarded_authority=original.guarded_authority,
            model_authority=original.model_authority, guarded_model_authority=original.guarded_model_authority,
            mcp_authority=original.mcp_authority, guarded_mcp_authority=original.guarded_mcp_authority,
            artifact_issuer_factory=original.artifact_issuer_factory,
            guarded_artifact_issuer=original.guarded_artifact_issuer,
        )
        self._requests[token] = entry
        content = HarnessInput(content=request.inputs["query"], metadata={_REQUEST_KEY: token})
        async def send():
            control._check_delivery(self, request.request_id)
            # HarnessIOAdapter's legacy STEER->AUTO fallback must not create a
            # replacement Turn for a managed exact-target input.
            return await self._native.send(content, mode=DeliveryMode.STEER)
        try:
            receipt = (await send() if self._output_router is None
                       else await self._output_router.submit(send))
            if receipt.accepted_mode is not DeliveryMode.STEER or receipt.turn_id != control._owned.turn_id:
                raise RuntimeError("Native steer returned a different original Turn")
            control._check_delivery(self, request.request_id)
            return receipt
        except Exception as exc:
            if entry.steer_delivered:
                from jiuwenswarm.server.runtime.agent_adapter.session_input import SessionInputDeliveryUnknown
                raise SessionInputDeliveryUnknown(
                    "Native steer changed after submission; do not retry automatically"
                ) from exc
            raise
        finally:
            object.__setattr__(control, "_in_flight", False)
            self._requests.pop(token, None)

    async def abort_owned_request_turn(self, owned: NativeOwnedTurn) -> None:
        """Wait for the original Provider barrier AND its sole observer terminal."""
        if (type(owned) is not NativeOwnedTurn or owned._native is not self
                or owned._entry.owned is not owned):
            raise PermissionError("Native exit requires its original owned Turn")
        entry = owned._entry
        if entry.terminal_kind is None:
            await self._native._abort_owned_turn(owned._pending, mode=AbortMode.FORCE)
            # A queued fence returns before its serialized ABORTED event. A
            # bounded caller can retry this same handle; no current-Turn fallback.
            await asyncio.wait_for(entry.terminal_event.wait(), RESOURCE_STOP_TIMEOUT_S)
        self._notify_lifecycle_terminal(entry)

    @staticmethod
    def _notify_lifecycle_bound(entry):
        if not entry.bound_notified:
            _sync_callback(entry.lifecycle.on_bound, entry.owned)
            entry.bound_notified = True

    @staticmethod
    def _notify_lifecycle_terminal(entry):
        if entry.lifecycle is not None and entry.terminal_kind is not None and not entry.terminal_notified:
            NativeExecutionSession._notify_lifecycle_bound(entry)
            _sync_callback(entry.lifecycle.on_terminal, entry.owned, entry.terminal_kind)
            entry.terminal_notified = True

    def _check_terminal_exit(self, entry, kind):
        pending = entry.owned._pending
        barrier = pending._exit
        if barrier is not None and barrier.confirmed.done():
            barrier.confirmed.result()
            return
        # Serialized queued abort/stop never entered the Provider body. A
        # generic producer terminal without either proof is still unknown.
        if (kind is TurnEventKind.ABORTED
                and (pending.abort_requested or self._native._stopping)
                and not pending._execution_done.is_set()
                and not pending._admissions and pending._stream is None):
            return
        raise ExecutionExitUnconfirmedError([("native_turn", RuntimeError("Original Native Turn exit is unconfirmed"))])

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
        async def send_and_bind():
            receipt = await self.io.send(content, immediate=immediate)
            token = content.metadata.get(_REQUEST_KEY)
            entry = self._requests.get(token)
            if entry is not None and entry.lifecycle is not None:
                # Bind before TurnOutputRouter's finally can suspend on its
                # mailbox lock. Accepted work must survive caller cancellation.
                self._remember_turn(receipt, token)
            return receipt

        if self._output_router is None:
            return await send_and_bind()
        return await self._output_router.submit(send_and_bind)

    def _register_host_request(self, **kwargs):
        from jiuwenswarm.governance.tool_context import (
            submitted_tool_authorizer, submitted_model_authorizer, submitted_mcp_authorizer,
            submitted_artifact_issuer_factory,
        )
        lifecycle = self._capture_lifecycle(kwargs.get("request"))
        token = uuid.uuid4().hex
        authority = submitted_tool_authorizer()
        model_authority = submitted_model_authorizer()
        mcp_authority = submitted_mcp_authorizer()
        artifact_factory = submitted_artifact_issuer_factory()
        self._artifact_resource_governed = self._artifact_resource_governed or artifact_factory is not None
        self._mcp_resource_governed = self._mcp_resource_governed or mcp_authority is not None
        self._model_resource_governed = self._model_resource_governed or model_authority is not None
        self._resource_governed = self._resource_governed or authority is not None
        entry = _HostRequest(**kwargs, lifecycle=lifecycle, authority=authority, model_authority=model_authority,
                             mcp_authority=mcp_authority, artifact_issuer_factory=artifact_factory)

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

        def guarded_artifact(*, source_execution, execution_slice, **facts):
            from jiuwenswarm.governance.resources import ResourceAccessDenied
            active = self._native.active_turn
            def current():
                return (
                    not self._closing and not self._closed and active is not None
                    and self._native.active_turn is active and not active.abort_requested
                    and active.content.metadata.get(_REQUEST_KEY) == token
                    and self._requests.get(token) is entry
                    and execution_slice.owner is self and execution_slice.active
                    and execution_slice.artifact_issuer_factory is guarded_artifact
                    and self._exit_state is ExecutionExitState.RUNNING
                    and execution_slice._task is not None
                    and not execution_slice._task.done() and not execution_slice._task.cancelling()
                )
            if artifact_factory is None or not current():
                raise ResourceAccessDenied('artifact execution authority unavailable')
            issuer = artifact_factory(source_execution=source_execution,
                execution_slice=execution_slice, native_session=self,
                is_current_host_request=current, **facts)
            if not current():
                raise ResourceAccessDenied('artifact execution authority changed')
            return issuer

        entry.guarded_authority = guarded
        entry.guarded_model_authority = guarded_model
        entry.guarded_mcp_authority = guarded_mcp
        entry.guarded_artifact_issuer = guarded_artifact
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

    def _goal_entry_for_slice(self, ctx):
        """Resolve only an actually executing, originally sourced Goal task."""
        from openjiuwen.core.controller.modules.task_manager import _current_task_execution
        from openjiuwen.core.controller.schema.execution_origin import (
            _capture_live_execution_origin, current_execution_origin,
        )
        from openjiuwen.harness.goal.schema import GoalRecord
        from openjiuwen.core.single_agent.rail.base import InvokeInputs, TaskIterationInputs, RunKind, RunContext
        from openjiuwen.harness.goal.store import SESSION_GOAL_RECORD_KEY
        from jiuwenswarm.governance.resources import ResourceAccessDenied

        denied = "Native Goal execution source unavailable"
        try:
            source = current_execution_origin()
            owner, pending = self._tool_owner, self._native.active_turn
            if not self._require_execution_origin or source is None or owner is None or pending is None:
                raise ResourceAccessDenied(denied)
            agent, _, session, binding = owner
            inputs = ctx.inputs
            token = pending.content.metadata.get(_REQUEST_KEY)
            entry = self._requests.get(token)
            if entry is None or entry.request is None or entry.lifecycle is None:
                raise ResourceAccessDenied(denied)
            request, lifecycle, owned, goal = entry.request, entry.lifecycle, entry.owned, entry.goal
            if owned is None:
                raise ResourceAccessDenied(denied)
            round_owner = agent._capture_owned_round(source)
            if round_owner is None:
                raise ResourceAccessDenied(denied)
            work, controller = round_owner.work, round_owner._controller
            manager, scheduler = controller.task_manager, controller.task_scheduler
            iteration = type(inputs) is TaskIterationInputs
            outer = type(inputs) is InvokeInputs
            if not (iteration or outer):
                raise ResourceAccessDenied(denied)
            capture = (_current_task_execution(manager, round_owner.task_id, session)
                       if iteration else None)
            if iteration and capture is None:
                raise ResourceAccessDenied(denied)
            wrapper = (scheduler._capture_owned_dispatch(capture, session)
                       if iteration else round_owner._facade_task)
            goal_manager = agent.goal_manager
            goal_source = goal_manager._execution_origin
            goal_facts = (work.context.get('session_id'), work.context.get('goal_id'),
                          work.context.get('revision'))

            def same_facts():
                # No checker invocation here: the final pass cannot select fresh
                # facts after the last potentially mutating source callback.
                run_context = getattr(inputs, 'run_context', None)
                context_facts = ({'session_id': run_context.session_id, **run_context.extra}
                                 if type(run_context) is RunContext else run_context)
                if not isinstance(context_facts, dict):
                    return False
                if outer:
                    if (type(run_context) is not RunContext or inputs.run_kind is not RunKind.GOAL
                            or round_owner._facade_task is not wrapper
                            or inputs.query != work.inputs.get('query')):
                        return False
                    if context_facts != dict(work.context):
                        return False
                else:
                    if (inputs.run_kind not in ('goal', RunKind.GOAL) or inputs.loop_event is None
                            or inputs.loop_event.execution_origin is not source
                            or round_owner._task_capture is None
                            or round_owner._task_capture.stored is not capture.stored
                            or capture.origin is not source
                            or _current_task_execution(manager, round_owner.task_id, session) is not capture
                            or manager._check_task_execution(capture) is not capture.stored
                            or scheduler._capture_owned_dispatch(capture, session) is not wrapper
                            or round_owner._scheduler_wrapper is not wrapper):
                        return False
                raw = session.get_state(SESSION_GOAL_RECORD_KEY)
                if not isinstance(raw, dict):
                    return False
                record = GoalRecord.from_dict(raw)
                return (
                    self._tool_owner is owner and ctx.agent is agent and ctx.session is session
                    and ctx.inputs is inputs and self.engine.binding is binding
                    and self._native.active_turn is pending and pending._origin is source
                    and pending._agent is agent and pending._session is session
                    and self._native._capture_owned_turn(pending.turn_id) is pending
                    and not pending.abort_requested and pending._exit is None
                    and current_execution_origin() is source
                    and self._requests.get(token) is entry and entry.lifecycle is lifecycle
                    and entry.goal is goal and entry.terminal_kind is None
                    and entry.owned is owned and owned._pending is pending and owned._entry is entry
                    and owned._native is self and owned.source is lifecycle.source
                    and entry.request is request and owned.request_id == request.request_id
                    and source.host_value is lifecycle.source.host_value
                    and pending.content.metadata.get(_REQUEST_KEY) == token
                    and agent._active_interaction_round is round_owner
                    and round_owner.work is work and round_owner._session is session
                    and round_owner._controller is controller and agent.loop_controller is controller
                    and agent._event_manager.active_work is work and work.kind == 'goal'
                    and work.execution_origin is source
                    and (context_facts.get('session_id'), context_facts.get('goal_id'),
                         context_facts.get('revision')) == goal_facts
                    and (work.context.get('session_id'), work.context.get('goal_id'),
                         work.context.get('revision')) == goal_facts
                    and (record.session_id, record.goal_id, record.revision) == goal_facts
                    and session.get_session_id() == binding.host_session_id == goal_facts[0]
                    and agent.goal_manager is goal_manager and goal_manager._execution_origin is goal_source
                    and goal_source[:3] == goal_facts and goal_source[3] is source
                    and goal_manager._store._session is session
                    and controller.task_manager is manager and controller.task_scheduler is scheduler
                    and asyncio.current_task() is wrapper
                    and wrapper is not None and not wrapper.done() and not wrapper.cancelling()
                    and self._exit_state is ExecutionExitState.RUNNING and not self._closing and not self._closed
                )

            if not same_facts():
                raise ResourceAccessDenied(denied)
            if _capture_live_execution_origin() is not source:
                raise ResourceAccessDenied(denied)
            agent._check_owned_round(round_owner)
            source._check_current()
            if not same_facts():
                raise ResourceAccessDenied(denied)
            return token, entry
        except Exception:
            raise ResourceAccessDenied(denied) from None

    def _execution_slice_for(self, ctx):
        from jiuwenswarm.governance.tool_context import NativeExecutionSlice
        from jiuwenswarm.governance.resources import ResourceAccessDenied
        from openjiuwen.harness.execution_subject import current_execution_subject
        if not (self._resource_governed or self._model_resource_governed or self._mcp_resource_governed or self._artifact_resource_governed):
            return None
        owner = self._tool_owner
        if owner is None or ctx.agent is not owner[0] or ctx.session is not owner[2]:
            raise ResourceAccessDenied('Native execution slice owner unavailable')
        context = getattr(ctx.inputs, 'run_context', None)
        extra = context.get('extra', context) if isinstance(context, dict) else getattr(context, 'extra', {})
        token = extra.get(_REQUEST_KEY) if isinstance(extra, dict) else None
        active = self._native.active_turn
        entry = self._requests.get(token)
        run_kind = getattr(ctx.inputs, 'run_kind', None)
        if (isinstance(extra, dict) and _REQUEST_KEY not in extra
                and getattr(run_kind, 'value', run_kind) == 'goal'):
            token, entry = self._goal_entry_for_slice(ctx)
        subject = current_execution_subject()
        if (self._closing or self._closed or self._exit_state is not ExecutionExitState.RUNNING
                or active is None or active.abort_requested
                or active.content.metadata.get(_REQUEST_KEY) != token or entry is None
                or (subject is not None and subject.kind == 'subagent')):
            raise ResourceAccessDenied('Native execution slice request unavailable')
        return NativeExecutionSlice(self, entry.guarded_authority, entry.guarded_model_authority, subject,
                                    mcp_authorizer=entry.guarded_mcp_authority,
                                    artifact_issuer_factory=entry.guarded_artifact_issuer)

    async def send_request(self, request: SendInputRequest, *, control: NativeSteerControl | None = None) -> SendReceipt:
        """Accept a host request without serializing its permission/context objects."""
        if isinstance(request.inputs.get("query"), InteractiveInput):
            raise ValueError("interrupt answers must use answer_request")
        query = request.inputs.get("query")
        if not isinstance(query, str) or not query.strip():
            raise ValueError("Native user input must be non-empty text")
        if self._require_execution_origin and request.mode is InputDispatchMode.STEER:
            return await self._send_managed_steer(request, control)
        if control is not None:
            raise PermissionError("Native steer control cannot authorize another input mode")
        token = self._register_host_request(request=request)
        content = HarnessInput(content=query, metadata={_REQUEST_KEY: token})
        try:
            receipt = await self._send(
                content, immediate=request.mode is InputDispatchMode.STEER
            )
        except BaseException:
            self._failed_submission(token)
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
        if self._require_execution_origin:
            if entry.lifecycle is None:
                raise PermissionError("Native continuation lacks its original source")
            entry.lifecycle.source._check_current()
        else:
            self._capture_lifecycle(request)  # A cached legacy Session cannot upgrade silently.
        entry.resumes.append(request)
        entry.answered.update(ids)
        try:
            await self.io.send(query)
        except BaseException:
            entry.resumes.remove(request)
            entry.answered.difference_update(ids)
            raise
        return True

    def _prepare_goal_readmission(self, agent, content, origin):
        """Called by core before attaching the sole output reader."""
        from openjiuwen.harness.goal.readmission import _NativeGoalReadmissionPlan

        token = content.metadata.get(_REQUEST_KEY)
        entry = self._requests.get(token)
        if entry is None:
            raise PermissionError("Goal readmission lacks its original host request")
        cap = entry.readmission
        if cap is None:
            return None  # Ordinary managed input still uses core's guarded attach.
        lifecycle = entry.lifecycle
        pending = self._native.active_turn

        def check():
            cap.check(entry, lifecycle)
            if (self._requests.get(token) is not entry or agent is not cap.agent
                    or self._native.active_turn is not pending
                    or pending is None or pending._origin is not origin
                    or origin.host_value is not lifecycle.source.host_value
                    or pending.content is not content):
                raise PermissionError("Goal readmission Pending or producer changed")

        check()
        if entry.readmission_plan is not None:
            raise PermissionError("Goal readmission plan already prepared")
        plan = _NativeGoalReadmissionPlan(cap.selector, cap.action, check,
            cap.previous._pending if cap.previous is not None else None)
        entry.readmission_plan = plan
        return plan

    async def submit_goal_readmission(
        self, *, request: SendInputRequest, action: str, expected_record,
        previous: NativeOwnedTurn | None, check_current: Callable[[], None],
    ) -> tuple[SendReceipt, asyncio.Future]:
        """Explicit fresh producer only; never invoked by an EOF observer.

        Runtime supplies its current admission through the original lifecycle
        factory and a synchronous checker. ``previous`` must be its retained
        original owned handle; no Session/latest lookup can substitute for it.
        """
        if (not self._require_execution_origin or type(action) is not str or action not in {"resume", "attach"}
                or type(request) is not SendInputRequest
                or type(request.request_id) is not str or not request.request_id
                or (request.mode is not None and request.mode is not InputDispatchMode.FOLLOW_UP)
                or not callable(check_current) or self._native.active_turn is not None):
            raise PermissionError("Goal readmission requires a fresh idle managed request")
        request = replace(request, inputs=freeze_json_object(request.inputs))
        binding, agent = self.engine.binding, self._native.agent
        session, owner = self._native._agent_session, self._tool_owner
        if (agent is None or session is None or owner is None
                or owner[0] is not agent or owner[2] is not session or owner[3] is not binding
                or session.get_session_id() != binding.host_session_id):
            raise PermissionError("Goal readmission lacks its original bound Session")
        selector = agent.goal_manager._capture_idle_readmission(expected_record=expected_record)
        facts = None
        if previous is not None:
            if type(previous) is not NativeOwnedTurn or previous._native is not self:
                raise PermissionError("Goal readmission requires an original Native owned handle")
            pending = previous._pending
            barrier = pending._exit
            facts = (previous._entry, pending, barrier,
                     getattr(barrier, 'confirmed', None), getattr(barrier, 'cleanup', None))
        cap = _HostGoalReadmission(self, binding, agent, session, owner, selector,
                                  request, action, previous, facts, check_current)
        cap.check_previous()
        result = asyncio.get_running_loop().create_future()
        token = self._register_host_request(request=request, readmission=cap, result=result)
        submission_started = False
        try:
            entry = self._requests[token]
            cap.check(entry, entry.lifecycle)
            selector.check_static()
            if self._requests.get(token) is not entry:
                raise PermissionError("Goal readmission request entry changed")
            submission_started = True
            receipt = await self._send(HarnessInput(content="", metadata={_REQUEST_KEY: token}))
            self._remember_turn(receipt, token)
            return receipt, result
        except BaseException:
            if not submission_started:
                self._not_admitted(entry)
            if self._requests.get(token) is entry:
                self._failed_submission(token)
            result.cancel()
            raise

    async def submit_goal(
        self, action: str, *, request: SendInputRequest | None = None, **kwargs: Any
    ) -> tuple[SendReceipt, asyncio.Future]:
        """Dispatch Goal work in the current Turn, or start one while idle.

        An active Native output lease continues after an in-flight set/resume;
        a fresh Turn attaches its own lease before calling GoalManager. The
        returned future contains the original host control result.
        """
        if action not in {"set", "resume"} or self._goal_dispatcher is None:
            raise ValueError("submit_goal requires a configured set/resume dispatcher")

        goal_operation = None
        if self._require_execution_origin:
            if self._native.active_turn is not None:
                raise UnsupportedHarnessCapabilityError("Managed Native active Goal control requires an original owner selector")
            if action != "set":
                raise UnsupportedHarnessCapabilityError("Managed Native idle Goal resume requires a new original admission")
            goal_operation = _initial_goal_operation(action,
                session_id=kwargs.get('session_id', self.engine.binding.host_session_id),
                **{key: value for key, value in kwargs.items() if key != 'session_id'})
            if goal_operation['session_id'] != self.engine.binding.host_session_id:
                raise PermissionError('Managed Native initial Goal session differs from Binding')
            # Snapshot before lifecycle factories/source checkers can reenter.
            kwargs = {key: value for key, value in json_value_to_builtin(goal_operation).items()
                      if key in kwargs}

        async def operation(agent):
            return await self._goal_dispatcher(action=action, **kwargs)

        if self._require_execution_origin:
            if not isinstance(request, SendInputRequest) or not request.request_id:
                raise PermissionError("Managed Native initial Goal requires its actual request")
        result = asyncio.get_running_loop().create_future()
        token = self._register_host_request(request=request, goal=operation, goal_operation=goal_operation, result=result)
        try:
            receipt = await self._send(
                HarnessInput(content="", metadata={_REQUEST_KEY: token}),
                immediate=not self._require_execution_origin,
            )
        except BaseException:
            self._failed_submission(token)
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
        entry = self._requests.get(token)
        if entry is not None and entry.lifecycle is not None:
            entry.receipt_seen = True
            if receipt.accepted_mode is DeliveryMode.STEER:
                return
            self.capture_owned_request_turn(token=token, turn_id=receipt.turn_id)
            if entry.terminal_kind is not None:
                self._notify_lifecycle_terminal(entry)
                self._requests.pop(token, None)
                self._turn_requests.pop(receipt.turn_id, None)
            return
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
        if self._require_execution_origin:
            raise UnsupportedHarnessCapabilityError("Managed Native Goal control requires an original owner selector")
        if action not in {"get", "pause", "clear"} or self._goal_dispatcher is None:
            raise ValueError("control_goal only accepts get/pause/clear")
        if self._native.agent is None:
            raise RuntimeError("Native session has not started")
        return await self._goal_dispatcher(action=action, **kwargs)

    async def attach_goal(self, *, request: SendInputRequest | None = None) -> SendReceipt:
        """Attach the existing active Goal through the same Turn output route."""
        if self._require_execution_origin:
            raise UnsupportedHarnessCapabilityError("Managed Native Goal attach requires a new original admission")
        token = self._register_host_request(request=request, attach_goal=True)
        try:
            receipt = await self._send(
                HarnessInput(content="", metadata={_REQUEST_KEY: token})
            )
        except BaseException:
            self._failed_submission(token)
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
        control = entry.steer_control
        if control is not None:
            if default_request.mode is not InputDispatchMode.STEER or resuming:
                raise PermissionError("Native control cannot dispatch another input kind")
            control._check_delivery(self, entry.request.request_id)
        if entry.lifecycle is not None:
            entry.lifecycle.source._check_current()
            if default_request.mode is InputDispatchMode.STEER:
                self._check_same_origin(entry.lifecycle, self._active_entry())
        if entry.readmission is not None:
            if resuming or default_request.mode is InputDispatchMode.STEER:
                raise PermissionError("Goal readmission cannot become an old-root control")
            plan = entry.readmission_plan
            if plan is None:
                raise PermissionError("Goal readmission was not prepared by Native admission")
            record = plan.result()
            if not entry.result.done():
                entry.result.set_result({'result_type': 'goal_stream', 'goal': record.to_dict()})
            return True
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

        control_dispatched = False
        async def send_if_current(host_request):
            nonlocal control_dispatched
            if control is not None:
                if control_dispatched:
                    raise PermissionError("Native steer input cannot be dispatched twice")
                if (host_request.request_id != request.request_id
                        or host_request.mode is not InputDispatchMode.STEER
                        or host_request.inputs.get("query") != content.content):
                    raise PermissionError("Native steer input changed during admission")
                control_dispatched = True
            active = self._native.active_turn
            if active is None or active.abort_requested:
                raise RuntimeError(
                    "Native turn was cancelled before host input dispatch"
                )
            if entry.lifecycle is not None:
                entry.lifecycle.source._check_current()
            if control is not None or self._resource_governed or self._model_resource_governed or self._mcp_resource_governed or self._artifact_resource_governed:
                inputs = dict(host_request.inputs)
                run = dict(inputs.get('run') or {})
                context = dict(run.get('context') or {})
                context.pop(_REQUEST_KEY, None)
                extra = dict(context.get('extra') or {})
                extra[_REQUEST_KEY] = (control._owned._pending.content.metadata[_REQUEST_KEY]
                                       if control is not None else token)
                context['extra'] = extra
                run['context'] = context
                inputs['run'] = run
                host_request = replace(host_request, inputs=inputs)
            if control is not None:
                control._check_delivery(self, host_request.request_id)
                sender = getattr(agent, "_send_owned_steer", None)
                if not callable(sender):
                    raise UnsupportedHarnessCapabilityError("Native exact Round steering is unavailable")
                await sender(control._round, host_request,
                             check_current=lambda: control._check_delivery(self, host_request.request_id))
                entry.steer_delivered = True
                control._check_delivery(self, host_request.request_id)
            else:
                await agent.send_input(host_request)

        if self._dispatch_guard is None:
            await send_if_current(request)
        else:
            await self._dispatch_guard(request, send=send_if_current)
        if control is not None:
            if not control_dispatched:
                raise PermissionError("Native steer input was not dispatched")
            control._check_delivery(self, request.request_id)
        if default_request.mode is InputDispatchMode.STEER:
            self._requests.pop(token, None)
        return True

    async def _observe(self, event: HarnessEvent) -> None:
        if self._require_execution_origin and isinstance(event.event, TurnLifecycleEvent):
            pending = self._native._capture_owned_turn(event.turn_id)
            token = pending.content.metadata.get(_REQUEST_KEY) if pending is not None else self._turn_requests.get(event.turn_id)
            entry = self._requests.get(token)
            if entry is not None and entry.lifecycle is not None and not entry.bound_notified:
                self.capture_owned_request_turn(token=token, turn_id=event.turn_id)
        if (
            isinstance(event.event, TurnLifecycleEvent)
            and event.event.kind in _TERMINAL
        ):
            self._terminal_turns.append(event.turn_id)
            token = self._turn_requests.pop(event.turn_id, None)
            entry = self._requests.get(token)
            if entry is not None and entry.lifecycle is not None:
                if entry.owned is None or entry.owned.turn_id != event.turn_id:
                    raise PermissionError("Native terminal lacks its original owned Turn")
                self._check_terminal_exit(entry, event.event.kind)
                entry.terminal_kind = event.event.kind
                entry.terminal_event.set()
                self._notify_lifecycle_terminal(entry)
                # _remember_turn must consume the exact original entry even
                # when terminal is delivered before send returns its receipt.
                if entry.owned is not None and entry.receipt_seen:
                    self._requests.pop(token, None)
            else:
                self._requests.pop(token, None)
            if entry is not None and entry.control_terminal is not None:
                entry.control_terminal(event.event.kind)
            if (
                entry is not None
                and entry.result is not None
                and not entry.result.done()
            ):
                if entry.readmission is not None and event.event.kind is not TurnEventKind.ABORTED:
                    # A denied/failed admission did not cancel its submitting
                    # producer. Preserve a visible failure for Runtime/UI.
                    entry.result.set_exception(HarnessStateError("Native Goal readmission ended without an accepted result"))
                else:
                    entry.result.cancel()
            if event.turn_id in self._goal_handoffs:
                self._goal_handoffs.discard(event.turn_id)
                if event.event.kind is TurnEventKind.FINISHED:
                    await self._attach_active_goal_after_eof()
        if self._observer is not None:
            await self._observer(event)

    async def _attach_active_goal_after_eof(self) -> None:
        """Keep a replacement Goal running if the previous lease reached EOF."""
        if self._require_execution_origin:
            raise UnsupportedHarnessCapabilityError("Managed Native Goal handoff requires an explicit service admission")
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
            self._failed_submission(token)
            if not self._closing:
                raise
            return
        except BaseException:
            self._failed_submission(token)
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
