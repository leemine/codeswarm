# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Original Runner and Team state across root Goal attempts.

Provider decisions/checkpoints and assessor are scripted. Reviewers are seeded
through the original task manager, not assigned by the scheduled dispatcher.
"""

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from jiuwenswarm.server.runtime.agent_adapter.team_engine_adapter import (
    ExternalTeamAgentAdapter,
)
from openjiuwen.harness.goal import GoalStatus
from openjiuwen.harness_protocol import TurnUsage
from tests.unit_tests.runtime.harness.test_execution_recovery import (
    recovery_env as recovery_env,
)
from tests.unit_tests.runtime.harness.test_team_execution import host as host
from tests.unit_tests.runtime.harness.test_team_goal_runtime import chain as chain
from tests.unit_tests.runtime.harness.test_team_goal_runtime import run


@pytest.mark.asyncio
@pytest.mark.parametrize("checkpoint_available", [True, False])
async def test_original_three_member_task_review_survives_goal_attempt_rebuild(
    chain, monkeypatch, tmp_path, checkpoint_available
):
    """Real Runner/Team tools and SQLite; only Provider decisions and assessment are scripted."""
    import json

    from jiuwenswarm.runtime.harness import team_execution
    from jiuwenswarm.server.runtime.agent_adapter import team_helpers
    from openjiuwen.agent_teams.schema.blueprint import StorageSpec
    from openjiuwen.agent_teams.schema.task import TaskGraphSpec
    from openjiuwen.agent_teams.schema.team import TeamMemberSpec
    from openjiuwen.core.runner import Runner
    from openjiuwen.harness_protocol import (
        CheckpointReason,
        HarnessCheckpoint,
        HarnessState,
        ResumePolicy,
        ToolInvocation,
    )
    from tests.unit_tests.runtime.harness.test_team_execution import spec_for

    adapter = chain.adapter
    monkeypatch.setattr(
        adapter,
        "process_goal_round",
        ExternalTeamAgentAdapter.process_goal_round.__get__(adapter),
    )
    monkeypatch.setattr(
        adapter,
        "confirm_goal_round_exit",
        ExternalTeamAgentAdapter.confirm_goal_round_exit.__get__(adapter),
    )
    monkeypatch.setattr(team_helpers, "get_team_manager", lambda _: chain.manager)
    monkeypatch.setattr(
        team_helpers, "_persist_team_file_monitor_roots", lambda *args: None
    )

    def spec(**kwargs):
        value = spec_for(chain.host)
        value.storage = StorageSpec(
            type="sqlite", params={"connection_string": str(tmp_path / "team.db")}
        )
        value.predefined_members = [
            TeamMemberSpec(member_name=name, display_name=name)
            for name in ("worker", "reviewer")
        ]
        return value

    monkeypatch.setattr(
        chain.manager, "get_swarm_enriched_team_spec", AsyncMock(side_effect=spec)
    )
    backends, gateways, databases, actions, turns, assessments = {}, {}, [], [], [], []
    seeded, finished_work, reviewed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    build_member = team_execution.ExternalTeamMemberFactory.build_member_runtime

    def member(factory, request):
        backends[request.context.member_name] = request.team_backend
        databases.append(request.team_backend.db)
        return build_member(factory, request)

    monkeypatch.setattr(
        team_execution.ExternalTeamMemberFactory, "build_member_runtime", member
    )
    products = team_execution.ExternalSubagentRuntime

    def product(*args, **kwargs):
        value = products(*args, **kwargs)
        gateways[kwargs["invoke_kwargs"]["member_name"]] = value.gateway
        return value

    monkeypatch.setattr(team_execution, "ExternalSubagentRuntime", product)

    async def call(name, tool, args):
        result = await gateways[name].invoke(
            ToolInvocation(str(len(actions)), tool, args)
        )
        actions.append((name, tool, result))
        assert not result.is_error, result.content
        return result

    create_engine = team_execution.create_harness_engine

    def engine(*args, **kwargs):
        value = create_engine(*args, **kwargs)
        execute = value.harness._execute_turn

        async def scripted(turn):
            name = value.harness.contexts[-1].agent_name
            turns.append(name)
            backend = backends[name]
            if name == "team_leader":
                if not seeded.is_set():
                    await call(
                        name,
                        "build_team",
                        {
                            "display_name": "Test",
                            "team_desc": "Review test",
                            "leader_display_name": "Lead",
                            "leader_desc": "Coordinate",
                        },
                    )
                    result = await backend.task_manager.add_graph(
                        [
                            TaskGraphSpec(
                                task_id="reviewed",
                                title="Work",
                                content="Verify ownership",
                                assignee="worker",
                                reviewer=("reviewer",),
                            )
                        ]
                    )
                    assert result.ok
                    seeded.set()
                    for recipient in ("worker", "reviewer"):
                        await call(
                            name,
                            "send_message",
                            {"to": recipient, "content": "Process reviewed task"},
                        )
                await asyncio.wait_for(reviewed.wait(), 15)
                assert (
                    await backend.task_manager.get("reviewed")
                ).status == "completed"
                await call(name, "view_task", {"action": "list"})
            elif name == "worker" and not finished_work.is_set():
                await asyncio.wait_for(seeded.wait(), 15)
                await call(
                    name, "claim_task", {"task_id": "reviewed", "status": "claimed"}
                )
                await call(
                    name, "claim_task", {"task_id": "reviewed", "status": "completed"}
                )
                assert (
                    await backend.task_manager.get("reviewed")
                ).status == "in_review"
                forged = await gateways[name].invoke(
                    ToolInvocation(
                        "forged",
                        "verify_task",
                        {
                            "task_id": "reviewed",
                            "decision": "pass",
                            "member_name": "reviewer",
                        },
                    )
                )
                assert forged.is_error
                finished_work.set()
            elif name == "reviewer" and not reviewed.is_set():
                await asyncio.wait_for(finished_work.wait(), 15)
                await call(
                    name, "verify_task", {"task_id": "reviewed", "decision": "pass"}
                )
                reviewed.set()
            context = value.harness.contexts[-1]
            if checkpoint_available:
                checkpoint = HarnessCheckpoint(
                    provider=chain.host.provider,
                    schema_version="test",
                    agent_id=context.agent_id,
                    host_session_id=context.host_session_id,
                    checkpoint_id=f"saved-{len(turns)}",
                    sequence=len(turns),
                    provider_session_id="provider-" + context.agent_id,
                    data={},
                )
                await context.checkpoint_sink.save(
                    checkpoint, reason=CheckpointReason.TURN_COMPLETED
                )
            kind, result = await execute(turn)
            return kind, replace(
                result, usage=TurnUsage(input_tokens=5, output_tokens=1)
            )

        async def checked(turn):
            try:
                return await scripted(turn)
            except Exception as exc:
                errors.append(repr(exc))
                raise

        value.harness._execute_turn = checked
        return value

    monkeypatch.setattr(team_execution, "create_harness_engine", engine)

    class Assessor:
        async def invoke(self, messages, **kwargs):
            assert kwargs["tools"] == []
            assert all(
                e.harness.state is HarnessState.TERMINATED for e in chain.host.engines
            ), [e.harness.state for e in chain.host.engines]
            evidence = adapter._goal_runtime.attempt
            assert evidence.ready_for_assessment
            task = await backends["team_leader"].task_manager.get("reviewed")
            assert task.status == "completed" and task.assignee == "worker"
            assert json.loads(task.reviewer) == ["reviewer"]
            assert task.review_round == 1
            members = await backends["team_leader"].db.member.get_team_members("team")
            assert {m.member_name for m in members} == {
                "team_leader",
                "worker",
                "reviewer",
            }
            assert len(members) == 3
            assessments.append(evidence)
            return SimpleNamespace(
                content=json.dumps(
                    {
                        "status": "continue" if len(assessments) == 1 else "complete",
                        "evidence": "Task and original reviewer receipt preserved",
                        "next_instruction": "Recheck the completed task",
                    }
                ),
                usage_metadata={"input_tokens": 3, "output_tokens": 1},
            )

    errors = []

    class RecordedAssessor(Assessor):
        async def invoke(self, *args, **kwargs):
            try:
                return await super().invoke(*args, **kwargs)
            except Exception as exc:
                errors.append(repr(exc))
                raise

    adapter.set_goal_assessor_factory(lambda: RecordedAssessor())
    from openjiuwen.agent_teams.context import reset_session_id, set_session_id

    token = set_session_id("session")
    await Runner.start()
    try:
        async with asyncio.timeout(45):
            await run(chain)
        assert not errors, errors
        goal = adapter._goal_runtime.manager.peek()
        assert goal.attempt_count == 2
        if checkpoint_available:
            assert goal.status is GoalStatus.COMPLETED, goal.to_dict()
            assert len(assessments) == 2
            resumed = [
                context
                for engine in chain.host.engines
                for context in engine.harness.contexts
                if context.resume_policy is ResumePolicy.REQUIRE_RESUME
            ]
            assert resumed and all(
                context.checkpoint is not None for context in resumed
            )
        else:
            assert goal.status is GoalStatus.BLOCKED
            assert len(assessments) == 1
            assert adapter._goal_runtime.accounting_unknown
            assert not any(
                chunk.runtime_completion == "completed" for chunk in chain.chunks
            )
        assert goal.token_usage.total_tokens == len(turns) * 6 + len(assessments) * 4
        assert {"team_leader", "worker", "reviewer"} <= set(turns)
        assert sum(tool == "build_team" for _, tool, _ in actions) == 1
        assert sum(tool == "verify_task" for _, tool, _ in actions) == 1
        assert sum(tool == "claim_task" for _, tool, _ in actions) == 2
        assert all(
            e.harness.readers == 1 for e in chain.host.engines if e.harness.contexts
        )
        assert not chain.manager.is_round_active("session")
    finally:
        await chain.manager.stop_session_runtime(
            "session", require_exit_confirmation=True
        )
        await Runner.stop()
        for db in set(databases):
            await db.close()
        reset_session_id(token)
