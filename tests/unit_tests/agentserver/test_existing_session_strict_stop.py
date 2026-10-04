"""Exact existing Native teardown; synthetic tasks only, no Provider/network."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from openjiuwen.core.controller.modules.task_scheduler import TaskScheduler
from jiuwenswarm.runtime.harness import execution_session
from jiuwenswarm.runtime.harness.execution_session import ExecutionExitState
from jiuwenswarm.server.runtime.agent_adapter.interface import JiuWenSwarm
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter
from jiuwenswarm.server.runtime.agent_manager import AgentManager
from jiuwenswarm.server.runtime.session.session_manager import SessionManager


def tree(execution):
    child = object.__new__(JiuWenSwarmDeepAdapter)
    child._is_session_scoped_adapter = True
    child._parent_session_id = 's'
    child._native_execution = execution
    child._native_execution_bindings = SimpleNamespace(release=Mock())
    child.cleanup = AsyncMock()
    root = object.__new__(JiuWenSwarmDeepAdapter)
    root._is_session_scoped_adapter = False
    root._session_adapters = {'s': child}
    root._session_adapter_locks = {'s': asyncio.Lock()}
    root._session_adapter_last_used = {}
    root._session_adapter_versions = {}
    root._session_adapter_reload_failures = {}
    root._native_session_routes = {'s': 'native'}
    root._active_session_ids = {}
    root._session_agent_tasks = {}
    root._get_or_create_session_adapter = AsyncMock(side_effect=AssertionError('create forbidden'))
    facade = object.__new__(JiuWenSwarm)
    facade._adapter = root
    facade._session_manager = SessionManager()
    manager = object.__new__(AgentManager)
    manager.agents = {'web': {'bound': facade}}
    manager.get_agent = AsyncMock(side_effect=AssertionError('create forbidden'))
    manager.get_agent_nowait = Mock(side_effect=AssertionError('fallback forbidden'))
    return manager, facade, root, child


def native(agent=None):
    execution = SimpleNamespace(_native=SimpleNamespace(agent=agent), engine=SimpleNamespace(binding=object()),
                                exit_state=ExecutionExitState.RUNNING)
    async def stop():
        execution.exit_state = ExecutionExitState.EXIT_CONFIRMED
    execution.stop = AsyncMock(side_effect=stop)
    return execution


@pytest.mark.asyncio
@pytest.mark.parametrize('state', ['waiting-control', 'detached-active'])
async def test_strict_stop_uses_existing_owner_even_when_idle_cleanup_would_keep_it(state):
    execution = native()
    manager, _, root, child = tree(execution)
    child._has_live_root_permission_owner = lambda _: state == 'waiting-control'
    child.is_session_active = lambda _: state == 'detached-active'
    child.is_deep_agent_executing_for_session = lambda _: True
    bindings = child._native_execution_bindings
    assert await manager.stop_existing_session_runtime(channel_id='web', session_id='s')
    execution.stop.assert_awaited_once()
    child.cleanup.assert_awaited_once()
    bindings.release.assert_called_once_with(execution.engine.binding)
    assert 's' not in root._session_adapters
    root._get_or_create_session_adapter.assert_not_called()
    manager.get_agent.assert_not_called()
    manager.get_agent_nowait.assert_not_called()


@pytest.mark.asyncio
async def test_absent_session_never_selects_another_agent_or_creates():
    manager, _, root, child = tree(native())
    assert not await manager.stop_existing_session_runtime(channel_id='web', session_id='other')
    assert not await manager.stop_existing_session_runtime(channel_id='other', session_id='s')
    child.cleanup.assert_not_called()
    assert root._session_adapters['s'] is child


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', [RuntimeError('provider still running'), asyncio.CancelledError()])
async def test_stop_failure_or_caller_cancel_keeps_original_cache_and_binding(failure):
    execution = native()
    execution.stop.side_effect = failure
    manager, _, root, child = tree(execution)
    bindings = child._native_execution_bindings
    with pytest.raises(type(failure)):
        await manager.stop_existing_session_runtime(channel_id='web', session_id='s')
    assert child._native_execution is execution and root._session_adapters['s'] is child
    bindings.release.assert_not_called()
    child.cleanup.assert_not_called()


@pytest.mark.asyncio
async def test_late_lock_wait_does_not_stop_replaced_child():
    manager, _, root, original = tree(native())
    replacement = SimpleNamespace(stop_interaction=AsyncMock(), cleanup=AsyncMock())
    lock = root._session_adapter_locks['s']
    await lock.acquire()
    stop = asyncio.create_task(manager.stop_existing_session_runtime(channel_id='web', session_id='s'))
    await asyncio.sleep(0)
    root._session_adapters['s'] = replacement
    lock.release()
    with pytest.raises(RuntimeError, match='changed before stop'):
        await stop
    original.cleanup.assert_not_called()
    replacement.stop_interaction.assert_not_called()


@pytest.mark.asyncio
async def test_core_stop_can_return_with_owned_tool_task_alive_and_host_refuses_false_exit(monkeypatch):
    started, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    async def stubborn_tool():
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.set()
            await release.wait()
    tool = asyncio.create_task(stubborn_tool())
    await started.wait()
    scheduler = object.__new__(TaskScheduler)
    scheduler._running = True
    scheduler._running_tasks = {'synthetic-tool': (object(), tool)}
    scheduler._lock = asyncio.Lock()
    scheduler._scheduler_task = None
    agent = SimpleNamespace(loop_controller=SimpleNamespace(task_scheduler=scheduler))
    execution = native(agent)
    async def stop():
        await TaskScheduler.stop(scheduler)  # Real installed core implementation.
        execution.exit_state = ExecutionExitState.EXIT_CONFIRMED
        execution._native.agent = None  # Provider may release its facade.
    execution.stop.side_effect = stop
    manager, _, root, child = tree(execution)
    bindings = child._native_execution_bindings
    monkeypatch.setattr(execution_session, 'RESOURCE_STOP_TIMEOUT_S', 0.02)
    try:
        with pytest.raises(RuntimeError, match='owned execution tasks have not exited'):
            await manager.stop_existing_session_runtime(channel_id='web', session_id='s')
        assert cancelled.is_set() and not tool.done()
        assert execution.exit_state is ExecutionExitState.EXIT_CONFIRMED
        assert child._native_execution is execution and root._session_adapters['s'] is child
        assert tool in child._native_pending_exit_tasks
        bindings.release.assert_not_called()
        child.cleanup.assert_not_called()
        release.set()
        await tool
        assert await manager.stop_existing_session_runtime(channel_id='web', session_id='s')
        bindings.release.assert_called_once()
        assert not child._native_pending_exit_tasks
    finally:
        release.set()
        await tool


@pytest.mark.asyncio
async def test_false_provider_exit_flag_does_not_release_binding():
    execution = native()
    execution.stop.side_effect = None
    manager, _, root, child = tree(execution)
    bindings = child._native_execution_bindings
    with pytest.raises(RuntimeError, match='provider exit is not confirmed'):
        await manager.stop_existing_session_runtime(channel_id='web', session_id='s')
    bindings.release.assert_not_called()
    assert root._session_adapters['s'] is child


@pytest.mark.asyncio
async def test_strict_stop_does_not_fall_back_to_unproven_legacy_instance():
    manager, _, root, child = tree(None)
    child._instance = SimpleNamespace(stop=AsyncMock())
    with pytest.raises(RuntimeError, match='strict Native execution owner unavailable'):
        await manager.stop_existing_session_runtime(channel_id='web', session_id='s')
    child._instance.stop.assert_not_called()
    assert root._session_adapters['s'] is child
