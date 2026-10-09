"""Real facade/adapter/Session/transport teardown; synthetic tasks, no network."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from openjiuwen.harness.engine import HarnessEngine
from openjiuwen.harness_protocol import (
    CheckpointReason, HarnessContext, HarnessInput, HarnessProtocolError,
)
from openjiuwen.harness_providers.opencode import (
    OpenCodeHarness, OpenCodeHarnessConfig, OpenCodeModelConfig,
)
from openjiuwen.harness_providers.opencode.errors import OpenCodeError
from jiuwenswarm.runtime.harness.recovery_store import SessionExecutionRecovery
from tests.unit_tests.runtime.harness.test_execution_recovery import recovery_env  # noqa: F401


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


@pytest.mark.asyncio
async def test_confirmed_external_stop_retires_exact_manager_cache_before_rebinding(tmp_path, monkeypatch):
    from jiuwenswarm.server.runtime.agent_manager import AgentManager

    # Keep the actual Manager cache/lock and facade cleanup chain. Only initial
    # provider allocation is a local synthetic harness; no CLI is launched.
    def facade_init(self):
        self._adapter = None
        self._runtime_execution_route = None
        self._session_manager = SessionManager()

    async def create_instance(self, _config, **kwargs):
        route = kwargs['execution_route']
        self._runtime_execution_route = route
        self._adapter = EngineAgentAdapter(route)
        harness = SimpleNamespace(stop=AsyncMock())
        owned = ExecutionSession(HarnessEngine(route.bound.binding, harness), route.runtime_paths)
        owned.io._stopped = False
        owned._started = True
        owned._exit_state = ExecutionExitState.RUNNING
        self._adapter._session = owned

    monkeypatch.setattr(JiuWenSwarm, '__init__', facade_init)
    monkeypatch.setattr(JiuWenSwarm, 'create_instance', create_instance)
    manager = AgentManager()
    first_route = _route(tmp_path, provider_id='opencode')
    other_route = _route(tmp_path, provider_id='opencode', session_id='other')
    old = await manager.get_agent(channel_id='web', mode='agent', execution_route=first_route)
    other = await manager.get_agent(channel_id='web', mode='agent', execution_route=other_route)
    old_adapter, old_session = old._adapter, old._adapter._session
    other_session = other._adapter._session
    assert await manager.stop_existing_session_runtime(channel_id='web', session_id='session-1')
    assert old_adapter._session is None and old_adapter._heartbeat_stopped_session is old_session
    assert old_session.closed and old_session.exit_state is ExecutionExitState.EXIT_CONFIRMED
    replacement_route = _route(tmp_path, provider_id='opencode')
    assert replacement_route.bound.binding == first_route.bound.binding
    assert replacement_route.bound.binding is not first_route.bound.binding
    # The original strict identity check still rejects replacing a cached root.
    with pytest.raises(RuntimeError, match='route changed'):
        old_adapter.bind_route(replacement_route)
    assert await manager.cleanup_session_runtime(channel_id='web', session_id='session-1')
    assert old not in manager.agents['web'].values()
    assert other in manager.agents['web'].values() and not other_session.closed
    assert not await manager.cleanup_session_runtime(channel_id='web', session_id='session-1')
    fresh = await manager.get_agent(channel_id='web', mode='agent', execution_route=replacement_route)
    assert fresh is not old and fresh._adapter is not old_adapter
    fresh._adapter.bind_route(replacement_route)
    assert fresh._adapter._session.binding is replacement_route.bound.binding
    await manager.cleanup_session_runtime(channel_id='web', session_id='session-1')
    await manager.cleanup_session_runtime(channel_id='web', session_id='other')


@pytest.mark.asyncio
@pytest.mark.parametrize('proof', ['absent', 'duck', 'binding', 'open', 'unknown', 'confirmed'])
async def test_cleanup_only_consumes_original_confirmed_execution_receipt(tmp_path, proof):
    from jiuwenswarm.server.runtime.agent_manager import AgentManager

    x = tree(tmp_path)
    x.facade._runtime_execution_route = x.route
    await x.adapter.stop_existing_session_adapter('session-1')
    if proof == 'absent':
        x.adapter._heartbeat_stopped_session = None
    elif proof == 'duck':
        x.adapter._heartbeat_stopped_session = SimpleNamespace(
            binding=x.route.bound.binding, closed=True, exit_state=ExecutionExitState.EXIT_CONFIRMED)
    elif proof == 'binding':
        replacement = _route(tmp_path, provider_id='opencode')
        x.session.engine = HarnessEngine(replacement.bound.binding, x.harness)
    elif proof == 'open':
        x.session._closed = False
    elif proof == 'unknown':
        x.session._exit_state = ExecutionExitState.EXIT_UNCONFIRMED
    manager = AgentManager()
    manager.agents = {'web': {'original': x.facade}}
    assert not await x.adapter.cleanup_session_adapter('other')
    assert (await manager.cleanup_session_runtime(channel_id='web', session_id='session-1')) is (proof == 'confirmed')
    assert ('original' in manager.agents.get('web', {})) is (proof != 'confirmed')
    if proof != 'confirmed':
        assert x.facade._adapter is x.adapter and x.adapter._session is None


# The native process/HTTP transport is controlled here; core's OpenCode state
# machine, IO event consumer, checkpoint publication and encrypted archive are real.


class _AbortNativeTransport:
    def __init__(self, history, mode):
        self.history, self.mode = history, mode
        self.queue = asyncio.Queue()
        self.prompted, self.aborting = asyncio.Event(), asyncio.Event()
        self.closed = False
        self.requests = []
        self.on_abort = None

    async def request(self, method, path, body=None):
        self.requests.append((method, path))
        if method == 'POST' and path == '/session':
            return {'id': 'ses_original'}
        if method == 'GET' and path == '/session/ses_original':
            return {'id': 'ses_original'}
        if path == '/session/status':
            return {}
        if path in {'/permission', '/question'}:
            return []
        if method == 'GET' and path.endswith('/message'):
            return self.history
        if method == 'GET' and path == '/session/ses_original/message/msg_original':
            return next(message for message in self.history if message['info']['id'] == 'msg_original')
        if path.endswith('/prompt_async'):
            self.user_id = body['messageID']
            self.prompted.set()
            return None
        if path.endswith('/abort'):
            self.aborting.set()
            if self.on_abort is not None:
                await self.on_abort()
            if self.mode == 'error':
                raise OpenCodeError('fixture_abort_error')
            if self.mode == 'timeout':
                await asyncio.Event().wait()
            if self.mode == 'no_idle':
                return True
            message = {'id': 'msg_original', 'role': 'assistant',
                       'parentID': self.user_id, 'sessionID': 'ses_original',
                       'time': {'completed': 1},
                       'error': {'name': 'MessageAbortedError', 'data': {'message': 'Aborted'}}}
            self.history.append({'info': message, 'parts': []})
            # Fixed CLI 1.18.18 emits an abort error and an early idle before
            # publishing the completed assistant, then confirms idle again.
            await self.queue.put({'type': 'session.error', 'properties': {
                'sessionID': 'ses_original', 'error': message['error']}})
            await self.queue.put({'type': 'session.idle', 'properties': {'sessionID': 'ses_original'}})
            await self.queue.put({'type': 'message.updated', 'properties': {'sessionID': 'ses_original', 'info': message}})
            await self.queue.put({'type': 'session.idle', 'properties': {'sessionID': 'ses_original'}})
            return True
        raise AssertionError((method, path))

    async def next_event(self):
        value = await self.queue.get()
        if value is None:
            raise OpenCodeError('event_stream_closed')
        return value

    async def close(self):
        self.closed = True
        await self.queue.put(None)


class _AbortNativeHarness(OpenCodeHarness):
    def __init__(self, history, mode='idle'):
        super().__init__(OpenCodeHarnessConfig(model=OpenCodeModelConfig(
            'fixture', 'http://127.0.0.1:1/v1', 'synthetic-key'), turn_timeout_s=30))
        self.fixture_transport = _AbortNativeTransport(history, mode)

    async def _open_session(self, ctx):
        self._transport = self.fixture_transport
        sid, resumed = await self._activate_session(ctx)
        self._session_id = sid
        await self._publish_session_checkpoint(reason=CheckpointReason.SESSION_ACTIVATED,
                                              resumable=True, state='idle', resumed=resumed)
        return sid

    async def _verify(self):
        pass


def _native_abort_tree(tmp_path, history, mode='idle'):
    route = _route(tmp_path, provider_id='opencode')
    harness = _AbortNativeHarness(history, mode)
    recovery = SessionExecutionRecovery(session_id='session-1', execution_profile_id='opencode-profile',
                                        binding=route.bound.binding, runtime_paths=route.runtime_paths)
    session = ExecutionSession(HarnessEngine(route.bound.binding, harness), route.runtime_paths,
                               recovery=recovery)
    adapter = EngineAgentAdapter(route)
    adapter._session = session
    context = HarnessContext('opencode', 'external:opencode:session-1', 'session-1', '', cwd=str(route.runtime_paths.cwd))
    return SimpleNamespace(session=session, adapter=adapter, harness=harness, recovery=recovery,
                           context=context, native=harness.fixture_transport)


@pytest.mark.asyncio
async def test_real_opencode_abort_idle_checkpoint_then_strict_stop_cold_resume(tmp_path, recovery_env):
    history = []
    x = _native_abort_tree(tmp_path, history)
    await x.session.start(x.context)
    await x.session.send(HarnessInput('ordinary slow request'))
    await x.native.prompted.wait()
    assert (await x.harness.export_checkpoint()).data['state'] == 'turn_active'
    assert await x.adapter.stop_existing_session_adapter('session-1')
    assert x.native.closed and x.session.exit_state is ExecutionExitState.EXIT_CONFIRMED
    assert ('POST', '/session/ses_original/abort') in x.native.requests
    assert ('GET', '/session/ses_original/message/msg_original') in x.native.requests
    resumed = _native_abort_tree(tmp_path, history)
    try:
        await resumed.session.start(resumed.context)
        checkpoint = await resumed.harness.export_checkpoint()
        assert checkpoint.data['resumed'] is True
        assert checkpoint.data['session_id'] == 'ses_original'
        assert ('POST', '/session') not in resumed.native.requests
        assert resumed.native.history is history and history[0]['info']['id'] == 'msg_original'
    finally:
        await resumed.session.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['timeout', 'error', 'no_idle'])
async def test_abort_failure_still_strict_stops_but_never_certifies_resume(tmp_path, recovery_env, monkeypatch, mode):
    monkeypatch.setattr(session_module, 'RESOURCE_STOP_TIMEOUT_S', .1)
    history = []
    x = _native_abort_tree(tmp_path, history, mode)
    await x.session.start(x.context)
    await x.session.send(HarnessInput('slow'))
    await x.native.prompted.wait()
    assert await x.adapter.stop_existing_session_adapter('session-1')
    assert x.native.closed and x.session.exit_state is ExecutionExitState.EXIT_CONFIRMED
    resumed = _native_abort_tree(tmp_path, history)
    with pytest.raises(HarnessProtocolError, match='confirmed idle'):
        await resumed.session.start(resumed.context)


@pytest.mark.asyncio
async def test_abort_cancellation_cleans_original_and_propagates_unknown(tmp_path, recovery_env):
    x = _native_abort_tree(tmp_path, [], 'timeout')
    await x.session.start(x.context)
    await x.session.send(HarnessInput('slow'))
    await x.native.prompted.wait()
    task = asyncio.create_task(x.adapter.stop_existing_session_adapter('session-1'))
    await x.native.aborting.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert x.native.closed
    assert x.adapter._session is x.session
    assert x.session.exit_state is ExecutionExitState.EXIT_UNCONFIRMED
    assert await x.adapter.stop_existing_session_adapter('session-1')
    assert x.native.requests.count(('POST', '/session/ses_original/abort')) == 1


@pytest.mark.asyncio
async def test_abort_owner_drift_does_not_stop_replacement(tmp_path, recovery_env):
    x = _native_abort_tree(tmp_path, [])
    await x.session.start(x.context)
    await x.session.send(HarnessInput('slow'))
    await x.native.prompted.wait()
    original_io = x.session.io
    replacement = SimpleNamespace(stop=AsyncMock())
    async def replace_io():
        x.session.io = replacement
    x.native.on_abort = replace_io
    with pytest.raises(RuntimeError, match='resources changed'):
        await x.adapter.stop_existing_session_adapter('session-1')
    replacement.stop.assert_not_awaited()
    assert not x.native.closed
    assert x.adapter._session is x.session
    x.session.io = original_io
    await x.session.stop()


@pytest.mark.asyncio
async def test_native_idle_does_not_override_failed_durable_checkpoint(tmp_path, recovery_env):
    history = []
    x = _native_abort_tree(tmp_path, history)
    await x.session.start(x.context)
    await x.session.send(HarnessInput('slow'))
    await x.native.prompted.wait()
    original_save = x.recovery._save_sync
    def fail_terminal(checkpoint, reason, expected_revision):
        if reason is CheckpointReason.TURN_COMPLETED:
            raise OSError('synthetic archive unavailable')
        return original_save(checkpoint, reason, expected_revision)
    x.recovery._save_sync = fail_terminal
    assert await x.adapter.stop_existing_session_adapter('session-1')
    assert x.native.closed and x.session.exit_state is ExecutionExitState.EXIT_CONFIRMED
    assert (await x.harness.export_checkpoint()).data['state'] == 'idle'
    resumed = _native_abort_tree(tmp_path, history)
    with pytest.raises(HarnessProtocolError, match='confirmed idle'):
        await resumed.session.start(resumed.context)


@pytest.mark.asyncio
@pytest.mark.parametrize('reason,expected', [
    ('explicit_model_required', 'compatible default model'),
    ('cli_unavailable', 'executable is not installed'),
])
async def test_startup_error_retains_safe_actionable_cause(tmp_path, monkeypatch, reason, expected):
    from openjiuwen.harness_providers.base import ProviderStartupError
    adapter = EngineAgentAdapter(_route(tmp_path, provider_id='opencode'))
    monkeypatch.setattr(adapter, '_compile_cold_surface_policy', lambda: None)
    monkeypatch.setattr(adapter, '_external_context', lambda: object())
    monkeypatch.setattr(adapter._projection, 'replay_product_artifacts', AsyncMock())
    error = ProviderStartupError('private detail must not become UI text', error=OpenCodeError(reason).turn_error())
    session = SimpleNamespace(started=False, start=AsyncMock(side_effect=error))
    with pytest.raises(RuntimeError, match=expected) as raised:
        await adapter._ensure_started(session)
    assert 'private detail' not in str(raised.value)
    assert adapter.route.provider_id == 'opencode'
    session.start.assert_awaited_once()


@pytest.mark.asyncio
async def test_builtin_runtime_directory_is_private_and_allocated_at_startup(tmp_path, monkeypatch):
    from dataclasses import replace
    from jiuwenswarm.runtime.harness.config_source import load_execution_catalog
    from jiuwenswarm.runtime.harness.binding_store import ExecutionBindingStore
    monkeypatch.setattr('jiuwenswarm.common.utils.get_agent_workspace_dir', lambda: tmp_path / 'internal')
    source = load_execution_catalog({}, selected_profile_id='builtin:opencode').source()
    runtime_root = tmp_path / 'internal' / 'opencode-runtime'
    assert not runtime_root.exists()
    route = _route(tmp_path, provider_id='opencode')
    bound = ExecutionBindingStore().bind(source, subject_id='alice', host_session_id='session-1',
                                         workspace=str(route.runtime_paths.cwd))
    adapter = EngineAgentAdapter(replace(route, source=source, bound=bound))
    adapter._route = replace(adapter.route, recovery=SimpleNamespace(execution_profile_id='builtin:opencode'))
    monkeypatch.setattr(adapter, '_compile_cold_surface_policy', lambda: None)
    monkeypatch.setattr(adapter, '_external_context', lambda: object())
    monkeypatch.setattr(adapter._projection, 'replay_product_artifacts', AsyncMock())
    session = SimpleNamespace(started=False, start=AsyncMock())
    await adapter._ensure_started(session)
    assert runtime_root.stat().st_mode & 0o777 == 0o700
    session.start.assert_awaited_once()


@pytest.mark.asyncio
async def test_ordinary_cancel_drains_terminal_before_strict_exit_and_cold_resume(tmp_path, recovery_env):
    from openjiuwen.harness_protocol import TurnEventKind
    history = []
    x = _native_abort_tree(tmp_path, history)
    await x.session.start(x.context)
    receipt = await x.session.send(HarnessInput('ordinary slow request'))
    await x.native.prompted.wait()
    owner = object()
    released = []
    def release(value):
        assert x.session.exit_state is ExecutionExitState.EXIT_CONFIRMED
        released.append(value)
    x.adapter._ordinary_owner = owner
    x.adapter._ordinary_runtime = SimpleNamespace(release_external_execution=release)
    x.adapter._ordinary_request = SimpleNamespace(request_id='ordinary')
    x.adapter._ordinary_reader_done.clear()
    async def consume():
        try:
            return [item async for item in x.session.outputs(receipt.turn_id)]
        finally:
            x.adapter._ordinary_reader_done.set()
    reader = asyncio.create_task(consume())
    try:
        result = await x.adapter.process_interrupt(SimpleNamespace(
            params={'intent':'cancel'}, request_id='cancel', channel_id='web', metadata={}))
        output = await reader
        assert result.ok
        assert any(item.terminal is TurnEventKind.ABORTED for item in output)
        assert released == [owner]
        assert x.native.closed
        x.session.abandon_output(receipt.turn_id)  # safe after the router has exited
        resumed = _native_abort_tree(tmp_path, history)
        try:
            await resumed.session.start(resumed.context)
            assert (await resumed.harness.export_checkpoint()).data['resumed'] is True
        finally:
            await resumed.session.stop()
    finally:
        if not reader.done():
            reader.cancel()
        await asyncio.gather(reader, return_exceptions=True)
        await x.session.stop()
