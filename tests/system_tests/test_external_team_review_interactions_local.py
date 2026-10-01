# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Real reviewer CLI approvals with loopback models and SQLite cold admission.

This exercises the production reviewer host, not a whole Goal/Runner restart.
"""
import asyncio
import json
import os
from pathlib import Path
import socket
from contextlib import asynccontextmanager
from types import SimpleNamespace

from openjiuwen.core.session import InteractiveInput
from openjiuwen.agent_teams.external.interaction_address import decode_interaction_address, encode_interaction_address
from openjiuwen.harness_protocol import HarnessStateError
from tests.system_tests.test_external_codex_product_route_local import _ResponsesFixture
from tests.system_tests.test_external_opencode_product_route_local import _ModelFixture
from tests.system_tests.test_external_team_review_failures_local import cold_read

import pytest
from aiohttp import web
from sqlalchemy.ext.asyncio import create_async_engine

from openjiuwen.agent_teams import TeamAgentSpec, TeamReviewRuntimeBuild
from openjiuwen.agent_teams.context import reset_session_id, set_session_id
from openjiuwen.agent_teams.schema.blueprint import DeepAgentSpec, StorageSpec
from openjiuwen.agent_teams.tools.locales import make_translator
from openjiuwen.agent_teams.tools.task_manager import TeamTaskManager
from openjiuwen.agent_teams.tools.tool_task import VerifyTaskTool, ViewTaskToolV2
from openjiuwen.core.session.agent_team import create_agent_team_session
from openjiuwen.core.session.checkpointer import CheckpointerFactory
from openjiuwen.core.session.checkpointer.persistence import PersistenceCheckpointerProvider
from openjiuwen.harness.engine import ExecutionBinding
from openjiuwen.harness_protocol import HarnessState, TurnEventKind
from jiuwenswarm.common.runtime_workspace import RuntimeWorkspacePaths
from jiuwenswarm.runtime.harness import team_execution as module
from jiuwenswarm.runtime.harness.binding_store import BoundExecution, ExecutionBindingStore
from jiuwenswarm.runtime.harness.config_source import load_execution_catalog
from jiuwenswarm.runtime.harness.request_binding import AdmittedExecutionRoute
from jiuwenswarm.runtime.harness.surface import EffectiveSurfaceSnapshot, build_surface_identity

pytestmark = [pytest.mark.integration, pytest.mark.system,
    pytest.mark.skipif(os.environ.get('RUN_TEAM_PROVIDER_LOCAL') != '1', reason='real reviewer CLI is opt-in')]


@asynccontextmanager
async def review_host(tmp_path, monkeypatch, provider, base_url, *, full_access):
    root, home = tmp_path / 'project', tmp_path / 'home'
    root.mkdir(mode=0o700, exist_ok=True); home.mkdir(mode=0o700, exist_ok=True)
    (tmp_path / 'provider-runtime').mkdir(mode=0o700, exist_ok=True)
    monkeypatch.setenv('OPENJIUWEN_HOME', str(home / 'core'))
    monkeypatch.setenv('JIUWENSWARM_DATA_DIR', str(home / 'swarm'))
    from jiuwenswarm.server.runtime.session import session_history
    monkeypatch.setattr(session_history, 'get_agent_sessions_dir', lambda: home / 'sessions')
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'checkpoints.db'}")
    previous = CheckpointerFactory.get_checkpointer()
    CheckpointerFactory.set_default_checkpointer(await PersistenceCheckpointerProvider().create({'db_client': engine}))
    from openjiuwen.agent_teams.spawn import shared_resources
    shared_resources.cleanup_shared_resources()
    token = set_session_id('review-local')
    leader = review = reader = session = None
    chunks = []
    try:
        settings = {'model': {'model': 'fixture', 'api_base': base_url, 'api_key': 'local-only'}}
        if provider == 'codex':
            codex_home = tmp_path / 'codex-home'; (codex_home / 'skills').mkdir(parents=True, exist_ok=True)
            settings.update({'inherit_process_env': False, 'mcp_required': True,
                'env': {'HOME': str(home), 'CODEX_HOME': str(codex_home), 'PATH': os.environ.get('PATH', '/usr/bin:/bin')},
                'startup_source_roots': [str(root), str(codex_home / 'skills')],
                'turn_idle_timeout_s': 20, 'turn_idle_retries': 0})
            settings['model'].update({'model': 'gpt-5.6-sol', 'provider': 'review_fixture'})
            if not full_access:
                import openai_codex as sdk
                binary = sdk.client._resolve_codex_bin(sdk.CodexConfig())
                readable = {':minimal': 'read', str(root): 'write', str(codex_home / 'tmp'): 'read',
                    str(Path(binary).parent): 'read'}
                (codex_home / 'config.toml').write_text(
                    'default_permissions = "review-read"\n[permissions.review-read.filesystem]\n'
                    + '\n'.join(f'{json.dumps(path)} = {json.dumps(access)}' for path, access in readable.items())
                    + '\n[permissions.review-read.network]\nenabled=false\n')
        else:
            cli = Path(os.environ.get('OPENCODE_OC1_CLI', str(Path.home() / '.opencode/bin/opencode')))
            assert cli.is_file(), 'OpenCode CLI required for this explicit validation'
            settings.update({'cli_path': str(cli), 'runtime_root': str(tmp_path / 'provider-runtime'), 'turn_timeout_s': 30})
        config = {'execution': {'default_profile_id': 'review-local', 'profiles': {'review-local': {
            'provider_id': provider, 'config_revision': 'review-local-1', 'provider_config': settings,
            'authorization': {'full_access': full_access}}}}}
        source = load_execution_catalog(config).source(explicit_profile_id='review-local')
        binding = ExecutionBinding.create(source.resolve(), subject_id='fixture-owner', host_session_id='review-local', workspace=str(root))
        paths = RuntimeWorkspacePaths(home, root, root, root)
        metadata = {'session_id': 'review-local', 'user_id': 'fixture-owner', 'channel_id': 'local',
            'team_name': 'team', 'mode': 'team.code.normal', 'work_mode': 'code', 'project_dir': str(root),
            'execution_profile_id': 'review-local', 'execution_config_revision': binding.config_revision,
            'execution_config_fingerprint': binding.fingerprint}
        monkeypatch.setattr(module, '_host_config', lambda: config)
        monkeypatch.setattr(module, '_session_metadata', lambda _: metadata)
        identity = build_surface_identity(metadata=metadata, binding=binding, paths=paths, channel_id='local')
        route = AdmittedExecutionRoute('local', source, ExecutionBindingStore(), BoundExecution(binding, source.resolve()),
            paths, surface=EffectiveSurfaceSnapshot(identity, metadata['mode']))
        spec = TeamAgentSpec(agents={'leader': DeepAgentSpec(), 'teammate': DeepAgentSpec()}, team_name='team',
            evolution_enabled=False, spawn_mode='inprocess', storage=StorageSpec(type='memory'))
        module.attach_external_team_execution(spec, route)
        leader = spec.build()
        spec.dispatch_mode = 'scheduled'
        manager = TeamTaskManager(team_name='team', member_name='reviewer', db=leader.team_backend.db,
            messager=None, dispatch_mode='scheduled')
        tr = make_translator('en')
        tools = (VerifyTaskTool(manager, tr, desc_key='verify_task_scheduled'), ViewTaskToolV2(leader.team_backend, tr))
        session = create_agent_team_session(session_id='review-local', team_id='team')
        await session.pre_run()
        async def consume():
            async for chunk in session.stream_iterator():
                chunks.append(chunk)
        reader = asyncio.create_task(consume())
        factory = module.ExternalTeamMemberFactory(route, team_name='team')
        request = TeamReviewRuntimeBuild(spec, 'reviewer', 'work', 1, 'interaction-invocation', 'Review output', 'en',
            tools, session, spec.build_context)
        review = factory.build_review_runtime(request)
        yield SimpleNamespace(review=review, factory=factory, request=request, leader=leader,
            session=session, chunks=chunks, root=root, route=route, spec=spec)
    finally:
        try:
            if review is not None:
                await asyncio.wait_for(review.dispose(), 20)
        finally:
            if session is not None:
                await session.close_stream()
            if reader is not None:
                await asyncio.gather(reader, return_exceptions=True)
            if leader is not None:
                await leader.harness.stop()
            for database in tuple(shared_resources._db_instances.values()):
                await database.close()
            shared_resources.cleanup_shared_resources()
            reset_session_id(token)
            CheckpointerFactory.set_default_checkpointer(previous)
            await engine.dispose()

@asynccontextmanager
async def local_model(provider, *, opencode_model=None):
    if provider == 'codex':
        with _ResponsesFixture() as model:
            yield model, model.base_url
    else:
        model = opencode_model or _ModelFixture()
        app = web.Application()
        app.router.add_post('/v1/chat/completions', model.respond)
        server = web.AppRunner(app)
        await server.setup()
        sock = socket.socket()
        sock.bind(('127.0.0.1', 0))
        await web.SockSite(server, sock).start()
        try:
            yield model, f'http://127.0.0.1:{sock.getsockname()[1]}/v1'
        finally:
            await server.cleanup()


def reply(key, approved):
    answer = InteractiveInput()
    answer.update(key, {'approved': approved})
    return answer


@pytest.mark.asyncio
@pytest.mark.parametrize('provider', ['codex', 'opencode'])
@pytest.mark.parametrize('decision', ['allow', 'deny', 'cancel'])
async def test_real_reviewer_approval_and_late_answers(tmp_path, monkeypatch, provider, decision):
    async with local_model(provider) as (model, url):
        command = 'printf REVIEW-APPROVED > approval-marker.txt'
        if provider == 'codex':
            model.items.append({'type': 'function_call', 'name': 'exec_command', 'id': 'fc_review',
                'call_id': 'call_review', 'arguments': json.dumps({'cmd': command, 'workdir': str(tmp_path / 'project')})})
        else:
            model.actions = [{'tool': 'bash', 'args': {'command': command, 'description': 'Reviewer approval fixture'}}]
        async with review_host(tmp_path, monkeypatch, provider, url, full_access=False) as host:
            review = host.review
            declined = provider == 'opencode' and decision == 'deny'
            running = asyncio.create_task(review.run_once('Inspect using the prescribed tool once.'))
            try:
                async with asyncio.timeout(40):
                    while not any((chunk.payload or {}).get('event_type') == 'chat.ask_user_question' for chunk in host.chunks):
                        if running.done():
                            await running
                            pytest.fail('Reviewer completed without native approval')
                        await asyncio.sleep(.01)
                interaction = next(chunk for chunk in host.chunks if (chunk.payload or {}).get('event_type') == 'chat.ask_user_question')
                key = interaction.payload['request_id']
                address = decode_interaction_address(key)
                assert address[:3] == ('team', review.interaction_owner, 'review-local')
                assert await cold_read(tmp_path / 'checkpoints.db') == {'allowed': False, 'status': 'pending'}
                marker = host.root / 'approval-marker.txt'
                assert not marker.exists() and not running.done()
                before = len(model.requests)
                for index in range(4):
                    wrong = list(address)
                    wrong[index] += '-stale'
                    with pytest.raises(HarnessStateError):
                        await review.send(reply(encode_interaction_address(*wrong), True))
                    assert review.is_pending_interrupt_resume_valid(reply(key, True))
                assert len(model.requests) == before and not marker.exists()
                if decision == 'cancel':
                    running.cancel()
                    await asyncio.gather(running, return_exceptions=True)
                else:
                    await review.send(reply(key, decision == 'allow'))
                    with pytest.raises(HarnessStateError):
                        await review.send(reply(key, True))
                    if declined:
                        with pytest.raises(RuntimeError, match='did not finish successfully'):
                            await asyncio.wait_for(running, 35)
                        assert review._terminal.result.error.code == 'interaction_declined'
                    else:
                        await asyncio.wait_for(running, 35)
                await asyncio.wait_for(review.dispose(), 20)
                assert marker.exists() is (decision == 'allow')
                if marker.exists():
                    assert marker.read_text() == 'REVIEW-APPROVED'
                assert review.runtime.harness.state is HarnessState.TERMINATED
                assert review._transport.exit_confirmed and not review._transport.started
                assert review._history_error is None
                assert review._terminal.kind is (TurnEventKind.ABORTED if decision == 'cancel' else TurnEventKind.FAILED if declined else TurnEventKind.FINISHED)
                assert await cold_read(tmp_path / 'checkpoints.db') == {
                    'allowed': decision != 'cancel' and not declined, 'status': 'blocked' if decision == 'cancel' or declined else 'closed'}
                before = len(model.requests)
                with pytest.raises(HarnessStateError):
                    await review.send(reply(key, True))
                assert len(model.requests) == before
                assert len([c for c in host.chunks if (c.payload or {}).get('event_type') == 'chat.ask_user_question']) == 1
            finally:
                if not running.done():
                    running.cancel()
                await review.dispose()
                await asyncio.gather(running, return_exceptions=True)
