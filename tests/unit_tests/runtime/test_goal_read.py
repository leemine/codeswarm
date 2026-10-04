"""Read real Session/serialized persistence state without execution or repair."""
import asyncio
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from openjiuwen.core.foundation.store.kv.in_memory_kv_store import InMemoryKVStore
from openjiuwen.core.session.agent import Session, create_agent_session
from openjiuwen.core.session.checkpointer import CheckpointerFactory
from openjiuwen.core.session.checkpointer.persistence import PersistenceCheckpointer
from openjiuwen.core.single_agent import AgentCard
from openjiuwen.harness.goal.schema import GoalRecord
from openjiuwen.harness.goal.store import SESSION_GOAL_RECORD_KEY as KEY
from openjiuwen.harness_protocol import AgentExecutionSpec

from jiuwenswarm.runtime.goal_read import (
    NativeGoalReadDescriptor, NativeGoalReadUnavailable, read_native_goal,
)
from jiuwenswarm.runtime.harness.binding_store import ExecutionBindingStore
from jiuwenswarm.runtime.harness.config_source import ExecutionConfigSource
from jiuwenswarm.runtime.harness.execution_session import ExecutionExitState
from jiuwenswarm.server.runtime.agent_adapter import interface_deep


@pytest.fixture
async def source(tmp_path, monkeypatch):
    store = InMemoryKVStore()
    persistence = PersistenceCheckpointer(store)
    monkeypatch.setattr(CheckpointerFactory, '_default_checkpointer', persistence)
    spec = AgentExecutionSpec('native', 'goal-read')
    bound = ExecutionBindingStore().bind(ExecutionConfigSource(explicit=spec),
        subject_id='owner', host_session_id='owned-session', workspace=str(tmp_path))
    descriptor = NativeGoalReadDescriptor('owned-session', 'native', 'profile',
                                         bound.binding.config_revision, bound.binding.fingerprint)
    session = create_agent_session('owned-session', card=AgentCard(id=interface_deep._AGENT_CARD_ID))
    record = GoalRecord.create(session_id='owned-session', objective='private objective')
    session.update_state({KEY: record.to_dict(), 'private_context': 'must not be returned'})
    await persistence._agent_storage.save(session._inner)
    instance = SimpleNamespace(card=session._card)
    harness = SimpleNamespace(_agent=instance, _agent_session=session)
    native = SimpleNamespace(engine=SimpleNamespace(binding=bound.binding), _native=harness,
        _tool_owner=(instance, object(), session, bound.binding), _closed=False,
        _exit_state=ExecutionExitState.RUNNING)
    child = SimpleNamespace(_native_execution=native, _instance=instance,
                            _is_session_scoped_adapter=True, _parent_session_id='owned-session')
    root = SimpleNamespace(_is_session_scoped_adapter=False, _session_adapters={'owned-session': child})
    facade = SimpleNamespace(_adapter=root)
    state = SimpleNamespace(**locals(), current_facade=facade, current_descriptor=descriptor,
                            live=True, hook=None, reads=0)
    def check():
        if not state.live:
            raise PermissionError('original owner denied')
        state.reads += 1
        if state.hook:
            state.hook()
    state.check = check
    async def read(*, cold=False):
        if cold:
            state.current_facade = None
        return await read_native_goal(descriptor=descriptor, lookup_descriptor=lambda: state.current_descriptor,
            lookup_facade=lambda: state.current_facade, check_owner=check,
            binding=None if cold else bound.binding)
    state.read = read
    # These lifecycle operations are not read primitives, even for ACTIVE data.
    for name in ('pre_run', 'commit'):
        monkeypatch.setattr(Session, name, Mock(side_effect=AssertionError('read cannot execute/write')))
    monkeypatch.setattr(interface_deep, 'ensure_persistent_checkpointer',
                        Mock(side_effect=AssertionError('read cannot configure persistence')))
    yield state


@pytest.mark.parametrize('cold', [False, True])
async def test_active_goal_is_copied_without_execution_checkpoint_or_full_context(source, cold):
    c = source
    original = deepcopy(c.store._store)
    result = await c.read(cold=cold)
    payload = result.to_payload()
    assert payload['goal']['status'] == 'active'
    assert payload['goal']['goal_id'] == c.record.goal_id
    assert 'private_context' not in repr(payload)
    payload['goal']['objective'] = 'caller mutation'
    assert result.to_payload()['goal']['objective'] == 'private objective'
    assert c.session.get_state(KEY)['objective'] == 'private objective'
    assert c.store._store == original


@pytest.mark.parametrize('cold', [False, True])
@pytest.mark.parametrize('value', [None, 'malformed', {'objective': 'bad'}, {'session_id': 'foreign'}])
async def test_missing_or_invalid_goal_is_never_repaired(source, cold, value):
    c = source
    c.session.update_state({KEY: None})
    if value is not None:
        c.session.update_state({KEY: value})
    await c.persistence._agent_storage.save(c.session._inner)
    original, raw = deepcopy(c.store._store), c.session.get_state(KEY)
    if value is None:
        assert (await c.read(cold=cold)).to_payload()['goal'] is None
    else:
        with pytest.raises(NativeGoalReadUnavailable, match='field is invalid'):
            await c.read(cold=cold)
    assert c.store._store == original and c.session.get_state(KEY) == raw


@pytest.mark.parametrize('change', ['facade', 'root', 'child', 'native', 'session', 'binding', 'binding_value', 'descriptor', 'owner'])
async def test_hot_final_delivery_rechecks_original_objects(source, change):
    c = source
    result = await c.read()
    if change == 'facade':
        c.current_facade = SimpleNamespace(_adapter=c.root)
    elif change == 'root':
        c.facade._adapter = SimpleNamespace(**vars(c.root))
    elif change == 'child':
        c.root._session_adapters['owned-session'] = SimpleNamespace(**vars(c.child))
    elif change == 'native':
        c.child._native_execution = SimpleNamespace(**vars(c.native))
    elif change == 'session':
        c.harness._agent_session = create_agent_session('owned-session', card=c.session._card)
    elif change == 'binding':
        c.native.engine.binding = replace(c.bound.binding)
    elif change == 'binding_value':
        object.__setattr__(c.bound.binding, 'subject_id', 'different')
    elif change == 'descriptor':
        c.current_descriptor = replace(c.descriptor)
    else:
        c.live = False
    with pytest.raises(PermissionError):
        result.final_check()


@pytest.mark.parametrize('change', ['facade', 'storage', 'checkpointer', 'kv', 'descriptor_value', 'owner'])
async def test_cold_await_cannot_switch_source(source, change, monkeypatch):
    c = source
    entered, release = asyncio.Event(), asyncio.Event()
    original = c.store._get_without_lock
    async def get(key):
        entered.set()
        await release.wait()
        return await original(key)
    monkeypatch.setattr(c.store, '_get_without_lock', get)
    task = asyncio.create_task(c.read(cold=True))
    await asyncio.wait_for(entered.wait(), 2)
    if change == 'facade':
        c.current_facade = c.facade
    elif change == 'storage':
        c.persistence._agent_storage = PersistenceCheckpointer(c.store)._agent_storage
    elif change == 'checkpointer':
        CheckpointerFactory.set_default_checkpointer(PersistenceCheckpointer(c.store))
    elif change == 'kv':
        c.persistence._agent_storage._kv_store = InMemoryKVStore()
    elif change == 'descriptor_value':
        object.__setattr__(c.descriptor, 'config_revision', 'late')
    else:
        c.live = False
    release.set()
    with pytest.raises(PermissionError):
        await task


@pytest.mark.parametrize('cold', [False, True])
async def test_first_owner_callback_cannot_reselect_mapping(source, cold):
    c = source
    c.hook = lambda: setattr(c, 'current_facade', SimpleNamespace(_adapter=c.root))
    with pytest.raises(NativeGoalReadUnavailable, match='source changed'):
        await c.read(cold=cold)


async def test_confirmed_closed_native_can_read_existing_checkpoint(source):
    c = source
    c.native._tool_owner = None
    c.native._closed = True
    c.native._exit_state = ExecutionExitState.EXIT_CONFIRMED
    result = await c.read()
    assert result.to_payload()['goal']['goal_id'] == c.record.goal_id
    c.native._closed = False
    with pytest.raises(NativeGoalReadUnavailable):
        result.final_check()


@pytest.mark.parametrize('problem', ['partial_native', 'unknown_exit', 'legacy_instance', 'plugin', 'wrong_provider'])
async def test_unproved_cold_paths_are_explicitly_unsupported(source, problem):
    c = source
    c.native._tool_owner = None
    if problem == 'unknown_exit':
        c.native._closed = True
        c.native._exit_state = ExecutionExitState.EXIT_UNCONFIRMED
    elif problem == 'legacy_instance':
        c.child._native_execution = None
    elif problem == 'plugin':
        CheckpointerFactory.set_default_checkpointer(object())
        c.current_facade = None
    elif problem == 'wrong_provider':
        object.__setattr__(c.descriptor, 'provider_id', 'opencode')
    with pytest.raises(NativeGoalReadUnavailable):
        await c.read()


async def test_cold_result_rechecks_configured_store_and_new_hot_mapping(source):
    c = source
    result = await c.read(cold=True)
    c.current_facade = c.facade
    with pytest.raises(NativeGoalReadUnavailable):
        result.final_check()
    c.current_facade = None
    CheckpointerFactory.set_default_checkpointer(PersistenceCheckpointer(c.store))
    with pytest.raises(NativeGoalReadUnavailable):
        result.final_check()


async def test_missing_checkpoint_and_known_whole_blob_limitation_do_not_write(source):
    c = source
    c.store._store.clear()
    assert (await c.read(cold=True)).to_payload()['goal'] is None
    await c.persistence._agent_storage.save(c.session._inner)
    # Existing core recover deliberately conflates deserialization failure with
    # absence. This documents the retained limit, not strict absence proof.
    for key, (value, expiry) in list(c.store._store.items()):
        if not key.endswith('dump_type'):
            c.store._store[key] = (b'not a serialized checkpoint', expiry)
    original = deepcopy(c.store._store)
    assert (await c.read(cold=True)).to_payload()['goal'] is None
    assert c.store._store == original


@pytest.mark.parametrize('cold', [False, True])
async def test_valid_goal_from_foreign_session_is_not_disclosed(source, cold):
    c = source
    record = GoalRecord.create(session_id='other-owner-session', objective='other private goal')
    c.session.update_state({KEY: record.to_dict()})
    await c.persistence._agent_storage.save(c.session._inner)
    original = deepcopy(c.store._store)
    with pytest.raises(NativeGoalReadUnavailable, match='field is invalid'):
        await c.read(cold=cold)
    assert c.store._store == original and c.session.get_state(KEY)['session_id'] == 'other-owner-session'


async def test_legacy_root_instance_does_not_fall_back_to_cold_disk(source):
    c = source
    c.root._session_adapters.clear()
    c.root._instance = c.instance
    with pytest.raises(NativeGoalReadUnavailable, match='Legacy root'):
        await c.read()
