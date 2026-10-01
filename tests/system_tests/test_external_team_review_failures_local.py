# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Real reviewer CLI failures with loopback models and SQLite cold admission.

This exercises the production reviewer host, not a whole Goal/Runner restart.
"""
import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path
import socket
import sys

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
from jiuwenswarm.runtime.harness.team_review import validate_review_recovery

pytestmark = [pytest.mark.integration, pytest.mark.system,
    pytest.mark.skipif(os.environ.get('RUN_TEAM_PROVIDER_LOCAL') != '1', reason='real reviewer CLI is opt-in')]

_COLD_READ = '''
import asyncio,json,sys
from sqlalchemy.ext.asyncio import create_async_engine
from openjiuwen.core.session.agent_team import create_agent_team_session
from openjiuwen.core.session.checkpointer import CheckpointerFactory
from openjiuwen.core.session.checkpointer.persistence import PersistenceCheckpointerProvider
from jiuwenswarm.runtime.harness.team_review import validate_review_recovery
async def main():
 engine=create_async_engine('sqlite+aiosqlite:///'+sys.argv[1])
 try:
  CheckpointerFactory.set_default_checkpointer(await PersistenceCheckpointerProvider().create({'db_client':engine}))
  session=create_agent_team_session(session_id='review-local',team_id='team')
  await session.pre_run()
  try:validate_review_recovery(session);allowed=True
  except ValueError:allowed=False
  records=session.get_state('external_team_reviews')
  assert len(records)==1
  print(json.dumps({'allowed':allowed,'status':next(iter(records.values()))['status']}))
 finally:await engine.dispose()
asyncio.run(main())
'''


async def cold_read(path):
    child = await asyncio.create_subprocess_exec(sys.executable, '-c', _COLD_READ, str(path),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, error = await asyncio.wait_for(child.communicate(), 25)
        assert child.returncode == 0, error.decode()
        return json.loads(out.decode().splitlines()[-1])
    finally:
        if child.returncode is None:
            child.kill()
            await child.wait()


@pytest.mark.asyncio
@pytest.mark.parametrize('provider', ['codex', 'opencode'])
@pytest.mark.parametrize('failure', ['model-rejection', 'cancel-active'])
async def test_real_review_failure_persists_blocked_and_refuses_replay(tmp_path, monkeypatch, provider, failure):
    root, home = tmp_path / 'project', tmp_path / 'home'
    root.mkdir(mode=0o700); home.mkdir(mode=0o700)
    (tmp_path / 'provider-runtime').mkdir(mode=0o700)
    monkeypatch.setenv('OPENJIUWEN_HOME', str(home / 'core'))
    monkeypatch.setenv('JIUWENSWARM_DATA_DIR', str(home / 'swarm'))
    from jiuwenswarm.server.runtime.session import session_history
    monkeypatch.setattr(session_history, 'get_agent_sessions_dir', lambda: home / 'sessions')
    entered, release = asyncio.Event(), asyncio.Event()
    requests = []
    async def respond(request):
        requests.append(await request.json())
        entered.set()
        if failure == 'cancel-active':
            await release.wait()
        return web.json_response({'error': {'message': 'fixture model rejects this request',
                                           'type': 'invalid_request_error', 'code': 'invalid_api_key'}}, status=401)
    app = web.Application()
    app.router.add_post('/v1/responses', respond)
    app.router.add_post('/v1/chat/completions', respond)
    server = web.AppRunner(app)
    await server.setup()
    sock = socket.socket(); sock.bind(('127.0.0.1', 0))
    port = sock.getsockname()[1]
    await web.SockSite(server, sock).start()
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'checkpoints.db'}")
    previous = CheckpointerFactory.get_checkpointer()
    CheckpointerFactory.set_default_checkpointer(await PersistenceCheckpointerProvider().create({'db_client': engine}))
    from openjiuwen.agent_teams.spawn import shared_resources
    shared_resources.cleanup_shared_resources()
    token = set_session_id('review-local')
    leader = review = running = reader = session = None
    chunks = []
    try:
        settings = {'model': {'model': 'fixture', 'api_base': f'http://127.0.0.1:{port}/v1', 'api_key': 'local-only'}}
        if provider == 'codex':
            codex_home = tmp_path / 'codex-home'; (codex_home / 'skills').mkdir(parents=True)
            settings.update({'inherit_process_env': False, 'mcp_required': True,
                'env': {'HOME': str(home), 'CODEX_HOME': str(codex_home), 'PATH': os.environ.get('PATH', '/usr/bin:/bin')},
                'startup_source_roots': [str(root), str(codex_home / 'skills')],
                'turn_idle_timeout_s': 20, 'turn_idle_retries': 0})
            settings['model'].update({'model': 'gpt-5.6-sol', 'provider': 'review_fixture'})
        else:
            cli = Path(os.environ.get('OPENCODE_OC1_CLI', str(Path.home() / '.opencode/bin/opencode')))
            assert cli.is_file(), 'OpenCode CLI required for this explicit validation'
            settings.update({'cli_path': str(cli), 'runtime_root': str(tmp_path / 'provider-runtime'), 'turn_timeout_s': 30})
        config = {'execution': {'default_profile_id': 'review-local', 'profiles': {'review-local': {
            'provider_id': provider, 'config_revision': 'review-local-1', 'provider_config': settings,
            'authorization': {'full_access': True}}}}}
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
        request = TeamReviewRuntimeBuild(spec, 'reviewer', 'work', 1, 'failure-invocation', 'Review output', 'en',
            tools, session, spec.build_context)
        review = factory.build_review_runtime(request)
        running = asyncio.create_task(review.run_once('Inspect this task.'))
        waiting = asyncio.create_task(entered.wait())
        try:
            done, _ = await asyncio.wait({waiting, running}, timeout=35, return_when=asyncio.FIRST_COMPLETED)
            if not entered.is_set():
                assert not running.done(), ('Reviewer failed before model request', await asyncio.gather(running, return_exceptions=True), review._terminal)
                assert done, 'No model request before timeout'
        finally:
            waiting.cancel()
            await asyncio.gather(waiting, return_exceptions=True)
        if failure == 'cancel-active':
            assert await cold_read(tmp_path / 'checkpoints.db') == {'allowed': False, 'status': 'pending'}
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)
        else:
            with pytest.raises(RuntimeError, match='did not finish successfully'):
                await asyncio.wait_for(running, 35)
        await asyncio.wait_for(review.dispose(), 20)
        assert review.runtime.harness.state is HarnessState.TERMINATED
        assert review._transport.exit_confirmed and not review._transport.started
        assert review._terminal.kind is (TurnEventKind.ABORTED if failure == 'cancel-active' else TurnEventKind.FAILED)
        assert await cold_read(tmp_path / 'checkpoints.db') == {'allowed': False, 'status': 'blocked'}
        restored = create_agent_team_session(session_id='review-local', team_id='team')
        await restored.pre_run()
        with pytest.raises(ValueError, match='unconfirmed'):
            validate_review_recovery(restored)
        count = len(requests)
        again = factory.build_review_runtime(replace(request, invocation_id='replay-attempt', team_session=restored))
        try:
            with pytest.raises(ValueError, match='replay refused'):
                await again.run_once('Retry')
            assert again._transport is None and again.runtime.harness.provider_session_id is None
            assert len(requests) == count
        finally:
            await again.dispose()
        await session.close_stream()
        await asyncio.wait_for(reader, 5)
        assert review._history_error is None
        assert chunks, 'original Team stream must observe the terminal'
    finally:
        if running is not None and not running.done():
            running.cancel()
        if review is not None:
            await asyncio.wait_for(review.dispose(), 20)
        release.set()
        if session is not None:
            await session.close_stream()
        if reader is not None:
            await asyncio.gather(reader, return_exceptions=True)
        if running is not None:
            await asyncio.gather(running, return_exceptions=True)
        if leader is not None:
            await leader.harness.stop()
        for database in tuple(shared_resources._db_instances.values()):
            await database.close()
        shared_resources.cleanup_shared_resources()
        reset_session_id(token)
        CheckpointerFactory.set_default_checkpointer(previous)
        await engine.dispose()
        await server.cleanup()
