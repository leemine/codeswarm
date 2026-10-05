"""Actual owner store/Runtime/Session reads and both existing delivery checks."""
import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.governance.organization_auth import authenticated_scope, current_identity
from jiuwenswarm.governance.session_boundary import (
    admit_session_request, delivery_authorized, delivery_scope, set_delivery_permit,
)
from jiuwenswarm.governance.preparation import GovernanceError
from jiuwenswarm.runtime import AgentRuntime
from jiuwenswarm.server.runtime.agent_adapter.interface import JiuWenSwarm
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter
from jiuwenswarm.server.runtime.agent_manager import AgentManager
from jiuwenswarm.server.runtime.session import project_store, session_metadata
from jiuwenswarm.server.runtime.session.project_access import ProjectAccessStore
from jiuwenswarm.server.runtime.session.session_manager import SessionManager
from jiuwenswarm.server.runtime.session.sharing_host import SharingHostService
from tests.unit_tests.governance import test_organization_auth as auth_fixture
from tests.unit_tests.runtime import test_goal_read as reader_fixture

credentials = auth_fixture.credentials
source = reader_fixture.source


@pytest.fixture
async def case(source, credentials, tmp_path, monkeypatch):
    c = source
    auth, tokens, authpath = credentials
    bob = auth_fixture.principal(auth, tokens, 'bob')
    alice = auth_fixture.principal(auth, tokens, 'alice')
    from jiuwenswarm.common import utils, config
    monkeypatch.setattr(project_store, 'get_agent_root_dir', lambda: tmp_path / 'registry')
    monkeypatch.setattr(utils, 'get_agent_sessions_dir', lambda: tmp_path / 'sessions')
    monkeypatch.setattr(session_metadata, 'get_agent_sessions_dir', lambda: tmp_path / 'sessions')
    monkeypatch.setattr(config, 'get_config', lambda: {'execution': {
        'default_profile_id': 'profile', 'profiles': {'profile': {
            'provider_id': 'native', 'config_revision': 'goal-read'}}}})
    project_store.invalidate_cache()
    project = project_store.create_project('Read', str(tmp_path))
    access = ProjectAccessStore()
    access.initialize(project.project_id, 'alice')
    access.replace_acl(project.project_id, 'alice', acl={'bob': ['read']}, expected_revision=1)
    host = SharingHostService(auth.resolve_actor, known_actor=auth.known_actor, storage=access)
    sid = c.descriptor.session_id
    host.register_owner_and_source(sid, bob.identity(), project.project_id)
    session_metadata.init_session_metadata(session_id=sid, channel_id='web', project_id=project.project_id,
        project_dir=str(tmp_path), mode='agent', work_mode='work', execution_profile_id='profile',
        execution_config_revision=c.descriptor.config_revision,
        execution_config_fingerprint=c.descriptor.config_fingerprint)
    assert host.owner_current(sid, bob.identity())
    assert not access.authorize(project.project_id, 'bob', 'execute').allowed
    binding = replace(c.bound.binding, subject_id='bob')
    c.native.engine.binding = binding
    c.native._tool_owner = (c.instance, c.native._tool_owner[1], c.session, binding)
    child = object.__new__(JiuWenSwarmDeepAdapter)
    child.__dict__.update(vars(c.child))
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
    runtime = AgentRuntime(agent_manager=manager, trusted_identity_resolver=lambda _: current_identity(),
        project_authorizer=access, resource_authorizer=access, organization_session_host=host)
    runtime._started = True  # The product server has already initialized its shared Runtime.
    monkeypatch.setattr(manager, 'get_agent', Mock(side_effect=AssertionError('read allocated Agent')))
    request = AgentRequest('read', session_id=sid, channel_id='web', req_method=ReqMethod.COMMAND_GOAL,
                           params={'session_id': sid, 'action': 'get', 'mode': 'agent'})
    value = SimpleNamespace(**locals())
    try:
        yield value
    finally:
        await runtime._session_coordinator.close()
        assert session_metadata.flush_pending_writes()
        project_store.invalidate_cache()


@pytest.mark.parametrize('cold', [False, True])
@pytest.mark.parametrize('stream', [False, True])
async def test_owner_with_only_read_can_query_without_execution(case, cold, stream):
    f = case
    if cold:
        f.manager.agents.clear()
    f.request.is_stream = stream
    before = dict(f.c.store._store)
    with authenticated_scope(f.bob), delivery_scope():
        permit = admit_session_request('command.goal', f.request.params,
            identity_resolver=f.bob.identity, host=f.host)
        set_delivery_permit(permit)
        events = ([event async for event in f.runtime.stream(f.request)] if stream
                  else await f.runtime.invoke(f.request))
        assert delivery_authorized()
    assert len(events) == 1 and events[0].payload['goal']['goal_id'] == f.c.record.goal_id
    assert f.runtime._session_coordinator.snapshot_session(f.sid) is None
    assert f.c.store._store == before
    assert not f.manager._agent_borrowers


async def test_foreign_authenticated_owner_cannot_query(case):
    with authenticated_scope(case.alice):
        with pytest.raises(PermissionError):
            await case.runtime.invoke(case.request)


async def test_existing_ui_mode_alias_reads_original_single_without_rebinding(case):
    f = case
    session_metadata.update_session_metadata(session_id=f.sid, mode='agent.work.normal',
                                            sync_write=True, cache_bust=True)
    with authenticated_scope(f.bob):
        result, = await f.runtime.invoke(f.request)  # Existing UI sends mode='agent'.
    assert result.payload['goal']['goal_id'] == f.c.record.goal_id
    assert f.c.native.engine.binding is f.binding


@pytest.mark.parametrize('change', ['credential', 'acl', 'metadata', 'child'])
async def test_original_agentserver_delivery_rechecks_after_read(case, change):
    f = case
    original_auth = f.authpath.read_bytes()
    with authenticated_scope(f.bob), delivery_scope():
        permit = admit_session_request('command.goal', f.request.params,
            identity_resolver=f.bob.identity, host=f.host)
        set_delivery_permit(permit)
        await f.runtime.invoke(f.request)
        assert delivery_authorized()
        if change == 'credential':
            f.auth.revoke(f.bob)
        elif change == 'acl':
            f.access.replace_acl(f.project.project_id, 'alice', acl={}, expected_revision=2)
        elif change == 'metadata':
            meta = session_metadata.get_session_metadata(f.sid, cache_bust=True, enable_writeback=False)
            meta['execution_config_revision'] = 'replacement'
            session_metadata._write_metadata_sync(f.sid, meta)
        else:
            f.root._session_adapters[f.sid] = object()
        assert not delivery_authorized()
    f.authpath.write_bytes(original_auth)


@pytest.mark.parametrize('extra', [{'action': 'resume'}, {'action': 'clear'}, {'query': 'execute'},
    {'share_id': 'shared'}, {'attach_goal': True}, {'mode': 'team.normal'}])
async def test_read_boundary_rejects_execution_and_sharing_hints(case, extra):
    f = case
    params = dict(f.request.params, **extra)
    with authenticated_scope(f.bob), pytest.raises(PermissionError):
        admit_session_request('command.goal', params, identity_resolver=f.bob.identity, host=f.host)


async def test_cold_storage_wait_rechecks_original_credential(case, monkeypatch):
    f = case
    f.manager.agents.clear()
    entered, release = asyncio.Event(), asyncio.Event()
    original = f.c.store._get_without_lock
    async def get(key):
        entered.set()
        await release.wait()
        return await original(key)
    monkeypatch.setattr(f.c.store, '_get_without_lock', get)
    original_auth = f.authpath.read_bytes()
    with authenticated_scope(f.bob):
        task = asyncio.create_task(f.runtime.invoke(f.request))
        await asyncio.wait_for(entered.wait(), 2)
        f.auth.revoke(f.bob)
        release.set()
        with pytest.raises(GovernanceError, match='authorization changed'):
            await task
    f.authpath.write_bytes(original_auth)


@pytest.mark.parametrize('stream', [False, True])
async def test_unstarted_runtime_get_does_not_initialize_execution(case, monkeypatch, stream):
    f = case
    f.runtime._started = False
    monkeypatch.setattr(f.runtime, 'start', Mock(side_effect=AssertionError('query starts Runtime')))
    with authenticated_scope(f.bob), pytest.raises(RuntimeError, match='started'):
        if stream:
            [event async for event in f.runtime.stream(f.request)]
        else:
            await f.runtime.invoke(f.request)
