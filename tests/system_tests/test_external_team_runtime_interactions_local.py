"""Real CLI approval through AgentRuntime, Team facade, Runner and scheduler.

Admission/config selection and assessor use local fixtures; no Web/TUI transport.
"""
import asyncio
from copy import copy
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from openjiuwen.harness.goal import GoalStatus
from openjiuwen.agent_teams.external.interaction_address import decode_interaction_address, encode_interaction_address
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.runtime.interaction import InteractionAnswerInput
from jiuwenswarm.runtime.service import AgentRuntime
from jiuwenswarm.runtime.session import SessionWorkKind
from jiuwenswarm.server.runtime.agent_adapter.interface import JiuWenSwarm
from jiuwenswarm.server.runtime.agent_adapter.team_engine_adapter import ExternalTeamAgentAdapter
from tests.system_tests.test_external_team_goal_local import test_real_team_goal_two_attempts_and_durable_history as _run_goal
from tests.unit_tests.runtime.harness.test_execution_recovery import recovery_env as recovery_env

pytestmark = [pytest.mark.integration, pytest.mark.system,
    pytest.mark.skipif(os.environ.get('RUN_TEAM_PROVIDER_LOCAL') != '1', reason='real Team Runtime CLI is opt-in')]


class RuntimeProbe:
    def __init__(self, decision):
        self.decision = decision
        self.queue = asyncio.Queue()
        self.answers = []
        self.review_questions = []
        self.runtime = None

    def configure(self, provider, settings, root, codex_home):
        if provider != 'codex':
            return
        import openai_codex as sdk
        binary = sdk.client._resolve_codex_bin(sdk.CodexConfig())
        readable = {':minimal': 'read', str(root): 'write', str(codex_home / 'tmp'): 'read', str(Path(binary).parent): 'read'}
        (codex_home / 'config.toml').write_text(
            'default_permissions = "team-read"\n[permissions.team-read.filesystem]\n'
            + '\n'.join(f'{json.dumps(p)} = {json.dumps(a)}' for p, a in readable.items())
            + '\n[permissions.team-read.network]\nenabled=false\n')
        settings['mcp_default_tools_approval_mode'] = 'prompt'

    def bind(self, coordinator, adapter):
        facade = object.__new__(JiuWenSwarm)
        facade._runtime_execution_route = adapter.route
        facade._ensure_adapter = lambda **kwargs: adapter
        registry = SimpleNamespace(
            get_agent_for_session_nowait=lambda ch, sid: facade if ch == adapter.route.channel_id and sid == 'team-local' else None,
            cleanup=AsyncMock(), cancel_all_inflight_work=AsyncMock(), cleanup_session_runtime=AsyncMock(return_value=True))
        self.runtime = AgentRuntime(agent_manager=registry, initializer=AsyncMock(), session_coordinator=coordinator)
        return self.runtime

    def observe(self, chunk):
        payload = chunk.payload or {}
        if payload.get('event_type') == 'chat.ask_user_question':
            self.queue.put_nowait(dict(payload))

    async def run(self, coordinator, produce, adapter, request, scenario):
        async def answer_questions():
            while True:
                payload = await self.queue.get()
                address = decode_interaction_address(payload['request_id'])
                assert address and address[0] == 'team' and address[2] == 'team-local'
                reviewer = address[1].startswith('review:')
                if reviewer:
                    self.review_questions.append(payload)
                answer = InteractionAnswerInput(request_id='answer-' + str(len(self.answers)),
                    channel_id=adapter.route.channel_id, session_id='team-local', interaction_id=payload['request_id'],
                    source=payload['source'], mode='team.code.normal', work_mode='code',
                    answers=({'selected_options': ['allow_once']},), session_generation=payload['session_generation']).to_agent_request()
                assert answer.req_method is ReqMethod.CHAT_SEND
                # Product channels also use CHAT_ANSWER; cover both original
                # wire forms while retaining the same pending owner.
                if len(self.answers) % 2:
                    answer.req_method = ReqMethod.CHAT_ANSWER
                stale = copy(answer)
                stale.params = {**answer.params, 'session_generation': payload['session_generation'] + 1}
                with pytest.raises(RuntimeError, match='generation'):
                    _ = [e async for e in self.runtime.stream(stale)]
                wrong = list(address); wrong[1] += '-other'
                stale.params = {**answer.params, 'request_id': encode_interaction_address(*wrong)}
                with pytest.raises(RuntimeError):
                    _ = [e async for e in self.runtime.stream(stale)]
                if reviewer and self.decision == 'cancel':
                    scenario.reviewed.set()  # release only the local model fixture's wait
                    assert await adapter.cancel_active_goal()
                    return
                events = [e async for e in self.runtime.stream(answer)]
                assert any(e.payload.get('event_type') == 'runtime.accepted' and e.payload.get('resolved') for e in events)
                self.answers.append(answer)
                duplicate = [e async for e in self.runtime.stream(answer)]
                assert len(duplicate) == 1 and duplicate[0].payload.get('duplicate') is True
        task = asyncio.create_task(coordinator.run_unary('team-local', 'root-goal', SessionWorkKind.GOAL_STREAM, produce))
        controls = asyncio.create_task(answer_questions())
        try:
            done, _ = await asyncio.wait({task, controls}, return_when=asyncio.FIRST_COMPLETED)
            if controls in done:
                await controls  # surface a failed control immediately
            if self.decision == 'cancel':
                with pytest.raises(asyncio.CancelledError):
                    await task
                goal = adapter._goal_runtime.manager.peek()
                assert goal.status is GoalStatus.PAUSED and goal.attempt_count == 1
                assert self.review_questions
                await adapter.complete_request_history(request)
                assert adapter._goal_runtime.owner is None
                restored = ExternalTeamAgentAdapter(adapter.route)
                await restored.create_instance()
                assert restored._goal_runtime.manager.peek().to_dict() == goal.to_dict()
                # The original reviewer ledger blocks cancelled invocation replay.
                from openjiuwen.core.session.agent_team import create_agent_team_session
                from jiuwenswarm.runtime.harness.team_review import validate_review_recovery
                # Runner's original helper uses the default Team checkpoint
                # namespace, independent of the product Team's display name.
                session = create_agent_team_session(session_id='team-local')
                await session.pre_run()
                assert session.get_state('external_team_reviews')
                with pytest.raises(ValueError, match='unconfirmed'):
                    validate_review_recovery(session)
                return True
            await task
            if hasattr(self, 'settle_failure'):
                await self.settle_failure(adapter, request)
                return True
            assert len(self.review_questions) == 1
            # Changing a previously accepted answer cannot create a second Turn.
            late = copy(self.answers[-1]); late.params = {**late.params, 'answers': [{'selected_options': ['reject']}]}
            with pytest.raises(RuntimeError):
                _ = [e async for e in self.runtime.stream(late)]
            return False
        finally:
            for pending in (controls, task):
                if not pending.done():
                    pending.cancel()
            await asyncio.gather(controls, task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('provider', ['codex', 'opencode'])
@pytest.mark.parametrize('decision', ['allow', 'cancel'])
async def test_real_runtime_reviewer_control_and_goal_settlement(tmp_path, monkeypatch, recovery_env, provider, decision):
    probe = RuntimeProbe(decision)
    try:
        await _run_goal(tmp_path, monkeypatch, provider, recovery_env, 'scheduled', probe=probe)
    finally:
        if probe.runtime is not None:
            await probe.runtime.close()
