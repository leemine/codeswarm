# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Admitted Team host construction, reconstruction and owned-resource lifecycle."""
import asyncio
from dataclasses import replace
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from openjiuwen.agent_teams import TEAM_MEMBER_RUNTIME_FACTORY, TeamAgent, TeamAgentSpec
from openjiuwen.agent_teams.schema.blueprint import DeepAgentSpec, StorageSpec
from openjiuwen.agent_teams.schema.team import TeamRole
from openjiuwen.core.session.agent_team import create_agent_team_session
from openjiuwen.core.session.checkpointer import CheckpointerFactory
from openjiuwen.core.session.checkpointer.checkpointer import InMemoryCheckpointer
from openjiuwen.harness.engine import ExecutionBinding, HarnessEngine
from openjiuwen.harness_protocol import (
    HarnessCard, HarnessCheckpoint, HarnessStateError, HostCapability, TurnEventKind, TurnResult, TurnStatus,
    CheckpointReason, ResumePolicy,
)
from openjiuwen.harness_providers.base import SerializedTurnHarness, TurnTiming

from jiuwenswarm.common.runtime_workspace import RuntimeWorkspacePaths
from jiuwenswarm.runtime.harness.binding_store import BoundExecution, ExecutionBindingStore
from jiuwenswarm.runtime.harness.config_source import load_execution_catalog
from jiuwenswarm.runtime.harness.request_binding import AdmittedExecutionRoute
from jiuwenswarm.runtime.harness.surface import EffectiveSurfaceSnapshot, build_surface_identity
from jiuwenswarm.runtime.harness import team_execution as module


class ScriptedHarness(SerializedTurnHarness):
    def __init__(self, provider):
        super().__init__()
        self.card = HarnessCard(name=provider, implementation_version='test')
        self.contexts = []
        self.readers = 0
        self.fail_start = False
        self.fail_stop = False

    async def _open_session(self, context):
        self.contexts.append(context)
        if self.fail_start:
            raise RuntimeError('test Provider start failed')
        return 'provider-' + context.agent_id

    async def _close_session(self):
        if self.fail_stop:
            raise RuntimeError('test Provider exit failed')

    async def _execute_turn(self, turn):
        timing = TurnTiming()
        return TurnEventKind.FINISHED, TurnResult(
            status=TurnStatus.COMPLETED, final_output='done', started_at=timing.started_at,
            completed_at=timing.completed_at(), duration_ms=timing.duration_ms(),
        )

    def events(self):
        self.readers += 1
        return super().events()


@pytest.fixture(params=['codex', 'opencode'])
async def host(tmp_path, monkeypatch, request):
    monkeypatch.setenv('OPENJIUWEN_HOME', str(tmp_path / 'home'))
    monkeypatch.setenv('JIUWENSWARM_DATA_DIR', str(tmp_path / 'data'))
    from jiuwenswarm.common import utils
    from jiuwenswarm.server.runtime.session import session_metadata, session_history
    # Path resolution is cached before this parametrized fixture runs.
    monkeypatch.setattr(utils, '_workspace_base_dir', tmp_path / 'data')
    config = {'execution': {'default_profile_id': 'selected', 'profiles': {'selected': {
        'provider_id': request.param, 'config_revision': 'r1', 'authorization': {'full_access': False},
    }}}}
    source = load_execution_catalog(config).source(explicit_profile_id='selected')
    root = tmp_path / 'project'
    root.mkdir()
    paths = RuntimeWorkspacePaths(tmp_path, root, root, root)
    binding = ExecutionBinding.create(source.resolve(), subject_id='alice', host_session_id='session',
                                      workspace=str(root))
    metadata = {
        'session_id': 'session', 'user_id': 'alice', 'channel_id': 'web', 'team_name': 'team',
        'mode': 'team.code.normal', 'work_mode': 'code', 'project_dir': str(root),
        'execution_profile_id': 'selected', 'execution_config_revision': binding.config_revision,
        'execution_config_fingerprint': binding.fingerprint,
    }
    identity = build_surface_identity(metadata=metadata, binding=binding, paths=paths, channel_id='web')
    route = AdmittedExecutionRoute('web', source, ExecutionBindingStore(), BoundExecution(binding, source.resolve()),
                                   paths, surface=EffectiveSurfaceSnapshot(identity, metadata['mode']))
    monkeypatch.setattr(module, '_host_config', lambda: config)
    monkeypatch.setattr(module, '_session_metadata', lambda _: metadata)
    engines = []
    def engine(spec, *, binding):
        value = HarnessEngine(binding, ScriptedHarness(spec.provider_id))
        engines.append(value)
        return value
    monkeypatch.setattr(module, 'create_harness_engine', engine)
    from openjiuwen.agent_teams.harness.team_harness import TeamHarness
    monkeypatch.setattr(TeamHarness, 'build', Mock(side_effect=AssertionError('Native fallback')))
    from openjiuwen.agent_teams.spawn.shared_resources import cleanup_shared_resources
    cleanup_shared_resources()
    previous = CheckpointerFactory.get_checkpointer()
    CheckpointerFactory.set_default_checkpointer(InMemoryCheckpointer())
    yield SimpleNamespace(config=config, route=route, metadata=metadata, engines=engines, provider=request.param)
    # Runner also opens its default lookup database, outside captured member
    # backends. Close every test-owned shared pool before dropping the registry.
    from openjiuwen.agent_teams.spawn import shared_resources
    for db in tuple(shared_resources._db_instances.values()):
        await db.close()
    CheckpointerFactory.set_default_checkpointer(previous)
    cleanup_shared_resources()
    assert session_history.flush_pending_writes()
    assert session_metadata.flush_pending_writes()


def spec_for(host):
    spec = TeamAgentSpec(agents={'leader': DeepAgentSpec(), 'teammate': DeepAgentSpec()},
                         team_name='team', evolution_enabled=False, spawn_mode='inprocess',
                         storage=StorageSpec(type='memory'))
    return module.attach_external_team_execution(spec, host.route)


@pytest.mark.asyncio
async def test_leader_and_member_use_independent_bindings_and_one_reader_each(host):
    spec = spec_for(host)
    leader = spec.build()
    from openjiuwen.agent_teams.schema.build_context import register_build_context_factory
    import openjiuwen.harness.schema.build_context as carriers
    previous = carriers._BUILD_CONTEXT_FACTORY
    from jiuwenswarm.agents.swarm.context import SwarmBuildContext
    register_build_context_factory(lambda seed: SwarmBuildContext.from_seed(seed, config=host.config,
                                                                           trajectory_span_processor=None))
    try:
        ctx = leader.blueprint.ctx.model_copy(update={'role': TeamRole.TEAMMATE, 'member_name': 'reviewer'})
        member = await TeamAgent.from_spawn_payload({'spec': spec.model_dump(mode='json'),
                                                     'context': ctx.model_dump(mode='json')})
    finally:
        carriers._BUILD_CONTEXT_FACTORY = previous
    session = create_agent_team_session(session_id='session', team_id='team')
    try:
        await leader.harness.start(team_session=session)
        await member.harness.start(team_session=session)
        assert host.engines[0].binding != host.engines[1].binding
        assert host.engines[0].binding.workspace == host.engines[1].binding.workspace == host.route.bound.binding.workspace
        contexts = [engine.harness.contexts[0] for engine in host.engines]
        assert {ctx.agent_name for ctx in contexts} == {'team_leader', 'reviewer'}
        assert contexts[0].host_session_id != contexts[1].host_session_id
        assert contexts[0].checkpoint_sink is not contexts[1].checkpoint_sink
        assert contexts[0].mcp_servers[0].url != contexts[1].mcp_servers[0].url
        assert all(HostCapability.TOOL_APPROVAL in ctx.host_capabilities for ctx in contexts)
        assert all(engine.harness.readers == 1 for engine in host.engines)
        assert all('Team session' in ctx.system_prompt for ctx in contexts)
    finally:
        await member.harness.stop()
        await leader.harness.stop()


def test_seed_contains_no_provider_config_and_rehydrates_same_selection(host):
    spec = spec_for(host)
    seed = spec.build_context.to_seed()
    assert 'provider_config' not in json.dumps(seed)
    from jiuwenswarm.agents.swarm.context import SwarmBuildContext
    restored = SwarmBuildContext.from_seed(seed, config=host.config, trajectory_span_processor=None)
    factory = restored.extras[TEAM_MEMBER_RUNTIME_FACTORY]
    assert factory.to_seed() == spec.build_context.extras[TEAM_MEMBER_RUNTIME_FACTORY].to_seed()
    host.config['execution']['profiles']['other'] = {'provider_id': 'opencode', 'config_revision': 'other'}
    host.config['execution']['default_profile_id'] = 'other'
    restored = SwarmBuildContext.from_seed(seed, config=host.config, trajectory_span_processor=None)
    assert restored.extras[TEAM_MEMBER_RUNTIME_FACTORY].provider_id == host.provider


@pytest.mark.parametrize('change', ['config', 'subject', 'project', 'mode', 'carrier'])
def test_seed_rejects_configuration_and_authoritative_identity_drift(host, change):
    spec = spec_for(host)
    seed = spec.build_context.to_seed()
    if change == 'config':
        host.config['execution']['profiles']['selected']['config_revision'] = 'changed'
    elif change == 'subject':
        host.metadata['user_id'] = 'mallory'
    elif change == 'project':
        host.metadata['project_dir'] += '/other'
    elif change == 'mode':
        host.metadata['mode'] = 'team.code.plan'
    else:
        seed['team_id'] = 'other-team'
    from jiuwenswarm.agents.swarm.context import SwarmBuildContext
    with pytest.raises(ValueError):
        SwarmBuildContext.from_seed(seed, config=host.config, trajectory_span_processor=None)


@pytest.mark.asyncio
async def test_start_checks_session_and_current_host_authorization_before_resources(host, monkeypatch):
    spec = spec_for(host)
    leader = spec.build()
    transport = Mock(side_effect=AssertionError('allocated before admission'))
    monkeypatch.setattr(module, 'ManagedProductToolTransport', transport)
    with pytest.raises(ValueError, match='another Team Session'):
        await leader.harness.start(team_session=create_agent_team_session(session_id='other'))
    host.config['execution']['profiles']['selected']['authorization']['full_access'] = True
    with pytest.raises(ValueError, match='changed before start'):
        await leader.harness.start(team_session=create_agent_team_session(session_id='session'))
    transport.assert_not_called()
    assert host.engines[0].harness.contexts == []


@pytest.mark.asyncio
async def test_failed_provider_start_closes_transport_and_retry_allocates_fresh_transport(host, monkeypatch):
    leader = spec_for(host).build()
    provider = host.engines[0].harness
    transports = []
    real = module.ManagedProductToolTransport
    def transport(*args, **kwargs):
        value = real(*args, **kwargs)
        transports.append(value)
        return value
    monkeypatch.setattr(module, 'ManagedProductToolTransport', transport)
    provider.fail_start = True
    session = create_agent_team_session(session_id='session')
    with pytest.raises(RuntimeError, match='start failed'):
        await leader.harness.start(team_session=session)
    assert transports[0].exit_confirmed and not transports[0].started
    provider.fail_start = False
    try:
        await leader.harness.start(team_session=session)
        assert len(transports) == 2 and transports[1].started
    finally:
        await leader.harness.stop()
    assert all(transport.exit_confirmed and not transport.started for transport in transports)


@pytest.mark.asyncio
async def test_team_manager_attaches_factory_without_native_enrichment(host, monkeypatch):
    from jiuwenswarm.agents.harness.team.team_manager import TeamManager
    import jiuwenswarm.agents.harness.team.team_manager as manager_module
    manager = TeamManager()
    spec = TeamAgentSpec(agents={'leader': DeepAgentSpec()}, team_name='team', evolution_enabled=False, spawn_mode='inprocess')
    monkeypatch.setattr(manager_module, 'get_config', lambda: host.config)
    monkeypatch.setattr(manager, '_load_session_team_spec', lambda *a, **k: (spec, True))
    postgres = AsyncMock()
    monkeypatch.setattr(manager, '_ensure_postgresql_for_leader', postgres)
    enrich = Mock(side_effect=AssertionError('Native enrichment'))
    monkeypatch.setattr('jiuwenswarm.agents.swarm.enrich_team_spec_for_swarm', enrich)
    result = await manager.get_swarm_enriched_team_spec('session', mode='team.code.normal',
                                                      user_id='alice', channel_id='web', execution_route=host.route)
    assert result.execution_provider == host.provider
    assert isinstance(result.build_context.extras[TEAM_MEMBER_RUNTIME_FACTORY], module.ExternalTeamMemberFactory)
    enrich.assert_not_called()
    postgres.assert_awaited_once()
    postgres.reset_mock()
    with pytest.raises(ValueError, match='plan integration is not available'):
        await manager.get_swarm_enriched_team_spec('session', mode='team.code.plan', user_id='alice',
                                                  execution_route=host.route)
    postgres.assert_not_awaited()
    with pytest.raises(ValueError, match='request differs'):
        await manager.get_swarm_enriched_team_spec('session', mode='team.code.normal', user_id='mallory',
                                                  execution_route=host.route)
    postgres.assert_not_awaited()


@pytest.mark.asyncio
async def test_provider_stop_failure_keeps_transport_until_retry_confirms_exit(host, monkeypatch):
    leader = spec_for(host).build()
    provider = host.engines[0].harness
    owned = []
    real = module.ManagedProductToolTransport
    def transport(*args, **kwargs):
        value = real(*args, **kwargs)
        owned.append(value)
        return value
    monkeypatch.setattr(module, 'ManagedProductToolTransport', transport)
    await leader.harness.start(team_session=create_agent_team_session(session_id='session'))
    try:
        provider.fail_stop = True
        with pytest.raises(RuntimeError, match='exit failed'):
            await leader.harness.stop()
        assert owned[0].started
        with pytest.raises(HarnessStateError):
            await leader.harness.start(team_session=create_agent_team_session(session_id='session'))
    finally:
        provider.fail_stop = False
        await leader.harness.stop()
    assert owned[0].exit_confirmed and not owned[0].started


@pytest.mark.asyncio
async def test_cold_member_requires_checkpoint_and_valid_saved_checkpoint_is_reused(host):
    spec = spec_for(host)
    leader = spec.build()
    leader.team_backend.mark_history_restored()
    with pytest.raises(HarnessStateError, match='strict member recovery'):
        await leader.harness.start(team_session=create_agent_team_session(session_id='session'))
    assert host.engines[0].harness.contexts == []
    leader = spec.build()
    session = create_agent_team_session(session_id='session')
    try:
        await leader.harness.start(team_session=session)
        context = host.engines[-1].harness.contexts[-1]
        checkpoint = HarnessCheckpoint(
            provider=host.provider, schema_version='test', agent_id=context.agent_id,
            host_session_id=context.host_session_id, checkpoint_id='saved', sequence=1,
            provider_session_id='provider-' + context.agent_id, data={},
        )
        await context.checkpoint_sink.save(checkpoint, reason=CheckpointReason.TURN_COMPLETED)
    finally:
        await leader.harness.stop()
    restored = spec.build()
    restored.team_backend.mark_history_restored()
    try:
        await restored.harness.start(team_session=create_agent_team_session(session_id='session'))
        resumed = host.engines[-1].harness.contexts[-1]
        assert resumed.checkpoint == checkpoint
        assert resumed.resume_policy is ResumePolicy.REQUIRE_RESUME
    finally:
        await restored.harness.stop()


def test_real_provider_factory_is_unstarted_and_does_not_construct_native(host, monkeypatch):
    from openjiuwen.harness.engine import create_harness_engine
    monkeypatch.setattr(module, 'create_harness_engine', create_harness_engine)
    leader = spec_for(host).build()
    assert leader.harness.provider_name == host.provider
    assert leader.harness.harness.provider_session_id is None


def test_team_factory_requires_explicit_profile_even_when_default_matches(host):
    identity = replace(host.route.surface.identity, execution_profile_id='')
    surface = replace(host.route.surface, identity=identity)
    with pytest.raises(ValueError, match='explicit execution profile'):
        module.ExternalTeamMemberFactory(replace(host.route, surface=surface), team_name='team')
    assert host.engines == []


@pytest.mark.parametrize('mode', ['agent.code.normal', 'team.code.plan'])
def test_team_factory_does_not_admit_single_or_unintegrated_plan(host, mode):
    surface = replace(host.route.surface, initial_mode=mode)
    with pytest.raises(ValueError):
        module.ExternalTeamMemberFactory(replace(host.route, surface=surface), team_name='team')


@pytest.mark.asyncio
async def test_running_member_tool_admission_is_revoked_when_profile_changes(host):
    import httpx
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    leader = spec_for(host).build()
    try:
        async with asyncio.timeout(15):
            await leader.harness.start(team_session=create_agent_team_session(session_id='session'))
            config = host.engines[0].harness.contexts[0].mcp_servers[0]
            async with httpx.AsyncClient(headers=dict(config.headers)) as client:
                async with streamable_http_client(config.url, http_client=client) as (read, write, _):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        assert 'create_task' in {t.name for t in (await session.list_tools()).tools}
                        host.config['execution']['profiles']['selected']['config_revision'] = 'changed'
                        result = await session.call_tool('view_task', {'action': 'list'})
                        assert result.isError
                        assert 'not allowed' in result.content[0].text
    finally:
        await leader.harness.stop()


@pytest.mark.parametrize('verification', [False, True])
def test_scheduled_external_team_uses_selected_member_factory(host, verification):
    spec = TeamAgentSpec(agents={'leader': DeepAgentSpec()}, team_name='team',
                         evolution_enabled=False, spawn_mode='inprocess')
    spec.dispatch_mode = 'scheduled'
    spec.enable_task_verification = verification
    module.attach_external_team_execution(spec, host.route)
    leader = spec.build()
    assert spec.execution_provider == host.route.provider_id
    assert leader.harness.provider_name == host.route.provider_id
    assert len(host.engines) == 1


@pytest.mark.asyncio
async def test_unsupported_spawn_drift_rejected_again_before_member_start(host, monkeypatch):
    spec = spec_for(host)
    leader = spec.build()
    spec.spawn_mode = 'process'
    transport = Mock(side_effect=AssertionError('MCP allocated before rejection'))
    monkeypatch.setattr(module, 'ManagedProductToolTransport', transport)
    with pytest.raises(ValueError, match='requires inprocess'):
        await leader.harness.start(team_session=create_agent_team_session(session_id='session'))
    transport.assert_not_called()
    assert all(not engine.harness.contexts for engine in host.engines)
    await leader.harness.stop()
