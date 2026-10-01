"""Original SQLite checkpoints across process boundaries and reviewer recovery admission."""
import asyncio
import json
import os
import sys
from dataclasses import replace
from unittest.mock import Mock

import pytest
from sqlalchemy.ext.asyncio import create_async_engine
from openjiuwen.core.session.agent_team import create_agent_team_session
from openjiuwen.core.session.checkpointer import CheckpointerFactory
from openjiuwen.core.session.checkpointer.persistence import PersistenceCheckpointerProvider
from openjiuwen.harness_protocol import TurnEventKind, TurnStatus, TurnTermination, TurnTerminationKind
from jiuwenswarm.runtime.harness import team_execution as module
from jiuwenswarm.runtime.harness.team_review import validate_review_recovery
from tests.unit_tests.runtime.harness.test_team_execution import host, ScriptedHarness
from tests.unit_tests.runtime.harness.test_team_review import prepare


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
  cp=await PersistenceCheckpointerProvider().create({'db_client':engine})
  CheckpointerFactory.set_default_checkpointer(cp)
  session=create_agent_team_session(session_id='session',team_id='team')
  await session.pre_run()
  try:validate_review_recovery(session);allowed=True
  except ValueError:allowed=False
  records=session.get_state('external_team_reviews')
  assert records and len(records)==1
  print(json.dumps({'allowed':allowed,'status':next(iter(records.values()))['status']}))
 finally:await engine.dispose()
asyncio.run(main())
'''


async def cold_read(path):
    child = await asyncio.create_subprocess_exec(sys.executable, '-c', _COLD_READ, str(path),
        env=os.environ.copy(), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        stdout, stderr = await asyncio.wait_for(child.communicate(), 30)
        assert child.returncode == 0, stderr.decode()
        return json.loads(stdout.decode().splitlines()[-1])
    finally:
        if child.returncode is None:
            child.kill()
            await child.wait()


@pytest.mark.asyncio
@pytest.mark.parametrize('stage', ['pending', 'blocked', 'closed'])
async def test_sqlite_review_state_survives_fresh_process(host, monkeypatch, tmp_path, stage):
    path = tmp_path / 'review-checkpoint.db'
    engine = create_async_engine(f'sqlite+aiosqlite:///{path}')
    checkpoint = await PersistenceCheckpointerProvider().create({'db_client': engine})
    CheckpointerFactory.set_default_checkpointer(checkpoint)
    factory, request, leader = await prepare(host, monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()
    original = ScriptedHarness._execute_turn
    async def execute(self, turn):
        entered.set()
        await release.wait()
        kind, result = await original(self, turn)
        return (TurnEventKind.ABORTED, replace(result, status=TurnStatus.INTERRUPTED, termination=TurnTermination(TurnTerminationKind.HARNESS_STOP))) if turn.stop_requested else (kind, result)
    async def close(self):
        release.set()
    monkeypatch.setattr(ScriptedHarness, '_execute_turn', execute)
    monkeypatch.setattr(ScriptedHarness, '_close_session', close)
    review = factory.build_review_runtime(request)
    running = asyncio.create_task(review.run_once('Inspect'))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        if stage == 'closed':
            release.set()
            assert await asyncio.wait_for(running, 5) == 'done'
            await review.dispose()
        elif stage == 'blocked':
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)
            await review.dispose()
        assert await cold_read(path) == {'allowed': stage == 'closed', 'status': stage}
        restored = create_agent_team_session(session_id='session', team_id='team')
        await restored.pre_run()
        if stage != 'closed':
            # A fresh factory must not dispatch another invocation, even when
            # the board still has no vote from this reviewer.
            another_factory = module.ExternalTeamMemberFactory(host.route, team_name='team')
            another = another_factory.build_review_runtime(replace(request, invocation_id='recovery', team_session=restored))
            try:
                with pytest.raises(ValueError, match='replay refused'):
                    await another.run_once('Retry')
                assert not another.runtime.harness.contexts
            finally:
                await another.dispose()
    finally:
        running.cancel()
        await review.dispose()
        await asyncio.gather(running, return_exceptions=True)
        await leader.harness.stop()
        await leader.team_backend.db.close()
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize('history_restored', [False, True])
async def test_leader_recovery_guard_precedes_provider_and_mcp_even_without_history_flag(host, monkeypatch, history_restored):
    factory, request, leader = await prepare(host, monkeypatch)
    request.team_session.update_state({'external_team_reviews': {'old': {'status': 'pending'}}})
    await request.team_session.commit()
    if history_restored:
        leader.team_backend.mark_history_restored()
    monkeypatch.setattr(module.ExternalTeamMemberFactory, 'validate_team_spec',
                        lambda f, s: f._validate_team_spec(s, scheduled=True))
    transport = Mock(side_effect=AssertionError('transport allocated before recovery rejection'))
    monkeypatch.setattr(module, 'ManagedProductToolTransport', transport)
    try:
        with pytest.raises(ValueError, match='unconfirmed'):
            await leader.harness.start(team_session=request.team_session)
        assert not leader.harness.harness.contexts
        transport.assert_not_called()
    finally:
        await leader.harness.stop()
        await leader.team_backend.db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('corrupt', [[], False, 0, '', {'key': []}])
async def test_corrupt_review_checkpoint_never_becomes_empty_history(host, monkeypatch, corrupt):
    factory, request, leader = await prepare(host, monkeypatch)
    request.team_session.update_state({'external_team_reviews': corrupt})
    try:
        with pytest.raises(ValueError):
            validate_review_recovery(request.team_session)
    finally:
        await leader.harness.stop()
        await leader.team_backend.db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['error', 'cancel'])
async def test_failed_closure_commit_retains_pending_until_retry(host, monkeypatch, failure):
    factory, request, leader = await prepare(host, monkeypatch)
    review = factory.build_review_runtime(request)
    original = request.team_session.commit
    async def fail():
        if failure == 'cancel':
            raise asyncio.CancelledError()
        raise OSError('checkpoint unavailable')
    try:
        await review.run_once('Inspect')
        monkeypatch.setattr(request.team_session, 'commit', fail)
        with pytest.raises(asyncio.CancelledError if failure == 'cancel' else OSError):
            await review.dispose()
        assert not review._closed
        assert next(iter(request.team_session.get_state('external_team_reviews').values()))['status'] == 'pending'
        with pytest.raises(ValueError, match='unconfirmed'):
            validate_review_recovery(request.team_session)
        monkeypatch.setattr(request.team_session, 'commit', original)
        await review.dispose()
        validate_review_recovery(request.team_session)
    finally:
        monkeypatch.setattr(request.team_session, 'commit', original)
        await review.dispose()
        await leader.harness.stop()
        await leader.team_backend.db.close()


@pytest.mark.asyncio
async def test_partial_votes_survive_scheduler_and_sqlite_reconstruction(host, monkeypatch, tmp_path):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from openjiuwen.agent_teams import TEAM_MEMBER_RUNTIME_FACTORY
    from openjiuwen.agent_teams.agent.infra import TeamInfra
    from openjiuwen.agent_teams.agent.scheduling import TeamScheduler
    from openjiuwen.agent_teams.context import set_session_id, reset_session_id
    from openjiuwen.agent_teams.messager import Messager
    from openjiuwen.agent_teams.schema.task import TaskGraphSpec
    from openjiuwen.agent_teams.tools.database import DatabaseConfig, DatabaseType, TeamDatabase
    from openjiuwen.agent_teams.tools.task_manager import TeamTaskManager
    from openjiuwen.core.single_agent import AgentCard
    from openjiuwen.harness_protocol import ToolInvocation
    from jiuwenswarm.runtime.harness import team_review

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'checkpoint.db'}")
    CheckpointerFactory.set_default_checkpointer(await PersistenceCheckpointerProvider().create({'db_client': engine}))
    factory, request, leader = await prepare(host, monkeypatch)
    spec = request.spec
    token = set_session_id('session')
    config = DatabaseConfig(db_type=DatabaseType.SQLITE, connection_string=str(tmp_path / 'board.db'))
    db = TeamDatabase(config)
    bus = AsyncMock(spec=Messager)
    reviews, gateways = [], {}
    phase = 1
    original_gateway = team_review.ProductToolGateway
    def gateway(*args, **kwargs):
        value = original_gateway(*args, **kwargs)
        gateways[kwargs['invoke_kwargs']['member_name']] = value
        return value
    monkeypatch.setattr(team_review, 'ProductToolGateway', gateway)
    original_execute = ScriptedHarness._execute_turn
    async def execute(self, turn):
        name = self.contexts[-1].metadata['source_member']
        if name == 'reviewer' or phase == 2:
            result = await gateways[name].invoke(ToolInvocation(call_id='vote', name='verify_task',
                arguments={'task_id': 'work', 'decision': 'pass'}))
            assert not result.is_error
        return await original_execute(self, turn)
    monkeypatch.setattr(ScriptedHarness, '_execute_turn', execute)
    monkeypatch.setattr(module.ExternalTeamMemberFactory, 'validate_team_spec',
                        lambda f, s: f._validate_team_spec(s, scheduled=True))
    original_build = module.ExternalTeamMemberFactory.build_review_runtime
    def build(self, request):
        result = original_build(self, request)
        reviews.append(result)
        return result
    monkeypatch.setattr(module.ExternalTeamMemberFactory, 'build_review_runtime', build)
    scheduler = None
    try:
        await db.initialize()
        await db.team.create_team(team_name='team', display_name='Team', leader_member_name='team_leader',
                                  dispatch_mode='scheduled')
        for name in ('worker', 'reviewer', 'reviewer-two'):
            await db.member.create_member(member_name=name, team_name='team', display_name=name,
                agent_card=AgentCard().model_dump_json(), status='READY', mode='build')
        for phase in (1, 2):
            if phase == 2:
                await db.close()
                await engine.dispose()
                db = TeamDatabase(config)
                await db.initialize()
                CheckpointerFactory.set_default_checkpointer(
                    await PersistenceCheckpointerProvider().create({'db_client': engine}))
            session = create_agent_team_session(session_id='session', team_id='team')
            await session.pre_run()
            session.write_stream = AsyncMock()
            validate_review_recovery(session)
            tm = TeamTaskManager(team_name='team', member_name='team_leader', db=db,
                                 messager=bus, dispatch_mode='scheduled')
            if phase == 1:
                assert (await tm.add_graph([TaskGraphSpec(task_id='work', title='Work', content='Review',
                    assignee='worker', reviewer=('reviewer', 'reviewer-two'))])).ok
                assert (await tm.start_task('work')).ok
                worker = TeamTaskManager(team_name='team', member_name='worker', db=db,
                    messager=bus, dispatch_mode='scheduled')
                assert (await worker.complete('work')).ok
            else:
                assert (await tm.get('work')).status == 'in_review'
                assert set((await tm.get_review_tally(await tm.get('work')))['voted']) == {'reviewer'}
            spec.build_context.extras[TEAM_MEMBER_RUNTIME_FACTORY] = module.ExternalTeamMemberFactory(host.route, team_name='team')
            infra = TeamInfra()
            infra.task_manager, infra.message_manager, infra.messager = tm, AsyncMock(), bus
            infra.team_backend = SimpleNamespace(team_name='team', db=db, workspace_cache=None,
                                                 task_verification_enabled=lambda: True)
            owner = SimpleNamespace(session_manager=SimpleNamespace(team_session=session),
                stream_controller=SimpleNamespace(stream_queue=asyncio.Queue()),
                harness=SimpleNamespace(find_rails=lambda _: []), deliver_input=AsyncMock(), auto_start_member=AsyncMock())
            scheduler = TeamScheduler(owner, blueprint=SimpleNamespace(spec=spec, team_name='team', language='en'),
                                      infra=infra, build_context=spec.build_context)
            await scheduler.activate()
            async with asyncio.timeout(10):
                while scheduler._review_runs:
                    await asyncio.sleep(.01)
            validate_review_recovery(session)
            records = session.get_state('external_team_reviews')
            assert len(records) == 2 and all(record['status'] == 'closed' for record in records.values())
            task = await tm.get('work')
            assert task.status == ('in_review' if phase == 1 else 'completed')
            assert set((await tm.get_review_tally(task))['voted']) == ({'reviewer'} if phase == 1 else {'reviewer', 'reviewer-two'})
            assert len(reviews) == (2 if phase == 1 else 3)
            await scheduler.stop_reviewers()
        assert [r.request.reviewer for r in reviews].count('reviewer') == 1
        assert len({r.request.invocation_id for r in reviews}) == 3
        assert all(r._closed and r._transport.exit_confirmed and r.runtime.harness.readers == 1 for r in reviews)
    finally:
        if scheduler is not None:
            await scheduler.stop_reviewers()
        await db.close()
        reset_session_id(token)
        await leader.harness.stop()
        await leader.team_backend.db.close()
        await engine.dispose()
