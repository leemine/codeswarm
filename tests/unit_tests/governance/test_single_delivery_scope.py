"""Original organization admission and Runtime scope, without Provider probes."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from openjiuwen.harness.goal.schema import GoalRecord
from openjiuwen.harness.schema.interaction import SendInputRequest
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.governance.organization_auth import authenticated_scope, current_identity
from jiuwenswarm.governance.preparation import GovernanceError
from jiuwenswarm.governance.session_boundary import admit_session_request
from jiuwenswarm.governance.single_delivery import require_single_delivery
from jiuwenswarm.governance.tool_context import tool_authority_scope
from jiuwenswarm.runtime import AgentRuntime
from jiuwenswarm.runtime.session import SessionWorkKind
from jiuwenswarm.runtime.session_provisioner import SessionCreateInput
from jiuwenswarm.server.runtime.session import project_store, session_metadata
from jiuwenswarm.server.runtime.session.project_access import ProjectAccessStore
from jiuwenswarm.server.runtime.session.sharing_host import SharingHostService
from tests.unit_tests.governance import test_organization_auth as auth_fixture
from tests.unit_tests.runtime import test_native_runtime_source as native_fixture
from tests.unit_tests.runtime.harness import test_native_goal_source as goal_fixture

credentials = auth_fixture.credentials
goal_case = goal_fixture.goal_case
full_case = native_fixture.full_case


@pytest.fixture
async def native_case(goal_case):
    goal_case.agent = goal_case.outer
    return goal_case


@pytest.fixture
async def scope_case(credentials, tmp_path, monkeypatch):
    auth, tokens, _ = credentials
    principal = auth_fixture.principal(auth, tokens, 'bob')
    from jiuwenswarm.common import utils
    monkeypatch.setattr(project_store, 'get_agent_root_dir', lambda: tmp_path / 'registry')
    monkeypatch.setattr(utils, 'get_agent_sessions_dir', lambda: tmp_path / 'sessions')
    monkeypatch.setattr(session_metadata, 'get_agent_sessions_dir', lambda: tmp_path / 'sessions')
    project_store.invalidate_cache()
    project = project_store.create_project('Single', str(tmp_path))
    access = ProjectAccessStore()
    access.initialize(project.project_id, 'bob')
    host = SharingHostService(auth.resolve_actor, known_actor=auth.known_actor, storage=access)
    sid = 'single-delivery-session'
    host.register_owner_and_source(sid, principal.identity(), project.project_id)
    session_metadata.init_session_metadata(session_id=sid, channel_id='web',
        project_id=project.project_id, project_dir=str(tmp_path), mode='agent.work.normal', work_mode='work')
    runtime = AgentRuntime(initializer=AsyncMock(), trusted_identity_resolver=lambda _: current_identity(),
        project_authorizer=access, resource_authorizer=access, organization_session_host=host)
    runtime._started = True  # Shared server startup is outside this admission test.
    try:
        yield SimpleNamespace(**locals())
    finally:
        await runtime._session_coordinator.close()
        assert session_metadata.flush_pending_writes()
        project_store.invalidate_cache()


@pytest.mark.parametrize('method,params', [
    ('command.goal', {'action': action}) for action in ('set', 'pause', 'clear', 'resume')
] + [('chat.send', {'attach_goal': True}), ('chat.resume', {'attach_goal': True}),
     ('session.create', {'mode': 'team.work.normal'}), ('chat.send', {'mode': 'code.team'})])
async def test_real_admit_refuses_unreleased_routes(scope_case, method, params):
    c = scope_case
    params = dict(params)
    if method != 'session.create':
        params['session_id'] = c.sid
    with authenticated_scope(c.principal), pytest.raises(PermissionError, match='not released'):
        admit_session_request(method, params, identity_resolver=current_identity, host=c.host)


@pytest.mark.parametrize('stored', ['team.work.normal', 'team.code.normal', 'team', 'team.plan.normal'])
async def test_persisted_team_cannot_be_hidden_by_wire_single(scope_case, stored):
    c = scope_case
    session_metadata.update_session_metadata(session_id=c.sid, mode=stored, sync_write=True, cache_bust=True)
    with authenticated_scope(c.principal), pytest.raises(PermissionError, match='Team'):
        admit_session_request('chat.send', {'session_id': c.sid, 'mode': 'agent', 'query': 'ordinary'},
            identity_resolver=current_identity, host=c.host)


@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.parametrize('method,params,channel', [
    (ReqMethod.COMMAND_GOAL, {'action': 'set', 'objective': 'not released'}, 'web'),
    (ReqMethod.CHAT_SEND, {'attach_goal': True}, 'web'),
    (ReqMethod.CHAT_SEND, {'query': '/goal resume'}, 'tui'),
    (ReqMethod.CHAT_SEND, {'mode': 'team'}, 'web')])
async def test_actual_runtime_rejects_before_execution(scope_case, stream, method, params, channel):
    c = scope_case
    request = AgentRequest('scope-reject', session_id=c.sid, channel_id=channel,
        req_method=method, is_stream=stream, params={'project_id': c.project.project_id, **params})
    with authenticated_scope(c.principal), pytest.raises(PermissionError, match='not released'):
        if stream:
            _ = [event async for event in c.runtime.stream(request)]
        else:
            await c.runtime.invoke(request)
    assert c.runtime._session_coordinator.snapshot_session(c.sid) is None
    assert not c.runtime._session_coordinator._registry.select(session_id=c.sid)


async def test_actual_runtime_create_rejects_before_provision(scope_case):
    c = scope_case
    prepare = AsyncMock(side_effect=AssertionError('unreleased Session allocated'))
    c.runtime._session_provisioner.prepare_session_create = prepare
    with authenticated_scope(c.principal), pytest.raises(PermissionError, match='Team'):
        await c.runtime.prepare_session_create(SessionCreateInput(channel_id='web',
            project_id=c.project.project_id, mode='team', create_token='single-scope'))
    prepare.assert_not_awaited()


@pytest.mark.parametrize('method', ['chat.send', 'chat.resume', ReqMethod.CHAT_ANSWER.value])
async def test_ordinary_chat_and_approval_keep_owner_permit(scope_case, method):
    c = scope_case
    params = {'session_id': c.sid, 'mode': 'agent', 'query': '/goal set plain Web text'}
    with authenticated_scope(c.principal):
        permit = admit_session_request(method, params, identity_resolver=current_identity, host=c.host)
        assert permit.revalidate()
        request = AgentRequest('ordinary', session_id=c.sid, channel_id='web',
            req_method=ReqMethod(method), params=params)
        assert c.runtime._governance_owned_request(request).session_id == c.sid


@pytest.mark.parametrize('query', ['/goal set task', '/goal pause', '/goal clear', '/goal resume'])
def test_original_tui_parser_refuses_mutation_but_web_text_remains_text(query):
    with pytest.raises(PermissionError, match='Goal'):
        require_single_delivery('chat.send', {'query': query}, channel_id='tui')
    require_single_delivery('chat.send', {'query': query}, channel_id='web')


def test_nonorganization_runtime_retains_goal_team_and_attach(monkeypatch):
    monkeypatch.delenv('JIUWENSWARM_ORGANIZATION_AUTH_FILE', raising=False)
    runtime = AgentRuntime()
    assert runtime._organization_session_host is None
    for method, params in [(ReqMethod.COMMAND_GOAL, {'action': 'set'}),
                           (ReqMethod.CHAT_SEND, {'attach_goal': True, 'mode': 'team'})]:
        request = AgentRequest('legacy', channel_id='web', req_method=method, params=params)
        assert runtime._governance_owned_request(request) is request


async def test_actual_native_factory_denies_active_goal_before_turn_or_tool(full_case):
    f = full_case
    runtime = AgentRuntime(agent_manager=f.manager, initializer=AsyncMock(),
        trusted_identity_resolver=lambda _: current_identity(), project_authorizer=f.access,
        resource_authorizer=f.access, organization_session_host=f.host)
    f.state.runtime = runtime
    coordinator = runtime._session_coordinator
    await coordinator.register_session(f.sid, 'web')
    request = AgentRequest('ordinary-active-goal', session_id=f.sid, channel_id='web',
        req_method=ReqMethod.CHAT_SEND, params={'project_id': f.project.project_id})
    record = GoalRecord.create(session_id=f.sid, objective='must not resume', max_attempts=2)
    f.c.outer.goal_manager._store.save(record)
    before = f.c.outer.goal_manager.peek().to_dict()

    async def operation():
        resources = runtime._resource_authorizers_for(request)
        with tool_authority_scope(None, provider_authorizers=resources):
            with pytest.raises(GovernanceError, match='active Goal'):
                await f.c.native.send_request(SendInputRequest(request.request_id, {'query': 'ordinary'}))

    with authenticated_scope(f.bob):
        await asyncio.wait_for(coordinator.run_unary(f.sid, request.request_id,
            SessionWorkKind.CHAT_UNARY, operation), 5)
    assert f.c.native._native.active_turn is None
    assert f.c.outer.goal_manager.peek().to_dict() == before
    assert f.c.side_effects == [] and f.c.http == []
    assert not f.c.native._requests
