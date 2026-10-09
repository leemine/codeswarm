from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from openjiuwen.harness_protocol import BeforeToolContext

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.governance.contracts import TrustedIdentity
from jiuwenswarm.governance.resources import ResourceDecision, ResourceRequest
from jiuwenswarm.governance.tool_resources import ToolResourceUse
from jiuwenswarm.runtime import AgentRuntime
from jiuwenswarm.runtime.session.model import RuntimeSessionState, SessionExecutionState
from jiuwenswarm.server.runtime.agent_adapter.engine_adapter import EngineAgentAdapter
from jiuwenswarm.server.runtime.session import project_store
from jiuwenswarm.server.runtime.session.project_access import ProjectAccessStore


@pytest.mark.asyncio
async def test_runtime_resource_callback_binds_identity_and_owned_generation(tmp_path, monkeypatch):
    monkeypatch.setattr(project_store, 'get_agent_root_dir', lambda: tmp_path)
    project_store.invalidate_cache()
    project = project_store.create_project('Resources', str(tmp_path))
    access = ProjectAccessStore()
    access.initialize(project.project_id, 'alice')
    state = SimpleNamespace(identity=TrustedIdentity('alice', 'alice', 'host'), allowed=True, calls=0)
    execution = SimpleNamespace(execution_id='original-execution', request_id='r', state=SessionExecutionState.RUNNING, cancellation_requested=False)
    snapshot = SimpleNamespace(generation=1, state=RuntimeSessionState.ACTIVE, executions=(execution,))
    def authorize(pid, identity, request):
        state.calls += 1
        return ResourceDecision(state.allowed, pid, identity.actor_id, identity.subject_id,
                                request, 1, 1, reference='native:read_file')
    resources = SimpleNamespace(authorize_resource=authorize, resources_for_tool=lambda *_: (
        ToolResourceUse(ResourceRequest('read-tool', 'invoke'), 'native:read_file'),
    ))
    runtime = AgentRuntime(initializer=AsyncMock(), trusted_identity_resolver=lambda _: state.identity,
                           resource_authorizer=resources)
    runtime._started = True
    monkeypatch.setattr(runtime, '_governance_project', lambda *_args, **_kwargs: project.project_id)
    monkeypatch.setattr(runtime._session_coordinator, 'snapshot_session', lambda _: snapshot)
    req = AgentRequest('r', session_id='private', req_method=ReqMethod.CHAT_SEND)
    callback = runtime._resource_authorizers_for(req)['native']
    tool = BeforeToolContext('a', 'private', 'turn', 'call', 'read_file', {})
    assert await callback(tool)
    state.allowed = False
    assert not await callback(tool)
    state.allowed = True
    state.identity = TrustedIdentity('bob', 'bob', 'host')
    assert not await callback(tool)
    state.identity = TrustedIdentity('alice', 'alice', 'host')
    snapshot.generation = 2
    assert not await callback(tool)
    snapshot.generation = 1
    execution.cancellation_requested = True
    assert not await callback(tool)
    assert state.calls == 2
    project_store.invalidate_cache()


@pytest.mark.asyncio
async def test_external_authority_never_borrows_next_turn_callback():
    adapter = EngineAgentAdapter.__new__(EngineAgentAdapter)
    alice, bob = AsyncMock(return_value=True), AsyncMock(return_value=True)
    adapter._turn_resource_authorizers = {'old': alice, 'new': bob}
    old = BeforeToolContext('a', 'p', 'old', 'call', 'read', {})
    new = replace(old, turn_id='new')
    assert await adapter._authorize_resource_tool(old)
    assert await adapter._authorize_resource_tool(new)
    adapter._turn_resource_authorizers.pop('old')
    assert not await adapter._authorize_resource_tool(old)
    assert not await adapter._authorize_resource_tool(replace(old, turn_id=None))
    assert bob.await_count == 1


@pytest.mark.asyncio
async def test_default_runtime_maps_only_exact_owned_opencode_session(tmp_path, monkeypatch):
    from jiuwenswarm.governance.resources import ResourceDefinition
    from jiuwenswarm.runtime.service import _StoredResourceAuthority
    monkeypatch.setenv('JIUWENSWARM_DATA_DIR', str(tmp_path / 'data'))
    monkeypatch.setattr(project_store, 'get_agent_root_dir', lambda: tmp_path / 'projects')
    project_store.invalidate_cache()
    project = project_store.create_project('OpenCode Resources', str(tmp_path))
    access = ProjectAccessStore()
    access.initialize(project.project_id, 'alice')
    identity = TrustedIdentity('alice', 'alice', 'host')
    for revision, (rid, kind, reference, actions) in enumerate([
        ('tool', 'tool', 'opencode:read', ('invoke',)),
        ('workspace', 'workspace', str(tmp_path), ('read',)),
    ]):
        access.register_resource(project.project_id, ResourceDefinition(rid, kind, reference),
                                 owner_subject_id='alice', actions=actions, expected_revision=revision)
    execution = SimpleNamespace(execution_id='original-execution', request_id='r', state=SessionExecutionState.RUNNING, cancellation_requested=False)
    snapshot = SimpleNamespace(generation=1, state=RuntimeSessionState.ACTIVE, executions=(execution,))
    owner = SimpleNamespace(owns_external_tool_session=lambda actual, sid: actual.session_id == 'private' and sid == 'native-issued')
    manager = SimpleNamespace(get_agent_for_session_nowait=lambda channel, sid: owner if (channel, sid) == ('web', 'private') else None)
    runtime = AgentRuntime(initializer=AsyncMock(), agent_manager=manager,
                           trusted_identity_resolver=lambda _: identity,
                           resource_authorizer=_StoredResourceAuthority())
    runtime._started = True
    monkeypatch.setattr(runtime, '_governance_project', lambda *_args, **_kwargs: project.project_id)
    monkeypatch.setattr(runtime._session_coordinator, 'snapshot_session', lambda _: snapshot)
    req = AgentRequest('r', channel_id='web', session_id='private', req_method=ReqMethod.CHAT_SEND)
    callback = runtime._resource_authorizers_for(req)['opencode']
    call = BeforeToolContext('a', 'native-issued', 'turn', 'call', 'read', {'filePath': str(tmp_path / 'fixture')})
    try:
        assert await callback(call)
        assert not await callback(replace(call, provider_session_id='private'))
        access.revoke_resource(project.project_id, identity, 'workspace', subject_id='alice', expected_revision=2)
        assert not await callback(call)
        execution.cancellation_requested = True
        assert not await callback(call)
    finally:
        project_store.invalidate_cache()


@pytest.mark.asyncio
@pytest.mark.parametrize('revoked', [False, True])
async def test_overlapping_resource_checks_keep_original_context_and_live_revocation(
    tmp_path, monkeypatch, revoked,
):
    import asyncio
    from contextvars import ContextVar
    import threading

    monkeypatch.setattr(project_store, 'get_agent_root_dir', lambda: tmp_path)
    project_store.invalidate_cache()
    project = project_store.create_project('Concurrent authority', str(tmp_path))
    access = ProjectAccessStore()
    access.initialize(project.project_id, 'alice')
    alice = TrustedIdentity('alice', 'alice', 'host')
    bob = TrustedIdentity('bob', 'bob', 'host')
    principal = ContextVar('resource_check_principal', default=None)
    entered, release = threading.Event(), threading.Event()
    state = SimpleNamespace(block=False, revoked=False, checked=[])

    def identity(_):
        if state.block and threading.current_thread() is not threading.main_thread():
            state.block = False
            entered.set()
            assert release.wait(3)
        return None if state.revoked else principal.get()

    def authorize(pid, subject, request):
        state.checked.append(subject)
        return ResourceDecision(True, pid, subject.actor_id, subject.subject_id,
                                request, 1, 1, reference='native:read_file')

    resources = SimpleNamespace(
        authorize_resource=authorize,
        resources_for_tool=lambda *_: (
            ToolResourceUse(ResourceRequest('read-tool', 'invoke'), 'native:read_file'),
        ),
    )
    runtime = AgentRuntime(initializer=AsyncMock(), trusted_identity_resolver=identity,
                           resource_authorizer=resources)
    runtime._started = True
    execution = SimpleNamespace(execution_id='original', request_id='r',
        state=SessionExecutionState.RUNNING, cancellation_requested=False)
    snapshot = SimpleNamespace(generation=1, state=RuntimeSessionState.ACTIVE,
                               executions=(execution,))
    monkeypatch.setattr(runtime, '_governance_project', lambda *_args, **_kwargs: project.project_id)
    monkeypatch.setattr(runtime._session_coordinator, 'snapshot_session', lambda _: snapshot)
    request = AgentRequest('r', session_id='private', req_method=ReqMethod.CHAT_SEND)
    token = principal.set(alice)
    worker = None
    try:
        callback = runtime._resource_authorizers_for(request)['native']
        tool = BeforeToolContext('a', 'private', 'turn', 'call', 'read_file', {})
        assert await callback(tool)
        principal.set(bob)
        state.block = True
        # Exercise the complete Native authority, including its instance
        # catalog/MCP wrapper, from a worker with its own event loop.
        worker = asyncio.create_task(asyncio.to_thread(lambda: asyncio.run(callback(tool))))
        assert await asyncio.wait_for(asyncio.to_thread(entered.wait, 3), 4)
        state.revoked = revoked
        # The worker is inside the original captured Context. Another valid
        # consumer must recheck under that same principal without entering
        # the very same Context object concurrently or borrowing Bob.
        overlap = await callback(tool)
    finally:
        release.set()
        if worker is not None:
            worker_result = await asyncio.wait_for(worker, 4)
        principal.reset(token)
        await runtime._session_coordinator.close()
        project_store.invalidate_cache()
    assert overlap is (not revoked)
    assert worker_result is (not revoked)
    assert state.checked and all(subject == alice for subject in state.checked)
