"""Owner-only Native Goal snapshots without constructing an executor.

This private reader deliberately does not call SessionGoalStore.load: malformed
Goal fields must not repair the original Session. A configured persistence
recover still has its existing whole-blob corruption/absence ambiguity.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, fields
from typing import Callable

from openjiuwen.core.session.agent import Session, create_agent_session
from openjiuwen.core.session.checkpointer import CheckpointerFactory
from openjiuwen.core.session.checkpointer.persistence import AgentStorage, PersistenceCheckpointer
from openjiuwen.core.single_agent import AgentCard
from openjiuwen.harness.engine import ExecutionBinding
from openjiuwen.harness.goal.schema import GoalRecord
from openjiuwen.harness.goal.store import SESSION_GOAL_RECORD_KEY

from jiuwenswarm.runtime.harness.execution_session import ExecutionExitState


class NativeGoalReadUnavailable(PermissionError):
    """No verified read-only Native source is available for this request."""


@dataclass(frozen=True, slots=True)
class NativeGoalReadDescriptor:
    session_id: str
    provider_id: str
    execution_profile_id: str
    config_revision: str
    config_fingerprint: str

    def __post_init__(self):
        if any(type(getattr(self, field.name)) is not str or not getattr(self, field.name)
               for field in fields(self)) or self.provider_id != 'native':
            raise NativeGoalReadUnavailable('Explicit Native Goal read description required')


@dataclass(frozen=True, repr=False)
class NativeGoalRead:
    _goal: dict | None
    final_check: Callable[[], None]

    def to_payload(self):
        self.final_check()
        goal = deepcopy(self._goal)
        return {'result_type': 'goal_control', 'action': 'get', 'goal': goal,
                'output': 'No goal in this session.' if goal is None else f"Goal: {goal['objective']}"}


def _descriptor_values(value):
    return tuple(getattr(value, field.name) for field in fields(NativeGoalReadDescriptor))


async def read_native_goal(*, descriptor: NativeGoalReadDescriptor,
                           lookup_descriptor: Callable[[], NativeGoalReadDescriptor],
                           lookup_facade: Callable[[], object | None],
                           check_owner: Callable[[], None],
                           binding: ExecutionBinding | None = None) -> NativeGoalRead:
    """Read one pinned hot Session or already configured cold checkpoint.

    Host callbacks must only look up existing state. ``check_owner`` binds the
    complete principal, original Session metadata and read permission; this
    descriptor does not itself grant authority. No Binding is allocated here.
    """
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import _AGENT_CARD_ID

    if type(descriptor) is not NativeGoalReadDescriptor:
        raise NativeGoalReadUnavailable('Explicit Native Goal read description required')
    descriptor.__post_init__()
    values = _descriptor_values(descriptor)
    sid = descriptor.session_id
    facade = lookup_facade()
    root = getattr(facade, '_adapter', None)
    if root is not None and type(getattr(root, '_is_session_scoped_adapter', None)) is not bool:
        raise NativeGoalReadUnavailable('Unsupported cached Goal executor')
    scoped = getattr(root, '_is_session_scoped_adapter', False) is True
    if root is not None and not scoped and getattr(root, '_instance', None) is not None:
        raise NativeGoalReadUnavailable('Legacy root Goal Session requires an explicit reader')
    slots = None if scoped else getattr(root, '_session_adapters', None)
    if slots is not None and type(slots) is not dict:
        raise NativeGoalReadUnavailable('Unsupported Native Goal cache')
    child = root if scoped else slots.get(sid) if slots is not None else None
    if child is not None and (getattr(child, '_is_session_scoped_adapter', False) is not True
                              or getattr(child, '_parent_session_id', None) != sid):
        raise NativeGoalReadUnavailable('Native Goal child Session differs')
    instance = getattr(child, '_instance', None)
    native = getattr(child, '_native_execution', None)
    engine = getattr(native, 'engine', None)
    owner = getattr(native, '_tool_owner', None)
    session = owner[2] if type(owner) is tuple and len(owner) == 4 else None
    cold = session is None
    exited = (native is not None and native._closed is True
              and native._exit_state is ExecutionExitState.EXIT_CONFIRMED and owner is None)
    if cold:
        if (native is not None and not exited) or (instance is not None and not exited):
            raise NativeGoalReadUnavailable('Native Goal Session is not an idle readable source')
    elif (type(session) is not Session or binding is None or owner[3] is not binding
          or engine is None or engine.binding is not binding or owner[0] is not instance
          or getattr(child, '_parent_session_id', None) != sid
          or binding.provider_id != 'native' or binding.host_session_id != sid
          or session.get_session_id() != sid or session.get_agent_id() != _AGENT_CARD_ID):
        raise NativeGoalReadUnavailable('Native Goal Session ownership differs')
    if native is not None and (engine is None or engine.binding.provider_id != 'native'
                               or engine.binding.host_session_id != sid):
        raise NativeGoalReadUnavailable('Native Goal Binding differs')
    original_binding = getattr(engine, 'binding', None)
    if original_binding is not None and (original_binding.config_revision != descriptor.config_revision
                                        or original_binding.fingerprint != descriptor.config_fingerprint):
        raise NativeGoalReadUnavailable('Native Goal configuration differs')
    if original_binding is not None and type(original_binding) is not ExecutionBinding:
        raise NativeGoalReadUnavailable('Unsupported Native Goal Binding')
    binding_values = (tuple(getattr(original_binding, field.name) for field in fields(ExecutionBinding))
                      if original_binding is not None else None)
    harness = getattr(native, '_native', None)
    if not cold and (getattr(harness, '_agent', None) is not instance
                     or getattr(harness, '_agent_session', None) is not session):
        raise NativeGoalReadUnavailable('Native Goal actual Session differs')
    card = getattr(instance, 'card', None)
    session_card = getattr(session, '_card', None)
    session_inner = getattr(session, '_inner', None)
    checkpointer = CheckpointerFactory.get_checkpointer() if cold else None
    storage = getattr(checkpointer, '_agent_storage', None)
    kv = getattr(checkpointer, '_kv_store', None)
    serde = getattr(storage, '_serde', None)
    recover = getattr(storage, 'recover', None)
    if cold and (type(checkpointer) is not PersistenceCheckpointer or type(storage) is not AgentStorage
                 or getattr(recover, '__func__', None) is not AgentStorage.recover
                 or storage._kv_store is not kv):
        raise NativeGoalReadUnavailable('Configured read-only Native persistence is unsupported')

    def facts():
        if (lookup_descriptor() is not descriptor or _descriptor_values(descriptor) != values
                or lookup_facade() is not facade or getattr(facade, '_adapter', None) is not root
                or getattr(root, '_is_session_scoped_adapter', False) is not scoped
                or (not scoped and getattr(root, '_instance', None) is not None)
                or (not scoped and (getattr(root, '_session_adapters', None) is not slots
                                    or (slots is not None and slots.get(sid) is not child)))
                or (child is not None and (getattr(child, '_is_session_scoped_adapter', False) is not True
                                          or getattr(child, '_parent_session_id', None) != sid))
                or getattr(child, '_instance', None) is not instance
                or getattr(child, '_native_execution', None) is not native
                or getattr(native, 'engine', None) is not engine
                or getattr(engine, 'binding', None) is not original_binding
                or (original_binding is not None
                    and tuple(getattr(original_binding, field.name) for field in fields(ExecutionBinding)) != binding_values)
                or getattr(native, '_tool_owner', None) is not owner
                or getattr(native, '_native', None) is not harness
                or getattr(instance, 'card', None) is not card):
            return False
        if cold:
            return ((not exited or (native._closed is True
                                    and native._exit_state is ExecutionExitState.EXIT_CONFIRMED))
                    and CheckpointerFactory.get_checkpointer() is checkpointer
                    and checkpointer._agent_storage is storage and checkpointer._kv_store is kv
                    and storage._kv_store is kv and storage._serde is serde
                    and storage.recover == recover)
        return (getattr(harness, '_agent', None) is instance
                and getattr(harness, '_agent_session', None) is session
                and getattr(child, '_parent_session_id', None) == sid
                and session._card is session_card and session._inner is session_inner
                and session.get_session_id() == sid and session.get_agent_id() == _AGENT_CARD_ID)

    def final_check():
        if not facts():
            raise NativeGoalReadUnavailable('Original Native Goal read source changed')
        if check_owner() is not None:
            raise NativeGoalReadUnavailable('Native Goal read owner check failed')
        if not facts():
            raise NativeGoalReadUnavailable('Original Native Goal read source changed')

    final_check()
    read_session = session
    if cold:
        detached = create_agent_session(session_id=sid, card=AgentCard(id=_AGENT_CARD_ID))
        final_check()
        await recover(detached._inner)
        final_check()
        read_session = detached
    raw = deepcopy(read_session.get_state(SESSION_GOAL_RECORD_KEY))
    if raw is None:
        goal = None
    else:
        try:
            record = GoalRecord.from_dict(raw) if type(raw) is dict else None
            valid = record is not None and record.session_id == sid
        except (KeyError, ValueError, TypeError, OverflowError):
            valid = False
        if not valid:
            raise NativeGoalReadUnavailable('Persisted Native Goal field is invalid')
        goal = record.to_dict()
    final_check()
    return NativeGoalRead(goal, final_check)
