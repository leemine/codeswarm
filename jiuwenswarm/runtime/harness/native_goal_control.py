"""Live host control over one captured Native Goal manager and original Turn."""
from __future__ import annotations

from dataclasses import dataclass, field

from openjiuwen.core.controller.schema.execution_origin import ExecutionOrigin
from openjiuwen.harness_protocol import UnsupportedHarnessCapabilityError

from .native_session import NativeOwnedTurn, _REQUEST_KEY, _TERMINAL, _sync_callback


@dataclass(frozen=True, eq=False, repr=False)
class NativeGoalControl:
    """A temporary control permission; it never becomes execution authority."""

    native: object
    binding: object
    owned: NativeOwnedTurn
    agent: object
    manager: object
    origin: ExecutionOrigin
    selector: object
    request_id: str
    checker: object
    ack_checker: object
    token: str
    harness: object
    lifecycle: object
    _bridge: object = field(init=False, repr=False)
    _ack_bridge: object = field(init=False, repr=False)

    def __post_init__(self):
        # Core retries require this very same callback, not another bound method.
        object.__setattr__(self, '_bridge', lambda: self.check_current())
        object.__setattr__(self, '_ack_bridge', lambda: self._check_ack())

    def _check_static(self, *, acknowledged_exit=False):
        n, o = self.native, self.owned
        pending, entry = o._pending, o._entry
        token = pending.content.metadata.get(_REQUEST_KEY)
        if (n._closing or n._closed or n.engine.binding is not self.binding
                or n._native is not self.harness
                or n._native.agent is not self.agent
                or self.agent.goal_manager is not self.manager
                or pending._origin is not self.origin
                or token != self.token or entry.owned is not o or o._native is not n
                or entry.lifecycle is not self.lifecycle or entry.lifecycle.source is not o.source
                or pending._agent is not self.agent):
            raise PermissionError('original Native Goal control owner changed')
        if acknowledged_exit and entry.terminal_kind is not None:
            if (entry.terminal_kind not in _TERMINAL or not entry.terminal_event.is_set()
                    or not entry.terminal_notified or not entry.bound_notified):
                raise PermissionError('original Native Goal exit receipt unavailable')
            n._check_terminal_exit(entry, entry.terminal_kind)
        elif (n._native.active_turn is not pending or pending.abort_requested
                or n._native._capture_owned_turn(o.turn_id) is not pending
                or n._requests.get(token) is not entry or entry.terminal_kind is not None):
            raise PermissionError('original Native Goal control owner changed')

    def check_current(self):
        self._check_static()
        self.owned.source._check_current()
        self.origin._check_current()
        _sync_callback(self.checker)
        # Reentrant source/host callbacks must not make a replacement current.
        self._check_static()

    def _check_ack(self):
        # Core calls this only after the captured clear's original drain has
        # confirmed exit. It cannot authorize another operation or resource use.
        self._check_static(acknowledged_exit=True)
        _sync_callback(self.ack_checker)
        self._check_static(acknowledged_exit=True)

    def check_result(self):
        from openjiuwen.harness.goal.schema import GoalOperationError

        progress = self.selector._run
        task = progress.task
        unchanged_error = (not progress.applied and task is not None and task.done()
            and not task.cancelled() and isinstance(task.exception(), GoalOperationError))
        if task is None or unchanged_error:
            self.check_current()
            self.selector._check_static(initial=True)
            self.selector.execution._check_owned_control(self.selector.target, live=True)
        else:
            self.selector.check_result()

    async def apply(self, action, **kwargs):
        if self.selector._run.task is None:
            self.check_current()
        else:
            # Core binds retries to the same operation/arguments/callbacks;
            # only its confirmed clear receipt can select the ACK-only path.
            self._check_static(acknowledged_exit=True)
        result = await self.manager._apply_owned_control(
            self.selector, action=action, check_current=self._bridge,
            check_ack=self._ack_bridge, **kwargs,
        )
        self.check_result()
        return result

    async def get(self):
        """Read only the originally selected record for control translation."""
        self.check_current()
        self.selector._check_static(initial=True)
        result = await self.manager.get()
        self.check_current()
        self.selector._check_static(initial=True)
        return result

    async def set(self, objective, **kwargs):
        return await self.apply('set', objective=objective, **kwargs)

    async def pause(self):
        return await self.apply('pause')

    async def resume(self):
        return await self.apply('resume')

    async def clear(self):
        return await self.apply('clear')

    def __copy__(self):
        return self

    def __deepcopy__(self, _memo):
        return self

    def __reduce__(self):
        raise TypeError('Native Goal controls cannot be serialized')


def capture_native_goal_control(native, *, source, request_id, check_current, check_ack=None):
    """Capture from the explicit Runtime parent, never a routing ID or latest user."""
    if (not native._require_execution_origin or type(source) is not ExecutionOrigin
            or not isinstance(request_id, str) or not request_id.strip()
            or not callable(check_current) or (check_ack is not None and not callable(check_ack))):
        raise PermissionError('Native Goal control requires an original Runtime owner')
    pending = native._native.active_turn
    token = pending.content.metadata.get(_REQUEST_KEY) if pending is not None else None
    entry = native._requests.get(token)
    agent, binding = native._native.agent, native.engine.binding
    manager = getattr(agent, 'goal_manager', None)
    if (pending is None or entry is None or entry.lifecycle is None
            or entry.lifecycle.source is not source or entry.owned is None
            or entry.owned._pending is not pending or manager is None):
        raise PermissionError('original Native Goal Turn unavailable')
    origin = pending._origin
    capture = getattr(manager, '_capture_owned_control', None)
    if not callable(capture) or not callable(getattr(manager, '_apply_owned_control', None)):
        raise UnsupportedHarnessCapabilityError('Native exact Goal control is unavailable')
    owned = entry.owned
    # Pin all host facts before the core selector invokes any source callback.
    cap = NativeGoalControl(native, binding, owned, agent, manager, origin,
                            None, request_id, check_current, check_ack or check_current, token,
                            native._native, entry.lifecycle)
    selector = capture(expected_origin=origin)
    object.__setattr__(cap, 'selector', selector)
    cap.check_current()
    # The temporary checker must not choose a replacement Goal or Round.
    # Pin the core target first, then recheck it without another callback.
    selector._check_static(initial=True)
    selector.execution._check_owned_control(selector.target, live=True)
    return cap
