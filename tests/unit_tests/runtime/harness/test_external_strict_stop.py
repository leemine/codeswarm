"""Real facade/adapter/Session/transport teardown; synthetic tasks, no network."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from openjiuwen.harness.engine import HarnessEngine

from jiuwenswarm.runtime.harness import execution_session as session_module
from jiuwenswarm.runtime.harness.execution_session import ExecutionExitState, ExecutionSession, ExecutionExitUnconfirmedError
from jiuwenswarm.runtime.harness.tool_transport import ManagedProductToolTransport
from jiuwenswarm.server.runtime.agent_adapter.engine_adapter import EngineAgentAdapter
from jiuwenswarm.server.runtime.agent_adapter.interface import JiuWenSwarm
from jiuwenswarm.server.runtime.session.session_manager import SessionManager
from tests.unit_tests.runtime.harness.test_external_execution_route import _route


def tree(tmp_path):
    route = _route(tmp_path, provider_id='opencode')
    harness = SimpleNamespace(stop=AsyncMock())
    session = ExecutionSession(HarnessEngine(route.bound.binding, harness), route.runtime_paths)
    session.io._stopped = False
    session._started = True
    session._exit_state = ExecutionExitState.RUNNING
    transport = ManagedProductToolTransport(None, host_session_id='session-1')
    session._tool_transport = transport
    adapter = EngineAgentAdapter(route)
    adapter._session = session
    facade = object.__new__(JiuWenSwarm)
    facade._adapter = adapter
    facade._session_manager = SessionManager()
    return SimpleNamespace(facade=facade, adapter=adapter, session=session, transport=transport,
                           harness=harness, route=route)


@pytest.mark.asyncio
async def test_facade_strict_stop_confirms_actual_transport_before_releasing_session(tmp_path):
    x = tree(tmp_path)
    consumer = SimpleNamespace(closed=False)
    async def close():
        consumer.closed = True
    consumer.close = close
    x.transport._model_consumer = consumer
    assert await x.facade.stop_existing_session_runtime('session-1')
    x.harness.stop.assert_awaited_once()
    assert x.transport.exit_confirmed and consumer.closed
    assert x.session.exit_state is ExecutionExitState.EXIT_CONFIRMED
    assert x.session.closed and x.adapter._session is None
    assert not await x.facade.stop_existing_session_runtime('session-1')


@pytest.mark.asyncio
async def test_strict_stop_other_or_missing_owner_is_not_exit_proof(tmp_path):
    x = tree(tmp_path)
    assert not await x.adapter.stop_existing_session_adapter('other')
    x.harness.stop.assert_not_awaited()
    x.adapter._session = None
    with pytest.raises(RuntimeError, match='owner unavailable'):
        await x.adapter.stop_existing_session_adapter('session-1')


@pytest.mark.asyncio
async def test_provider_stop_timeout_retains_real_session_and_retries(tmp_path, monkeypatch):
    x = tree(tmp_path)
    release = asyncio.Event()
    x.harness.stop.side_effect = release.wait
    monkeypatch.setattr(session_module, 'RESOURCE_STOP_TIMEOUT_S', .02)
    with pytest.raises(ExecutionExitUnconfirmedError):
        await x.facade.stop_existing_session_runtime('session-1')
    assert x.adapter._session is x.session and not x.session.closed
    assert x.session.exit_state is ExecutionExitState.EXIT_UNCONFIRMED
    release.set()
    assert await x.facade.stop_existing_session_runtime('session-1')
    assert x.harness.stop.await_count == 2 and x.transport.exit_confirmed


@pytest.mark.asyncio
async def test_transport_pending_model_close_retains_owner_until_retry(tmp_path, monkeypatch):
    x = tree(tmp_path)
    release = asyncio.Event()
    consumer = SimpleNamespace(closed=False)
    async def close():
        await release.wait()
        consumer.closed = True
    consumer.close = close
    x.transport._model_consumer = consumer
    monkeypatch.setattr(session_module, 'RESOURCE_STOP_TIMEOUT_S', .02)
    with pytest.raises(ExecutionExitUnconfirmedError):
        await x.facade.stop_existing_session_runtime('session-1')
    assert x.adapter._session is x.session and not x.transport.exit_confirmed
    release.set()
    assert await x.facade.stop_existing_session_runtime('session-1')
    assert consumer.closed and x.transport.exit_confirmed


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['session', 'transport', 'route', 'binding', 'subagent'])
async def test_provider_wait_cannot_select_or_stop_replacement(tmp_path, change):
    x = tree(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()
    async def stop():
        entered.set()
        await release.wait()
    x.harness.stop.side_effect = stop
    task = asyncio.create_task(x.facade.stop_existing_session_runtime('session-1'))
    await entered.wait()
    replacement = tree(tmp_path / 'replacement')
    if change == 'session':
        x.adapter._session = replacement.session
    elif change == 'transport':
        x.session._tool_transport = replacement.transport
    elif change == 'route':
        x.adapter._route = replacement.route
    elif change == 'subagent':
        x.adapter._subagent_runtime = SimpleNamespace(close=AsyncMock())
    else:
        x.session.engine = HarnessEngine(replacement.route.bound.binding, x.harness)
    untouched_close = AsyncMock()
    replacement.transport._model_consumer = SimpleNamespace(closed=False, close=untouched_close)
    release.set()
    with pytest.raises(RuntimeError, match='changed'):
        await task
    assert not x.session.closed and x.session.exit_state is ExecutionExitState.EXIT_UNCONFIRMED
    replacement.harness.stop.assert_not_awaited()
    untouched_close.assert_not_awaited()
    if change == 'subagent':
        x.adapter._subagent_runtime.close.assert_not_awaited()
    assert replacement.session.exit_state is ExecutionExitState.RUNNING
    assert x.adapter._session is (replacement.session if change == 'session' else x.session)


@pytest.mark.asyncio
async def test_session_lock_wait_rechecks_captured_transport_before_provider_stop(tmp_path):
    x = tree(tmp_path)
    await x.session._lifecycle_lock.acquire()
    task = asyncio.create_task(x.facade.stop_existing_session_runtime('session-1'))
    await asyncio.sleep(0)
    replacement = ManagedProductToolTransport(None, host_session_id='session-1')
    x.session._tool_transport = replacement
    x.session._lifecycle_lock.release()
    with pytest.raises(RuntimeError, match='changed'):
        await task
    x.harness.stop.assert_not_awaited()
    assert x.adapter._session is x.session and not x.session.closed


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['adapter', 'manager'])
async def test_facade_processor_wait_does_not_select_replacement(tmp_path, change):
    x = tree(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()
    async def processor():
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            entered.set()
            await release.wait()
    owned = asyncio.create_task(processor())
    await asyncio.sleep(0)
    x.facade._session_manager._session_processors['session-1'] = owned
    stopping = asyncio.create_task(x.facade.stop_existing_session_runtime('session-1'))
    await entered.wait()
    other = tree(tmp_path / 'replacement')
    if change == 'adapter':
        x.facade._adapter = other.adapter
    else:
        x.facade._session_manager = SessionManager()
    release.set()
    with pytest.raises(RuntimeError, match='facade owner changed'):
        await stopping
    x.harness.stop.assert_not_awaited()
    other.harness.stop.assert_not_awaited()
    assert owned.done()


@pytest.mark.asyncio
async def test_non_none_private_checker_is_not_success(tmp_path):
    x = tree(tmp_path)
    with pytest.raises(RuntimeError, match='return None'):
        await x.session.stop(ownership_check=lambda: True)
    x.harness.stop.assert_not_awaited()
    assert not x.session.closed


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', [RuntimeError('synthetic provider failure'), asyncio.CancelledError()])
async def test_failed_or_cancelled_stop_retains_original_owner(tmp_path, failure):
    x = tree(tmp_path)
    x.harness.stop.side_effect = failure
    with pytest.raises((ExecutionExitUnconfirmedError, asyncio.CancelledError)):
        await x.facade.stop_existing_session_runtime('session-1')
    assert x.adapter._session is x.session and not x.session.closed
    assert x.session.exit_state is ExecutionExitState.EXIT_UNCONFIRMED
    x.harness.stop.side_effect = None
    assert await x.facade.stop_existing_session_runtime('session-1')


@pytest.mark.asyncio
async def test_old_transport_completed_during_replace_does_not_certify_new_owner(tmp_path):
    x = tree(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()
    consumer = SimpleNamespace(closed=False)
    async def close():
        entered.set()
        await release.wait()
        consumer.closed = True
    consumer.close = close
    x.transport._model_consumer = consumer
    stopping = asyncio.create_task(x.facade.stop_existing_session_runtime('session-1'))
    await entered.wait()
    replacement = tree(tmp_path / 'replacement')
    x.adapter._session = replacement.session
    release.set()
    with pytest.raises(RuntimeError, match='changed'):
        await stopping
    assert consumer.closed and x.transport.exit_confirmed
    assert not x.session.closed and x.session.exit_state is ExecutionExitState.EXIT_UNCONFIRMED
    assert x.adapter._session is replacement.session
    replacement.harness.stop.assert_not_awaited()
