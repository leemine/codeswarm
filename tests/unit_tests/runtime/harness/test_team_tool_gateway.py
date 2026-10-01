# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Original Team operations, role isolation and host admission through MCP ports."""

from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from openjiuwen.agent_teams import TeamAgentSpec, TeamMemberRuntimeBuild
from openjiuwen.agent_teams.context import reset_session_id, set_session_id
from openjiuwen.agent_teams.schema.blueprint import DeepAgentSpec
from openjiuwen.agent_teams.schema.team import TeamRole, TeamRuntimeContext
from openjiuwen.agent_teams.schema.task import TaskGraphSpec
from openjiuwen.agent_teams.tools.database import DatabaseConfig, TeamDatabase
from openjiuwen.agent_teams.tools.team import TeamBackend
from openjiuwen.core.single_agent import AgentCard
from openjiuwen.harness_protocol import ToolInvocation

from jiuwenswarm.runtime.harness.team_tools import build_team_tool_gateway
from jiuwenswarm.runtime.harness.tool_gateway import ProductToolScope


@pytest_asyncio.fixture
async def team(tmp_path):
    token = set_session_id('gateway-team-session')
    db = TeamDatabase(DatabaseConfig(connection_string=':memory:'))
    await db.initialize()
    try:
        await db.team.create_team(team_name='team', display_name='Team', leader_member_name='leader')
        requests = {}
        for name in ('leader', 'worker', 'reviewer'):
            card = AgentCard(id=f'team-{name}', name=name)
            await db.member.create_member(
                member_name=name, team_name='team', display_name=name,
                agent_card=card.model_dump_json(), status='READY', mode='build_mode',
            )
            role = TeamRole.LEADER if name == 'leader' else TeamRole.TEAMMATE
            bus = AsyncMock()
            backend = TeamBackend(
                team_name='team', member_name=name, is_leader=role is TeamRole.LEADER,
                db=db, messager=bus, evolution_enabled=False,
            )
            requests[name] = TeamMemberRuntimeBuild(
                spec=TeamAgentSpec(agents={'leader': DeepAgentSpec()}, team_name='team',
                                   execution_provider='codex', evolution_enabled=False),
                context=TeamRuntimeContext(role=role, member_name=name), card=card,
                language='en', team_mode='default', team_backend=backend,
                workspace_manager=None, model_allocator=None, messager=bus,
            )

        def gateway(name, admit=lambda scope, call: True):
            return build_team_tool_gateway(
                requests[name], scope=ProductToolScope('alice', f'session-{name}', str(tmp_path)),
                admit=admit,
            )

        yield requests, gateway
    finally:
        await db.close()
        reset_session_id(token)


@pytest.mark.asyncio
async def test_catalog_keeps_original_role_and_dispatch_permissions(team):
    requests, build = team
    leader, worker = build('leader'), build('worker')
    assert {'build_team', 'spawn_teammate', 'create_task', 'update_task'} <= set(leader.tool_names)
    assert {'claim_task', 'verify_task', 'send_message'} <= set(worker.tool_names)
    assert 'create_task' not in worker.tool_names
    assert 'verify_task' not in leader.tool_names
    assert not {'checkpoint', 'swarmflow', 'async_tasks_list'} & set(leader.tool_names)
    requests['worker'].spec.dispatch_mode = 'scheduled'
    scheduled = build('worker')
    assert 'member_complete_task' in scheduled.tool_names
    assert 'claim_task' not in scheduled.tool_names


@pytest.mark.asyncio
async def test_member_cannot_acquire_leader_tools_via_forged_arguments(team):
    requests, build = team
    result = await build('worker').invoke(ToolInvocation(
        'forged', 'create_task', {'member_name': 'leader', 'tasks': [{'title': 'bad', 'content': 'bad'}]},
    ))
    assert result.is_error
    assert 'Unknown' in result.content


@pytest.mark.asyncio
async def test_original_task_claim_and_completion_remain_member_owned(team):
    requests, build = team
    leader, worker, reviewer = build('leader'), build('worker'), build('reviewer')
    created = await leader.invoke(ToolInvocation('create', 'create_task', {
        'tasks': [{'task_id': 'work', 'title': 'Work', 'content': 'Implement'}],
    }))
    assert not created.is_error, created.content
    claim = await worker.invoke(ToolInvocation('claim', 'claim_task', {'task_id': 'work', 'status': 'claimed'}))
    assert not claim.is_error, claim.content
    forged = await reviewer.invoke(ToolInvocation('forge', 'claim_task', {
        'task_id': 'work', 'status': 'completed', 'member_name': 'worker',
    }))
    assert forged.is_error
    task = await requests['leader'].team_backend.task_manager.get('work')
    assert task.assignee == 'worker'
    assert task.status == 'in_progress'
    completed = await worker.invoke(ToolInvocation('done', 'claim_task', {'task_id': 'work', 'status': 'completed'}))
    assert not completed.is_error, completed.content
    task = await requests['leader'].team_backend.task_manager.get('work')
    assert task.status == 'completed'


@pytest.mark.asyncio
async def test_runtime_admission_is_checked_on_each_call(team):
    requests, build = team
    allowed = True
    observed = []

    def admit(scope, invocation):
        observed.append((scope.host_session_id, invocation.name))
        return allowed

    gateway = build('leader', admit)
    call = ToolInvocation('read', 'view_task', {'action': 'list'})
    assert not (await gateway.invoke(call)).is_error
    allowed = False
    assert (await gateway.invoke(call)).is_error
    assert observed == [('session-leader', 'view_task')] * 2


@pytest.mark.asyncio
async def test_review_and_message_identity_remain_owned_by_original_backend(team):
    requests, build = team
    manager = requests['leader'].team_backend.task_manager
    seeded = await manager.add_graph([TaskGraphSpec(
        task_id='reviewed', title='Work', content='Review this', assignee='worker', reviewer=('reviewer',),
    )])
    assert seeded.ok
    worker, reviewer = build('worker'), build('reviewer')
    assert not (await worker.invoke(ToolInvocation('claim', 'claim_task', {
        'task_id': 'reviewed', 'status': 'claimed',
    }))).is_error
    assert not (await worker.invoke(ToolInvocation('done', 'claim_task', {
        'task_id': 'reviewed', 'status': 'completed',
    }))).is_error
    assert (await manager.get('reviewed')).status == 'in_review'
    forged = await worker.invoke(ToolInvocation('self-review', 'verify_task', {
        'task_id': 'reviewed', 'decision': 'pass', 'member_name': 'reviewer',
    }))
    assert forged.is_error
    assert not (await reviewer.invoke(ToolInvocation('review', 'verify_task', {
        'task_id': 'reviewed', 'decision': 'pass',
    }))).is_error
    assert (await manager.get('reviewed')).status == 'completed'
    sent = await worker.invoke(ToolInvocation('message', 'send_message', {
        'to': 'reviewer', 'content': 'Review complete', 'from_member_name': 'leader',
    }))
    assert not sent.is_error, sent.content
    messages = await requests['reviewer'].team_backend.message_manager.get_messages('reviewer')
    assert any(message.content == 'Review complete' and message.from_member_name == 'worker' for message in messages)


@pytest.mark.asyncio
async def test_real_authenticated_mcp_transport_keeps_member_role(team):
    import asyncio
    import httpx
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    from jiuwenswarm.runtime.harness.tool_transport import ManagedProductToolTransport

    _, build = team
    gateway = build('worker')
    transport = ManagedProductToolTransport(gateway, host_session_id=gateway.scope.host_session_id)
    try:
        async with asyncio.timeout(15):
            await transport.start()
            config = transport.server_config()
            async with httpx.AsyncClient(headers=dict(config.headers)) as client:
                async with streamable_http_client(config.url, http_client=client) as (read, write, _):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        catalog = await session.list_tools()
                        assert 'create_task' not in {tool.name for tool in catalog.tools}
                        result = await session.call_tool('view_task', {'action': 'list'})
                        assert not result.isError
    finally:
        await transport.stop()
    assert transport.exit_confirmed
    assert not transport.started


@pytest.mark.asyncio
async def test_backend_role_mismatch_and_missing_admission_rejected(team, tmp_path):
    requests, _ = team
    scope = ProductToolScope('alice', 'session-worker', str(tmp_path))
    forged = replace(requests['worker'], team_backend=requests['leader'].team_backend)
    with pytest.raises(ValueError, match='member identity'):
        build_team_tool_gateway(forged, scope=scope, admit=lambda *_: True)
    with pytest.raises(ValueError, match='runtime admission'):
        build_team_tool_gateway(requests['worker'], scope=scope, admit=None)
