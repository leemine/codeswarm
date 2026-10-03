# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Real Provider CLIs through the Team member factory; loopback models only."""
import asyncio
from contextlib import AsyncExitStack
from copy import deepcopy
import json
import os
from pathlib import Path
import socket

import pytest
from aiohttp import web

from openjiuwen.agent_teams import TeamAgent, TeamAgentSpec
from openjiuwen.agent_teams.context import set_session_id, reset_session_id
from openjiuwen.agent_teams.schema.blueprint import DeepAgentSpec, StorageSpec
from openjiuwen.agent_teams.schema.team import TeamRole
from openjiuwen.core.session.agent_team import create_agent_team_session
from openjiuwen.core.session.checkpointer import CheckpointerFactory
from openjiuwen.core.session.checkpointer.checkpointer import InMemoryCheckpointer
from openjiuwen.core.single_agent import AgentCard
from openjiuwen.harness.engine import ExecutionBinding
from openjiuwen.harness_protocol import TurnStatus

from jiuwenswarm.common.runtime_workspace import RuntimeWorkspacePaths
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.server.runtime.agent_adapter.team_engine_adapter import ExternalTeamAgentAdapter
from jiuwenswarm.runtime.harness.binding_store import BoundExecution, ExecutionBindingStore
from jiuwenswarm.runtime.harness.config_source import load_execution_catalog
from jiuwenswarm.runtime.harness.request_binding import AdmittedExecutionRoute
from jiuwenswarm.runtime.harness.surface import EffectiveSurfaceSnapshot, build_surface_identity, creation_surface
from jiuwenswarm.runtime.harness.recovery_store import SessionExecutionRecovery
from tests.unit_tests.runtime.harness.test_execution_recovery import recovery_env as recovery_env
from jiuwenswarm.runtime.harness import team_execution as module
from tests.system_tests.test_external_codex_product_route_local import _ResponsesFixture
from tests.system_tests.test_external_opencode_product_route_local import _ModelFixture

pytestmark = [pytest.mark.integration, pytest.mark.system,
              pytest.mark.skipif(os.environ.get('RUN_TEAM_PROVIDER_LOCAL') != '1',
                                 reason='real Team Provider CLI construction is opt-in')]


@pytest.mark.asyncio
@pytest.mark.parametrize('provider', ['codex', 'opencode'])
@pytest.mark.parametrize('trusted_subject', [False, True], ids=['routing-subject', 'trusted-subject'])
async def test_real_team_member_provider_turn_and_resume(tmp_path, monkeypatch, recovery_env, provider, trusted_subject):
    root = tmp_path / 'project'
    home = tmp_path / 'home'
    runtime_root = tmp_path / 'provider-runtime'
    for path in (root, home, runtime_root):
        path.mkdir(mode=0o700)
    monkeypatch.setenv('OPENJIUWEN_HOME', str(home / 'openjiuwen'))
    previous = CheckpointerFactory.get_checkpointer()
    CheckpointerFactory.set_default_checkpointer(InMemoryCheckpointer())
    from openjiuwen.agent_teams.spawn.shared_resources import cleanup_shared_resources
    cleanup_shared_resources()
    token = set_session_id('team-local')
    members = []
    transports = []
    tool_results = []
    real_products = module.ExternalSubagentRuntime
    def products(*args, **kwargs):
        result = real_products(*args, **kwargs)
        invoke = result.gateway.invoke
        async def observed(invocation):
            output = await invoke(invocation)
            tool_results.append((kwargs['invoke_kwargs']['member_name'], invocation.name, output))
            return output
        result.gateway.invoke = observed
        return result
    monkeypatch.setattr(module, 'ExternalSubagentRuntime', products)
    real_transport = module.ManagedProductToolTransport
    def transport(*args, **kwargs):
        result = real_transport(*args, **kwargs)
        transports.append(result)
        return result
    monkeypatch.setattr(module, 'ManagedProductToolTransport', transport)
    async with AsyncExitStack() as stack:
        try:
            if provider == 'codex':
                pytest.importorskip('openai_codex')
                model = stack.enter_context(_ResponsesFixture())
                codex_home = tmp_path / 'codex-home'
                (codex_home / 'skills').mkdir(parents=True)
                settings = {
                    'inherit_process_env': False,
                    'env': {'HOME': str(home), 'CODEX_HOME': str(codex_home),
                            'PATH': os.environ.get('PATH', '/usr/bin:/bin')},
                    'startup_source_roots': [str(root), str(codex_home / 'skills')],
                    'mcp_required': True,
                    'model': {'model': 'gpt-5.6-sol', 'provider': 'team_fixture',
                              'api_base': model.base_url, 'api_key': 'local-only'},
                }
            else:
                cli = Path(os.environ.get('OPENCODE_OC1_CLI', str(Path.home() / '.opencode/bin/opencode')))
                if not cli.is_file():
                    pytest.skip('OpenCode CLI is not installed')
                model = _ModelFixture()
                app = web.Application()
                app.router.add_post('/v1/chat/completions', model.respond)
                runner = web.AppRunner(app)
                await runner.setup()
                stack.push_async_callback(runner.cleanup)
                sock = socket.socket()
                sock.bind(('127.0.0.1', 0))
                port = sock.getsockname()[1]
                await web.SockSite(runner, sock).start()
                settings = {'cli_path': str(cli), 'runtime_root': str(runtime_root), 'turn_timeout_s': 25,
                            'model': {'model': 'fixture', 'api_base': f'http://127.0.0.1:{port}/v1',
                                      'api_key': 'local-only'}}
            config = {'execution': {'default_profile_id': 'team-local', 'profiles': {'team-local': {
                'provider_id': provider, 'config_revision': 'team-local-1', 'provider_config': settings,
                'authorization': {'full_access': True},
            }}}}
            source = load_execution_catalog(config).source(explicit_profile_id='team-local')
            binding = ExecutionBinding.create(source.resolve(), subject_id='host-worker' if trusted_subject else 'fixture-owner',
                                              host_session_id='team-local', workspace=str(root))
            paths = RuntimeWorkspacePaths(home, root, root, root)
            metadata = {'session_id': 'team-local', 'user_id': 'fixture-owner', 'channel_id': 'local',
                        'team_name': 'team', 'mode': 'team.code.normal', 'work_mode': 'code',
                        'project_dir': str(root), 'execution_profile_id': 'team-local',
                        'execution_config_revision': binding.config_revision,
                        'execution_config_fingerprint': binding.fingerprint}
            monkeypatch.setattr(module, '_host_config', lambda: config)
            monkeypatch.setattr(module, '_session_metadata', lambda _: metadata)
            metadata['surface_creation'] = creation_surface(metadata)
            persisted = deepcopy(metadata)
            surface_metadata = deepcopy(metadata)
            if trusted_subject:
                surface_metadata['user_id'] = binding.subject_id
                surface_metadata['surface_creation']['user_id'] = binding.subject_id
            identity = build_surface_identity(metadata=surface_metadata, binding=binding, paths=paths, channel_id='local')
            recovery = SessionExecutionRecovery(
                session_id='team-local', execution_profile_id='team-local', binding=binding,
                runtime_paths=paths, surface_identity=identity,
            )
            route = AdmittedExecutionRoute('local', source, ExecutionBindingStore(), BoundExecution(binding, source.resolve()),
                                           paths, recovery=recovery, surface=EffectiveSurfaceSnapshot(identity, metadata['mode']),
                                           trusted_subject_id=binding.subject_id if trusted_subject else None)
            request = AgentRequest('team-turn', session_id='team-local', channel_id='local',
                                   user_id='wire-routing-user' if trusted_subject else 'fixture-owner',
                                   params={'mode': metadata['mode']})
            request._execution_route = route
            ExternalTeamAgentAdapter(route).select_execution_for_request(request)
            factory = module.ExternalTeamMemberFactory(route, team_name='team')
            reconstructed = module.ExternalTeamMemberFactory.from_seed(factory.to_seed(), config=config)
            assert reconstructed._host_selection_unchanged()
            assert metadata == persisted
            spec = TeamAgentSpec(agents={'leader': DeepAgentSpec(), 'teammate': DeepAgentSpec()},
                                 team_name='team', evolution_enabled=False, spawn_mode='inprocess',
                                 storage=StorageSpec(type='memory'))
            module.attach_external_team_execution(spec, route)
            leader = spec.build()
            members.append(leader)
            await leader.team_backend.db.initialize()
            worker_ctx = leader.blueprint.ctx.model_copy(update={'role': TeamRole.TEAMMATE, 'member_name': 'worker'})
            worker = TeamAgent(AgentCard(id='team-worker', name='worker')).configure(spec, worker_ctx)
            members.append(worker)
            session = create_agent_team_session(session_id='team-local', team_id='team')
            provider_sessions = []
            async with asyncio.timeout(75):
                for member in members:
                    if provider == 'codex':
                        model.items.append({
                            'type': 'function_call', 'namespace': 'mcp__jiuwenswarm_product_tools',
                            'name': 'view_task', 'id': f'fc_{len(model.requests)}',
                            'call_id': f'call_{len(model.requests)}',
                            'arguments': json.dumps({'action': 'list'}),
                        })
                    else:
                        model.actions.append({'tool': 'jiuwenswarm_product_tools_view_task',
                                              'args': {'action': 'list'}})
                    await member.harness.start(team_session=session)
                    provider_sessions.append(member.harness.harness.provider_session_id)
                    complete = asyncio.Event()
                    results = []
                    async def on_round(*, kind, round_id, result):
                        if kind in {'finished', 'failed', 'aborted'}:
                            results.append(result)
                            complete.set()
                    await member.harness.subscribe(on_round=on_round)
                    await member.harness.send('Return the fixture completion marker.')
                    await asyncio.wait_for(complete.wait(), timeout=30)
                    assert results[0].status is TurnStatus.COMPLETED, results[0]
                    assert await member.harness.harness.export_checkpoint() is not None
                assert len(set(provider_sessions)) == 2
                await leader.harness.stop()
                # Same member, same stored child Session and Provider checkpoint.
                leader.team_backend.mark_history_restored()
                await leader.harness.start(team_session=create_agent_team_session(session_id='team-local'))
                assert leader.harness.harness.provider_session_id == provider_sessions[0]
                assert len(model.requests) >= 4
                assert [(name, tool) for name, tool, _ in tool_results] == [
                    (member.blueprint.ctx.member_name, 'view_task') for member in members
                ]
                assert all(not output.is_error for _, _, output in tool_results), tool_results
                assert metadata == persisted
        finally:
            for member in reversed(members):
                await member.harness.stop()
            if members:
                await members[0].team_backend.db.close()
            CheckpointerFactory.set_default_checkpointer(previous)
            cleanup_shared_resources()
            reset_session_id(token)
    assert all(transport.exit_confirmed and not transport.started for transport in transports)
