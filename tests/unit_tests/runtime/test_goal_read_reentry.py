"""Real owner/project/auth/Runtime/Session read with first identity reentry."""
from copy import copy
from types import SimpleNamespace
import pytest
from openjiuwen.core.session.agent import create_agent_session
from openjiuwen.harness.goal.schema import GoalRecord
from openjiuwen.harness.goal.store import SESSION_GOAL_RECORD_KEY
from jiuwenswarm.governance.organization_auth import authenticated_scope, current_identity
from tests.unit_tests.runtime import test_native_goal_read_runtime as component

case = component.case
source = component.source
credentials = component.credentials


async def test_first_identity_callback_cannot_adopt_replacement_hot_session(case):
    f = case
    replacement = create_agent_session(f.sid, card=f.c.session._card)
    replacement.update_state({SESSION_GOAL_RECORD_KEY: GoalRecord.create(
        session_id=f.sid, objective='replacement live Goal').to_dict()})
    instance = SimpleNamespace(card=replacement._card)
    native = SimpleNamespace(engine=f.c.native.engine, _closed=False,
        _tool_owner=(instance, object(), replacement, f.binding),
        _native=SimpleNamespace(_agent=instance, _agent_session=replacement))
    child = copy(f.child)
    child._native_execution = native
    child._instance = instance
    first = True
    def identity(_request):
        nonlocal first
        if first:
            first = False
            f.root._session_adapters[f.sid] = child
        return current_identity()
    f.runtime._trusted_identity_resolver = identity
    with authenticated_scope(f.bob):
        with pytest.raises(PermissionError):
            await f.runtime.invoke(f.request)


async def test_first_identity_callback_cannot_adopt_replacement_cold_storage(case):
    from openjiuwen.core.foundation.store.kv.in_memory_kv_store import InMemoryKVStore
    from openjiuwen.core.session.checkpointer import CheckpointerFactory
    from openjiuwen.core.session.checkpointer.persistence import PersistenceCheckpointer
    f = case
    f.manager.agents.clear()
    replacement = create_agent_session(f.sid, card=f.c.session._card)
    replacement.update_state({SESSION_GOAL_RECORD_KEY: GoalRecord.create(
        session_id=f.sid, objective='replacement persisted Goal').to_dict()})
    storage = PersistenceCheckpointer(InMemoryKVStore())
    await storage._agent_storage.save(replacement._inner)
    first = True
    def identity(_request):
        nonlocal first
        if first:
            first = False
            CheckpointerFactory.set_default_checkpointer(storage)
        return current_identity()
    f.runtime._trusted_identity_resolver = identity
    with authenticated_scope(f.bob):
        with pytest.raises(PermissionError):
            await f.runtime.invoke(f.request)
