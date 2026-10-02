"""Real reviewer scheduler rebuilt in a second Python process over original stores.

This is graceful reviewer-host restart, not whole Runner/Goal or crash recovery.
"""
import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from openjiuwen.agent_teams import TEAM_MEMBER_RUNTIME_FACTORY
from openjiuwen.agent_teams.agent.infra import TeamInfra
from openjiuwen.agent_teams.agent.scheduling import TeamScheduler
from openjiuwen.agent_teams.messager import Messager
from openjiuwen.agent_teams.schema.task import TaskGraphSpec
from openjiuwen.agent_teams.tools.database import DatabaseConfig, DatabaseType, TeamDatabase
from openjiuwen.agent_teams.tools.task_manager import TeamTaskManager
from openjiuwen.core.single_agent import AgentCard
from jiuwenswarm.runtime.harness import team_execution as module
from jiuwenswarm.runtime.harness.team_review import validate_review_recovery
from tests.system_tests.test_external_opencode_product_route_local import _ModelFixture
from tests.system_tests.test_external_team_review_interactions_local import local_model, review_host

pytestmark = [pytest.mark.integration, pytest.mark.system,
    pytest.mark.skipif(os.environ.get('RUN_TEAM_PROVIDER_LOCAL') != '1', reason='real reviewer CLI is opt-in')]


async def host_phase(root, provider, phase, url):
    # Each phase constructs its own real CLI processes against one stable model endpoint.
    # The only application continuity is the original board/checkpoint/history.
    with pytest.MonkeyPatch.context() as patch:
        async with review_host(root, patch, provider, url, full_access=True) as host:
            config = DatabaseConfig(db_type=DatabaseType.SQLITE, connection_string=str(root / 'board.db'))
            db = TeamDatabase(config)
            bus = AsyncMock(spec=Messager)
            reviews = []
            original_build = module.ExternalTeamMemberFactory.build_review_runtime
            def build(factory, request):
                request = replace(request, system_prompt=request.system_prompt + '\nRESTART_PRIVATE_' + request.reviewer + f'\nRESTART_PHASE_{phase}')
                runtime = original_build(factory, request)
                reviews.append(runtime)
                return runtime
            patch.setattr(module.ExternalTeamMemberFactory, 'build_review_runtime', build)
            patch.setattr(module.ExternalTeamMemberFactory, 'validate_team_spec',
                lambda factory, spec: factory._validate_team_spec(spec, scheduled=True))
            scheduler = None
            try:
                await db.initialize()
                await host.leader.team_backend.db.initialize()
                tm = TeamTaskManager(team_name='team', member_name='team_leader', db=db,
                    messager=bus, dispatch_mode='scheduled')
                if phase == 1:
                    await db.team.create_team(team_name='team', display_name='Team', leader_member_name='team_leader', dispatch_mode='scheduled')
                    for name in ('worker', 'reviewer_a', 'reviewer_b'):
                        await db.member.create_member(member_name=name, team_name='team', display_name=name,
                            agent_card=AgentCard().model_dump_json(), status='READY', mode='build')
                    assert (await tm.add_graph([TaskGraphSpec(task_id='work', title='Work', content='Review',
                        assignee='worker', reviewer=('reviewer_a', 'reviewer_b'))])).ok
                    assert (await tm.start_task('work')).ok
                    worker = TeamTaskManager(team_name='team', member_name='worker', db=db, messager=bus, dispatch_mode='scheduled')
                    assert (await worker.complete('work')).ok
                else:
                    assert (await tm.get('work')).status == 'in_review'
                    assert set((await tm.get_review_tally(await tm.get('work')))['voted']) == {'reviewer_a'}
                validate_review_recovery(host.session)
                # Persist a real leader Turn before scheduling. Codex does
                # not persist a native rollout for a start/stop-only thread.
                await host.leader.harness.start(team_session=host.session)
                leader_session = host.leader.harness.session_id
                await host.leader.harness.send('Prepare the review phase.')
                async with asyncio.timeout(30):
                    async for chunk in host.leader.harness.outputs():
                        if (chunk.payload or {}).get('terminal_status'):
                            assert chunk.payload['terminal_status'] == 'completed', chunk.payload
                            break
                    else:
                        raise AssertionError('Leader stream closed without terminal')
                await host.leader.harness.stop()
                host.spec.build_context.extras[TEAM_MEMBER_RUNTIME_FACTORY] = host.factory
                infra = TeamInfra()
                infra.task_manager, infra.message_manager, infra.messager = tm, AsyncMock(), bus
                infra.team_backend = SimpleNamespace(team_name='team', db=db, workspace_cache=None,
                    task_verification_enabled=lambda: True)
                owner = SimpleNamespace(session_manager=SimpleNamespace(team_session=host.session),
                    stream_controller=SimpleNamespace(stream_queue=asyncio.Queue()),
                    harness=SimpleNamespace(find_rails=lambda _: []), deliver_input=AsyncMock(), auto_start_member=AsyncMock())
                scheduler = TeamScheduler(owner, blueprint=SimpleNamespace(spec=host.spec, team_name='team', language='en'),
                    infra=infra, build_context=host.spec.build_context)
                await scheduler.activate()
                async with asyncio.timeout(60):
                    while scheduler._review_runs:
                        await asyncio.sleep(.02)
                task = await tm.get('work')
                voted = set((await tm.get_review_tally(task))['voted'])
                assert task.status == ('in_review' if phase == 1 else 'completed')
                assert voted == ({'reviewer_a'} if phase == 1 else {'reviewer_a', 'reviewer_b'})
                assert {r.request.reviewer for r in reviews} == ({'reviewer_a', 'reviewer_b'} if phase == 1 else {'reviewer_b'})
                assert len(reviews) == (2 if phase == 1 else 1)
                assert all(r._closed and r._transport.exit_confirmed and r._history_error is None for r in reviews)
                validate_review_recovery(host.session)
                records = host.session.get_state('external_team_reviews')
                assert len(records) == 2 and all(r['status'] == 'closed' for r in records.values())
                review_chunks = []
                while not owner.stream_controller.stream_queue.empty():
                    review_chunks.append(owner.stream_controller.stream_queue.get_nowait())
                assert review_chunks
                assert {c.payload['review_invocation_id'] for c in review_chunks} == {
                    r.request.invocation_id for r in reviews}
                result = {'pid': os.getpid(), 'phase': phase, 'provider': provider, 'status': task.status,
                    'voted': sorted(voted), 'reviewers': [r.request.reviewer for r in reviews],
                    'invocations': [r.request.invocation_id for r in reviews], 'source_core': __import__('openjiuwen').__file__, 'source_swarm': __import__('jiuwenswarm').__file__,
                    'closed': True, 'leader_session': leader_session, 'records': {r['reviewer']: r['invocation_id'] for r in records.values()}}
            finally:
                if scheduler is not None:
                    await scheduler.stop_reviewers()
                await db.close()
        return result

@pytest.mark.asyncio
@pytest.mark.parametrize('provider', ['codex', 'opencode'])
async def test_partial_votes_across_real_reviewer_host_processes(tmp_path, provider):
    seen = set()
    def next_action(body):
        rendered = json.dumps(body)
        role = next((name for name in ('reviewer_a', 'reviewer_b') if f'RESTART_PRIVATE_{name}' in rendered), 'leader')
        phase = 2 if 'RESTART_PHASE_2' in rendered else 1
        key = (role, phase)
        if key in seen:
            return None
        seen.add(key)
        return ('verify_task', {'task_id': 'work', 'decision': 'pass'}) if role == 'reviewer_a' or (role == 'reviewer_b' and phase == 2) else None
    class RestartModel(_ModelFixture):
        async def respond(self, request):
            action = next_action(await request.json())
            fixture = _ModelFixture()
            fixture.actions = [{'tool': 'jiuwenswarm_product_tools_' + action[0], 'args': action[1]}] if action else []
            response = await fixture.respond(request)
            self.requests.extend(fixture.requests)
            return response
    async with local_model(provider, opencode_model=RestartModel()) as (model, url):
        if provider == 'codex':
            def respond(body, index):
                action = next_action(body)
                if action:
                    return {'type': 'function_call', 'namespace': 'mcp__jiuwenswarm_product_tools',
                        'name': action[0], 'arguments': json.dumps(action[1]), 'id': f'fc_{index}', 'call_id': f'call_{index}'}
                return {'type': 'message', 'role': 'assistant', 'id': f'msg_{index}', 'status': 'completed',
                    'content': [{'type': 'output_text', 'text': 'REVIEW-END', 'annotations': []}]}
            model.responder = respond
        phases = []
        for phase in (1, 2):
            child = await asyncio.create_subprocess_exec(sys.executable, '-m', __name__, str(tmp_path), provider, str(phase), url,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            try:
                out, error = await asyncio.wait_for(child.communicate(), 100)
                (tmp_path / f'phase-{phase}.stdout').write_bytes(out)
                (tmp_path / f'phase-{phase}.stderr').write_bytes(error)
                assert child.returncode == 0, error.decode()[-15000:] + out.decode()[-15000:]
                result = json.loads(next(line[7:] for line in out.decode().splitlines() if line.startswith('RESULT:')))
                phases.append(result)
                evidence_dir = os.environ.get('TEAM_REVIEW_EVIDENCE_DIR')
                if evidence_dir:
                    destination = Path(evidence_dir)
                    destination.mkdir(parents=True, exist_ok=True)
                    (destination / f'{provider}-phase-{phase}.json').write_text(json.dumps(result, indent=2) + '\n')
                    (destination / f'{provider}-phase-{phase}.stdout.log').write_bytes(out)
                    (destination / f'{provider}-phase-{phase}.stderr.log').write_bytes(error)
                print('RESTART_EVIDENCE:' + json.dumps(result))
            finally:
                if child.returncode is None:
                    child.kill()
                    await child.wait()
        assert phases[0]['pid'] != phases[1]['pid']
        assert phases[0]['leader_session'] == phases[1]['leader_session']
        assert phases[0]['records']['reviewer_a'] == phases[1]['records']['reviewer_a']
        assert phases[0]['records']['reviewer_b'] != phases[1]['records']['reviewer_b']
        assert len(set(phases[0]['invocations'] + phases[1]['invocations'])) == 3


if __name__ == '__main__':
    result = asyncio.run(host_phase(Path(sys.argv[1]), sys.argv[2], int(sys.argv[3]), sys.argv[4]))
    print('RESULT:' + json.dumps(result))
