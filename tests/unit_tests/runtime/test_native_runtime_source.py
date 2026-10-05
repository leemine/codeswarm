"""Actual Runtime/registry/host store -> real Native core ordinary Turn.

Allocation shape and model/Session IO are fixtures. No Runtime/source/registry
methods are replaced. The Manager lookup and facade/root/child ownership methods
are production implementations.
"""
import asyncio
import hashlib
import json
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from openjiuwen.harness.schema.interaction import SendInputRequest
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.governance.organization_auth import authenticated_scope, current_identity, CONFIG_ENV
from jiuwenswarm.governance.resources import ResourceDefinition
from jiuwenswarm.governance.tool_context import tool_authority_scope
from jiuwenswarm.runtime import AgentRuntime
from jiuwenswarm.runtime.session import SessionWorkKind
from jiuwenswarm.runtime.session.model import SessionExecutionState
from jiuwenswarm.server.runtime.agent_adapter.interface import JiuWenSwarm
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter
from jiuwenswarm.server.runtime.agent_manager import AgentManager
from jiuwenswarm.server.runtime.session import project_store, session_metadata
from jiuwenswarm.server.runtime.session.project_access import ProjectAccessStore
from jiuwenswarm.server.runtime.session.session_manager import SessionManager
from jiuwenswarm.server.runtime.session.sharing_host import SharingHostService
from tests.unit_tests.governance import test_organization_auth as auth_fixtures
from tests.unit_tests.governance.test_organization_auth import principal
from tests.unit_tests.runtime.harness import test_native_request_origin as native_fixtures

credentials = auth_fixtures.credentials
native_case = native_fixtures.native_case


@pytest.fixture
async def full_case(native_case, credentials, tmp_path, monkeypatch):
    c = native_case
    auth, tokens, authpath = credentials
    alice, bob = principal(auth, tokens, 'alice'), principal(auth, tokens, 'bob')
    from jiuwenswarm.common import utils
    monkeypatch.setattr(project_store, 'get_agent_root_dir', lambda: tmp_path / 'registry')
    monkeypatch.setattr(utils, 'get_agent_sessions_dir', lambda: tmp_path / 'sessions')
    monkeypatch.setattr(session_metadata, 'get_agent_sessions_dir', lambda: tmp_path / 'sessions')
    project_store.invalidate_cache()
    project = project_store.create_project('Native Origin', str(tmp_path))
    access = ProjectAccessStore()
    access.initialize(project.project_id, 'bob')
    access.register_resource(project.project_id, ResourceDefinition('workspace', 'workspace', str(tmp_path)),
                             owner_subject_id='bob', actions=('read',), expected_revision=0)
    access.register_resource(project.project_id, ResourceDefinition('model', 'credential', 'model-account:bob'),
                             owner_subject_id='bob', actions=('use',), expected_revision=1)
    host = SharingHostService(auth.resolve_actor, known_actor=auth.known_actor, storage=access)
    sid = c.session.get_session_id()
    host.register_owner_and_source(sid, bob.identity(), project.project_id)
    session_metadata.update_session_metadata(session_id=sid, channel_id='web', project_id=project.project_id,
        project_dir=str(tmp_path), mode='agent', work_mode='work', sync_write=True, cache_bust=True)
    assert host.owner_current(sid, bob.identity())
    assert not host.owner_current(sid, alice.identity())

    # Real product ownership shape; avoid facade initialization's unrelated
    # skill/config side effects, while exercising all actual ownership methods.
    child = object.__new__(JiuWenSwarmDeepAdapter)
    child._is_session_scoped_adapter = True
    child._parent_session_id = sid
    child._native_execution = c.native
    child._instance = c.agent
    root = object.__new__(JiuWenSwarmDeepAdapter)
    root._is_session_scoped_adapter = False
    root._session_adapters = {sid: child}
    root._session_adapter_locks = {}
    root._active_session_ids = {}
    root._session_agent_tasks = {}
    facade = object.__new__(JiuWenSwarm)
    facade._adapter = root
    facade._session_manager = SessionManager()
    manager = AgentManager()
    manager.agents['web'] = {'agent': facade}
    assert manager.get_agent_for_session_nowait('web', sid) is facade
    assert root._get_cached_session_adapter(sid) is child
    state = SimpleNamespace(runtime=None)
    try:
        yield SimpleNamespace(**locals())
    finally:
        if state.runtime is not None:
            await state.runtime._session_coordinator.close()
        assert session_metadata.flush_pending_writes()
        project_store.invalidate_cache()


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['organization', 'sdk-trusted-resolver'])
async def test_actual_runtime_root_child_native_source_completes(full_case, monkeypatch, mode):
    f = full_case
    if mode == 'sdk-trusted-resolver':
        monkeypatch.delenv(CONFIG_ENV)
        def resolver(_):
            return f.bob.identity()
        scope = nullcontext()
    else:
        def resolver(_):
            return current_identity()
        scope = authenticated_scope(f.bob)
    runtime = AgentRuntime(agent_manager=f.manager, initializer=AsyncMock(),
        trusted_identity_resolver=resolver, project_authorizer=f.access,
        resource_authorizer=f.access, organization_session_host=f.host)
    f.state.runtime = runtime
    coordinator = runtime._session_coordinator
    await coordinator.register_session(f.sid, 'web')
    request = AgentRequest('original-request', channel_id='web', session_id=f.sid,
                           req_method=ReqMethod.CHAT_SEND, params={'project_id': f.project.project_id})
    seen = []
    async def operation():
        resources = runtime._resource_authorizers_for(request)
        assert resources.native_lifecycle_factory is not None
        with tool_authority_scope(None, provider_authorizers=resources):
            receipt = await f.c.native.send_request(SendInputRequest(request.request_id, {'query': 'ordinary'}))
        owner, = coordinator._registry.select(session_id=f.sid, request_id=request.request_id)
        admission = owner._native_admission
        assert admission.owner is owner and admission.native is f.c.native
        assert admission.source.host_value is owner
        assert admission.owned_turn.request_id == request.request_id
        assert admission.owned_turn.turn_id == receipt.turn_id
        # A later ambient principal cannot replace the original credential/source.
        with authenticated_scope(f.alice):
            admission.check_current()
        await asyncio.wait_for(admission.confirmed.wait(), 3)
        seen.append(owner)
        return 'done'
    with scope:
        task = asyncio.create_task(coordinator.run_unary(f.sid, request.request_id, SessionWorkKind.CHAT_UNARY, operation))
        assert await asyncio.wait_for(task, 5) == 'done'
    await asyncio.sleep(0)
    owner, = seen
    assert owner._execution_authority is (f.bob if mode == 'organization' else None)
    assert owner._native_admission.confirmed.is_set()
    assert owner.state is SessionExecutionState.SUCCEEDED
    assert f.c.calls[0]['query'] == 'ordinary'
    assert not coordinator._sessions[f.sid].external_execution


@pytest.mark.asyncio
async def test_runtime_control_keeps_original_native_parent_credential(full_case, monkeypatch):
    """Original real lifecycle; interactive IO acceptance is a bounded fixture."""
    import secrets
    import time
    from openjiuwen.core.session.interaction.interactive_input import InteractiveInput
    f = full_case
    token2 = secrets.token_urlsafe(32)
    config = json.loads(f.authpath.read_text())
    config['credentials'].append({'actor_id': 'bob', 'sha256': hashlib.sha256(token2.encode()).hexdigest(),
                                  'expires_at': time.time() + 600, 'revoked': False})
    f.authpath.write_text(json.dumps(config))
    second = f.auth.principal({'Authorization': 'Bearer ' + token2})
    assert second.identity() == f.bob.identity() and second is not f.bob
    runtime = AgentRuntime(agent_manager=f.manager, initializer=AsyncMock(),
        trusted_identity_resolver=lambda _: current_identity(), project_authorizer=f.access,
        resource_authorizer=f.access, organization_session_host=f.host)
    f.state.runtime = runtime
    c = runtime._session_coordinator
    await c.register_session(f.sid, 'web')
    entered, release = asyncio.Event(), asyncio.Event()
    async def invoke(*_, **__):
        entered.set()
        await release.wait()
        return {'output': 'ordinary control completed'}
    f.c.react.invoke = invoke
    req = AgentRequest('control-parent', channel_id='web', session_id=f.sid, req_method=ReqMethod.CHAT_SEND,
                       params={'project_id': f.project.project_id})
    original = {}
    async def body():
        with tool_authority_scope(None, provider_authorizers=runtime._resource_authorizers_for(req)):
            receipt = await f.c.native.send_request(SendInputRequest(req.request_id, {'query': 'ordinary'}))
        owner, = c._registry.select(session_id=f.sid, request_id=req.request_id)
        original.update(owner=owner, receipt=receipt, admission=owner._native_admission)
        await owner._native_admission.confirmed.wait()
        return 'done'
    with authenticated_scope(f.bob):
        task = asyncio.create_task(c.run_unary(f.sid, req.request_id, SessionWorkKind.CHAT_UNARY, body))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        assert c.record_interaction(f.sid, req.request_id, 'question')
        monkeypatch.setattr(f.c.native.io, 'is_pending_interrupt_resume_valid', lambda _: True)
        monkeypatch.setattr(type(f.c.native.io), 'pending_interrupt_ids', property(lambda _: ('question',)))
        monkeypatch.setattr(f.c.native.io, 'send', AsyncMock(return_value=None))
        answer = InteractiveInput()
        answer.update('question', 'yes')
        async def deliver():
            parent = c.claimed_control_parent(f.sid, 'question')
            assert parent is original['owner']
            c.retain_native_control_origin(f.sid, 'question', f.c.native, original['receipt'].turn_id)
            assert await f.c.native.answer_request(SendInputRequest('question', {'query': answer}))
            return 'accepted'
        with authenticated_scope(second):
            assert await c.deliver_control(f.sid, 'question', deliver) == 'accepted'
        parent = original['owner']
        control, = c._registry.select(session_id=f.sid, request_id='question')
        assert parent._execution_authority is f.bob
        assert control._execution_authority is second
        assert parent._native_admission is original['admission']
        assert control._native_admission is None
        assert not parent.state.terminal
        release.set()
        assert await asyncio.wait_for(task, 3) == 'done'
        await asyncio.sleep(0)
        assert parent.state is SessionExecutionState.SUCCEEDED
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
