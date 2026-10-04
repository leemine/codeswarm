"""Host-only pause/clear of a proven idle Native Goal; no execution admission."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from openjiuwen.harness_protocol import UnsupportedHarnessCapabilityError

from .native_session import NativeOwnedTurn, _REQUEST_KEY, _TERMINAL, _sync_callback


@dataclass(frozen=True, eq=False, repr=False)
class NativeIdleGoalControl:
    native: Any
    engine: Any
    binding: Any
    harness: Any
    agent: Any
    session: Any
    tool_owner: Any
    manager: Any
    previous: NativeOwnedTurn | None
    previous_facts: Any
    checker: Callable[[], None]
    selector: Any = None
    _bridge: Any = field(init=False)

    def __post_init__(self):
        object.__setattr__(self, '_bridge', lambda: self.check_current())

    def _check_static(self):
        n, owner = self.native, self.tool_owner
        if (not n._require_execution_origin or n._closing or n._closed
                or n.engine is not self.engine or self.engine.binding is not self.binding
                or n._native is not self.harness or self.harness.agent is not self.agent
                or self.harness._agent_session is not self.session
                or n._tool_owner is not owner or owner is None or len(owner) != 4
                or owner[0] is not self.agent or owner[1] is not self.agent.react_agent
                or owner[2] is not self.session or owner[3] is not self.binding
                or self.session.get_session_id() != self.binding.host_session_id
                or self.agent.goal_manager is not self.manager):
            raise PermissionError('original idle Native Goal host target changed')
        old = self.previous
        if old is None:
            return
        (entry, pending, lifecycle, source, content, request, terminal, kind,
         barrier, confirmed, cleanup) = self.previous_facts
        if (old._native is not n or old._entry is not entry or old._pending is not pending
                or old.source is not source or entry.owned is not old
                or entry.lifecycle is not lifecycle or lifecycle is None or lifecycle.source is not source
                or entry.request is not request or entry.terminal_event is not terminal
                or entry.terminal_kind is not kind or kind not in _TERMINAL
                or not terminal.is_set() or not entry.terminal_notified or not entry.bound_notified
                or pending.content is not content or pending._agent is not self.agent
                or pending._session is not self.session or pending._harness_owner is not self.harness
                or pending.turn_id != old.turn_id or request is None or request.request_id != old.request_id
                or content.metadata.get(_REQUEST_KEY) is None
                or pending._exit is not barrier or barrier is None
                or barrier.confirmed is not confirmed or barrier.cleanup is not cleanup
                or confirmed is None or not confirmed.done() or confirmed.cancelled()
                or cleanup is None or not cleanup.done() or cleanup.cancelled()):
            raise PermissionError('idle Goal control requires the original confirmed host exit')
        confirmed.result()
        cleanup.result()

    def check_current(self):
        self._check_static()
        _sync_callback(self.checker)
        self._check_static()

    async def _apply(self, action):
        self._check_static()
        result = await self.selector.apply(action=action)
        self.check_result()
        return result

    async def pause(self):
        return await self._apply('pause')

    async def clear(self):
        return await self._apply('clear')

    def check_result(self):
        self._check_static()
        self.selector.check_result()
        self._check_static()

    def __copy__(self):
        return self

    def __deepcopy__(self, _memo):
        return self

    def __reduce__(self):
        raise TypeError('idle Native Goal controls cannot be serialized')


def capture_idle_goal_control(native, *, expected_record, previous, check_current):
    """Only explicit controller authority and an original retained exit handle.

    Runtime owns principal/owner/resource checks in the synchronous callback.
    No new lifecycle, host request, Pending, source or output lease is created.
    """
    if not callable(check_current) or not native._require_execution_origin:
        raise PermissionError('idle Goal control requires an explicit managed controller')
    engine, harness = native.engine, native._native
    binding, agent, session = engine.binding, harness.agent, harness._agent_session
    owner, manager = native._tool_owner, getattr(agent, 'goal_manager', None)
    if manager is None or session is None:
        raise PermissionError('idle Goal control requires the original prepared manager')
    capture = getattr(harness, '_capture_idle_goal_control', None)
    if not callable(capture):
        raise UnsupportedHarnessCapabilityError('Native idle Goal control is unavailable')
    facts = None
    if previous is not None:
        if type(previous) is not NativeOwnedTurn or previous._native is not native:
            raise PermissionError('idle Goal control requires its original Native owned handle')
        entry, pending = previous._entry, previous._pending
        barrier = pending._exit
        facts = (entry, pending, entry.lifecycle, previous.source, pending.content, entry.request,
                 entry.terminal_event, entry.terminal_kind, barrier,
                 getattr(barrier, 'confirmed', None), getattr(barrier, 'cleanup', None))
    cap = NativeIdleGoalControl(native, engine, binding, harness, agent, session, owner,
                               manager, previous, facts, check_current)
    cap._check_static()  # No callback: host references are fixed before core capture.
    selector = capture(expected_record=expected_record,
                       previous_turn=None if previous is None else previous._pending,
                       check_current=cap._bridge)
    object.__setattr__(cap, 'selector', selector)
    cap._check_static()
    return cap
