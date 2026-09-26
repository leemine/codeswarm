# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Goal durability shares the original encrypted parent archive and recovery gate."""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from openjiuwen.harness.goal import GoalStatus, SessionGoalStore
from openjiuwen.harness.goal.schema import GoalRecord
from openjiuwen.harness.goal.store import SESSION_GOAL_RECORD_KEY
from openjiuwen.harness.subagent_runtime.persistence import (
    merge_subagent_bucket,
    read_subagent_bucket,
)
from openjiuwen.harness_protocol import (
    HarnessEvent,
    TurnLifecycleEvent,
    TurnEventKind,
    TurnResult,
    TurnStatus,
    TurnUsage,
    CheckpointReason,
    HarnessCheckpoint,
    ResumePolicy,
)

from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.runtime.session.model import SessionWorkKind
from jiuwenswarm.runtime.harness.external_goal import ExternalGoalRuntime
from jiuwenswarm.runtime.harness.goal_evidence import (
    GoalAttemptEvidence,
    GoalAttemptIdentity,
)
from jiuwenswarm.runtime.harness.external_subagents import ExternalSubagentParentSession
from jiuwenswarm.runtime.harness.recovery_store import (
    ExecutionRecoveryUnavailableError,
    SessionExecutionRecovery,
)
from jiuwenswarm.runtime.harness.tool_gateway import ProductToolScope
from jiuwenswarm.server.runtime.agent_adapter.engine_adapter import EngineAgentAdapter
from tests.unit_tests.runtime.harness.test_execution_recovery import (
    _card,
    recovery_env as _recovery_env,
)
from tests.unit_tests.runtime.harness.test_external_execution_route import _route
from tests.unit_tests.runtime.harness.test_external_goal_runtime import (
    chain as _chain,
    request,
    run,
)


recovery_env = _recovery_env
chain = _chain

_ATTEMPT_KEY = "harness.goal.external_attempt"


def recovery_for(route, *, profile="codex-profile"):
    return SessionExecutionRecovery(
        session_id=route.bound.binding.host_session_id,
        execution_profile_id=profile,
        binding=route.bound.binding,
        runtime_paths=route.runtime_paths,
    )


def parent_for(recovery):
    return ExternalSubagentParentSession(
        "session-1", write_output=AsyncMock(), recovery=recovery
    )


def goal_runtime(parent, route):
    binding = route.bound.binding
    return ExternalGoalRuntime(
        SimpleNamespace(),
        parent,
        ProductToolScope(
            binding.subject_id,
            binding.host_session_id,
            binding.workspace,
        ),
    )


@pytest.mark.asyncio
async def test_goal_and_original_subagent_registry_share_one_parent_archive(
    tmp_path, recovery_env
):
    route = _route(tmp_path)
    recovery = recovery_for(route)
    parent = parent_for(recovery)
    runtime = goal_runtime(parent, route)
    merge_subagent_bucket(
        parent, {"records": {"child": {"display_name": "durable child"}}, "revision": 1}
    )
    parent.update_state({"product.recovery.sentinel": {"retained": True}})
    goal = await runtime.manager.set(
        "durable private objective", token_budget=50, max_attempts=3
    )
    merge_subagent_bucket(
        parent, {"turns": {"child": [{"turn_id": "child-turn"}]}, "revision": 2}
    )
    await runtime.manager.pause()
    assert SessionGoalStore(parent).load().goal_id == goal.goal_id
    assert "durable private objective" not in recovery.path.read_text()
    assert "durable child" not in recovery.path.read_text()

    restored_parent = parent_for(recovery_for(route))
    restored = goal_runtime(restored_parent, route)
    loaded = await restored.manager.get()
    assert loaded.status is GoalStatus.PAUSED
    assert loaded.token_budget == 50
    assert (
        read_subagent_bucket(restored_parent)["turns"]["child"][0]["turn_id"]
        == "child-turn"
    )
    await restored.manager.clear()
    after_clear = parent_for(recovery_for(route))
    assert SessionGoalStore(after_clear).load() is None
    assert (
        read_subagent_bucket(after_clear)["records"]["child"]["display_name"]
        == "durable child"
    )
    assert after_clear.get_state("product.recovery.sentinel") == {"retained": True}


@pytest.mark.asyncio
async def test_goal_mutations_preserve_original_provider_checkpoint(
    tmp_path, recovery_env
):
    route = _route(tmp_path)
    recovery = recovery_for(route)
    recovery.prepare(_card(), agent_id="external:codex:session-1")
    checkpoint = HarnessCheckpoint(
        provider="codex",
        schema_version="1",
        agent_id="external:codex:session-1",
        host_session_id="session-1",
        checkpoint_id="goal-coexist",
        sequence=1,
        provider_session_id="original-thread",
        data={"thread_id": "original-thread"},
    )
    receipt = await recovery.save(checkpoint, reason=CheckpointReason.TURN_COMPLETED)
    parent = parent_for(recovery)
    runtime = goal_runtime(parent, route)
    await runtime.manager.set("preserve checkpoint")
    await runtime.manager.clear()
    recovered = recovery_for(route).prepare(
        _card(), agent_id="external:codex:session-1"
    )
    assert recovered.resume_policy is ResumePolicy.REQUIRE_RESUME
    assert recovered.checkpoint == replace(
        checkpoint, revision=receipt.storage_revision
    )


def test_legacy_goal_archive_without_new_fields_is_readable(tmp_path, recovery_env):
    route = _route(tmp_path)
    parent = parent_for(recovery_for(route))
    record = GoalRecord.create(session_id="session-1", objective="legacy objective")
    record.status = GoalStatus.PAUSED
    record.attempt_count = 2
    record.token_usage.accumulate(9, 3, 0)
    archive = record.to_dict()
    for key in ("last_assessed_attempt", "time_used_seconds", "active_started_at"):
        archive.pop(key, None)
    parent.update_state({SESSION_GOAL_RECORD_KEY: archive})
    loaded = SessionGoalStore(parent_for(recovery_for(route))).load()
    assert loaded.objective == "legacy objective"
    assert loaded.status is GoalStatus.PAUSED
    assert loaded.attempt_count == 2
    assert loaded.last_assessed_attempt == 0
    assert loaded.token_usage.total_tokens == 12


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [GoalStatus.ACTIVE, GoalStatus.PAUSED])
@pytest.mark.parametrize(
    "marker",
    [
        None,
        {},
        {"goal_id": "old", "turn_id": None},
        {"goal_id": "old", "turn_id": "unknown-turn"},
    ],
)
async def test_unknown_cold_attempt_never_resends_from_resume(
    chain, recovery_env, marker, status
):
    route = chain.adapter.route
    recovery = recovery_for(route)
    parent = parent_for(recovery)
    record = GoalRecord.create(
        session_id="session-1", objective="unknown accepted input"
    )
    record.status = status
    record.attempt_count = 2
    record.last_assessed_attempt = 1
    SessionGoalStore(parent).save(record)
    parent.update_state({_ATTEMPT_KEY: marker})
    adapter = EngineAgentAdapter(replace(route, recovery=recovery_for(route)))
    await adapter.create_instance()
    try:
        recovered = SimpleNamespace(adapter=adapter, runtime=chain.runtime)
        chunks = await run(recovered, request("cold-resume", action="resume"))
        assert any(
            chunk.payload.get("code") == "goal_recovery_unconfirmed" for chunk in chunks
        ), chunks
        assert adapter._goal_runtime.manager.peek().attempt_count == 2
        assert all(not provider.sent for provider in chain.providers)
        assert adapter._parent_session is adapter._subagent_runtime.parent_session
    finally:
        await adapter.cleanup()


@pytest.mark.asyncio
async def test_settled_cold_record_is_snapshot_only_and_does_not_repeat_attempt(
    chain, recovery_env
):
    route = chain.adapter.route
    recovery = recovery_for(route)
    parent = parent_for(recovery)
    record = GoalRecord.create(session_id="session-1", objective="already complete")
    record.status = GoalStatus.COMPLETED
    record.attempt_count = record.last_assessed_attempt = 2
    record.token_usage.accumulate(10, 4, 0)
    SessionGoalStore(parent).save(record)
    adapter = EngineAgentAdapter(replace(route, recovery=recovery_for(route)))
    await adapter.create_instance()
    try:
        attach = request("completed-attach")
        attach.req_method = ReqMethod.CHAT_SEND
        attach.params = {"attach_goal": True, "mode": "agent"}
        chunks = await run(
            SimpleNamespace(adapter=adapter, runtime=chain.runtime),
            attach,
            SessionWorkKind.GOAL_ATTACH,
        )
        assert any(
            chunk.payload.get("event_type") == "goal.snapshot" for chunk in chunks
        ), chunks
        restored = adapter._goal_runtime.manager.peek()
        assert restored.status is GoalStatus.COMPLETED
        assert restored.attempt_count == restored.last_assessed_attempt == 2
        assert restored.token_usage.total_tokens == 14
        assert all(not provider.sent for provider in chain.providers)
    finally:
        await adapter.cleanup()


@pytest.mark.parametrize("mismatch", ["subject", "profile", "fingerprint"])
def test_wrong_recovery_scope_cannot_read_or_replace_goal_archive(
    tmp_path, recovery_env, mismatch
):
    route = _route(tmp_path)
    recovery = recovery_for(route)
    parent = parent_for(recovery)
    record = GoalRecord.create(session_id="session-1", objective="bound objective")
    SessionGoalStore(parent).save(record)
    wrong = (
        _route(tmp_path, subject_id="another-subject")
        if mismatch == "subject"
        else _route(tmp_path, revision="different-config")
        if mismatch == "fingerprint"
        else route
    )
    with pytest.raises(ExecutionRecoveryUnavailableError):
        recovery_for(
            wrong,
            profile="another-profile" if mismatch == "profile" else "codex-profile",
        )
    assert (
        SessionGoalStore(parent_for(recovery_for(route))).load().goal_id
        == record.goal_id
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["finished", "exit_confirmed"])
async def test_confirmed_prior_attempt_allows_explicit_control_resume_without_auto_send(
    tmp_path, recovery_env, boundary
):
    route = _route(tmp_path)
    recovery = recovery_for(route)
    parent = parent_for(recovery)
    record = GoalRecord.create(
        session_id="session-1", objective="confirmed previous turn"
    )
    record.status = GoalStatus.PAUSED
    record.attempt_count = 2
    record.last_assessed_attempt = 1
    SessionGoalStore(parent).save(record)
    parent.update_state(
        {
            _ATTEMPT_KEY: {
                "goal_id": record.goal_id,
                "revision": record.revision,
                "attempt_index": 2,
                "safe_boundary": boundary,
            }
        }
    )
    runtime = goal_runtime(parent_for(recovery_for(route)), route)
    assert not runtime.cold_unconfirmed
    result = await runtime.control({"action": "resume"})
    assert result["result_type"] == "goal_stream"
    assert result["goal"]["attempt_count"] == 2
    assert result["goal"]["revision"] == record.revision + 1
    assert runtime.owner is None


@pytest.mark.parametrize("field", ["goal_id", "revision", "attempt_index"])
def test_old_boundary_for_another_attempt_cannot_authorize_cold_replay(
    tmp_path, recovery_env, field
):
    route = _route(tmp_path)
    parent = parent_for(recovery_for(route))
    record = GoalRecord.create(session_id="session-1", objective="unknown next turn")
    record.attempt_count = 2
    record.last_assessed_attempt = 1
    SessionGoalStore(parent).save(record)
    marker = {
        "goal_id": record.goal_id,
        "revision": record.revision,
        "attempt_index": 2,
        "safe_boundary": "finished",
    }
    marker[field] = "different-goal" if field == "goal_id" else marker[field] + 1
    parent.update_state({_ATTEMPT_KEY: marker})
    assert goal_runtime(parent_for(recovery_for(route)), route).cold_unconfirmed


@pytest.mark.asyncio
async def test_pending_interaction_retains_goal_snapshot_and_original_recovery_gate(
    tmp_path, recovery_env
):
    route = _route(tmp_path)
    recovery = recovery_for(route)
    recovery.prepare(_card(), agent_id="external:codex:session-1")
    checkpoint = HarnessCheckpoint(
        provider="codex",
        schema_version="1",
        agent_id="external:codex:session-1",
        host_session_id="session-1",
        checkpoint_id="waiting",
        sequence=1,
        data={"thread_id": "same-thread"},
    )
    await recovery.save(checkpoint, reason=CheckpointReason.TURN_COMPLETED)
    record = GoalRecord.create(session_id="session-1", objective="awaiting permission")
    SessionGoalStore(parent_for(recovery)).save(record)
    await recovery.mark_pending_interaction("permission", turn_id="unconfirmed-turn")
    restored = recovery_for(route)
    assert SessionGoalStore(parent_for(restored)).load().goal_id == record.goal_id
    with pytest.raises(ExecutionRecoveryUnavailableError, match="interaction"):
        restored.prepare(_card(), agent_id="external:codex:session-1")
    await restored.clear_pending_interactions()
    assert (
        restored.prepare(
            _card(), agent_id="external:codex:session-1"
        ).checkpoint.checkpoint_id
        == "waiting"
    )


@pytest.mark.asyncio
async def test_truncated_history_refuses_cold_replay_without_changing_goal(
    tmp_path, recovery_env
):
    route = _route(tmp_path)
    recovery = recovery_for(route)
    recovery.prepare(_card(), agent_id="external:codex:session-1")
    checkpoint = HarnessCheckpoint(
        provider="codex",
        schema_version="1",
        agent_id="external:codex:session-1",
        host_session_id="session-1",
        checkpoint_id="before-history",
        sequence=1,
        data={"thread_id": "same-thread"},
    )
    history = recovery_env / "session-1" / "history.jsonl"
    history.write_text('{"delivery_id":"before-checkpoint"}\n')
    await recovery.save(checkpoint, reason=CheckpointReason.TURN_COMPLETED)
    record = GoalRecord.create(
        session_id="session-1", objective="preserve replay position"
    )
    SessionGoalStore(parent_for(recovery)).save(record)
    history.write_text("")
    restored = recovery_for(route)
    with pytest.raises(ExecutionRecoveryUnavailableError):
        restored.prepare(_card(), agent_id="external:codex:session-1")
    assert SessionGoalStore(parent_for(restored)).load().goal_id == record.goal_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "persistent", [False, True], ids=["one-write-fails", "all-writes-fail"]
)
async def test_usage_persistence_failure_stays_visible_and_never_reopens_execution(
    chain, recovery_env, monkeypatch, persistent
):
    route = chain.adapter.route
    recovery = recovery_for(route)
    adapter = EngineAgentAdapter(replace(route, recovery=recovery))
    await adapter.create_instance()
    runtime = adapter._goal_runtime
    parent = adapter._parent_session
    original_save = recovery.save_host_state
    try:
        goal = await runtime.manager.set("usage must be durable")
        goal = await runtime.driver.begin(
            goal_id=goal.goal_id, revision=goal.revision, attempt_index=1
        )
        marker = {
            "goal_id": goal.goal_id,
            "revision": goal.revision,
            "attempt_index": 1,
            "turn_id": "accepted-turn",
        }
        parent.update_state({_ATTEMPT_KEY: marker})
        identity = GoalAttemptIdentity(
            goal.goal_id, goal.revision, 1, "original-owner", 7
        )
        evidence = GoalAttemptEvidence(
            identity, host_session_id="session-1", agent_id="agent"
        )
        evidence.bind_turn("accepted-turn")
        evidence.observe(
            HarnessEvent(
                1,
                0.0,
                TurnLifecycleEvent(
                    TurnEventKind.FINISHED,
                    TurnResult(
                        TurnStatus.COMPLETED,
                        usage=TurnUsage(input_tokens=3, output_tokens=2),
                    ),
                ),
                "session-1",
                "agent",
                turn_id="accepted-turn",
            ),
            generation=7,
        )
        runtime.attempt = evidence
        writes = 0

        def fail_save(state):
            nonlocal writes
            writes += 1
            if persistent or writes == 1:
                raise OSError("test archive write failed")
            return original_save(state)

        monkeypatch.setattr(recovery, "save_host_state", fail_save)
        with pytest.raises(OSError, match="archive write failed"):
            await runtime._account_provider_usage()
        assert evidence.take_usage_delta() is None
        assert parent.state_write_failed
        assert runtime.manager.peek().token_usage.total_tokens == 0
        assert (
            SessionGoalStore(parent_for(recovery_for(route)))
            .load()
            .token_usage.total_tokens
            == 0
        )
        assert parent.get_state(_ATTEMPT_KEY) == marker
        # A later unrelated success must not silently reset the failed receipt.
        if persistent:
            with pytest.raises(OSError):
                parent.update_state({"unrelated": "later write"})
        else:
            parent.update_state({"unrelated": "later write"})
        assert parent.state_write_failed
        runtime.attempt = None
        resume = await runtime.control({"action": "resume"})
        assert resume["error_code"] == "goal_recovery_unconfirmed"
        attach = request("after-failed-write")
        attach.req_method = ReqMethod.CHAT_SEND
        attach.params = {"attach_goal": True, "mode": "agent"}
        chunks = await run(
            SimpleNamespace(adapter=adapter, runtime=chain.runtime),
            attach,
            SessionWorkKind.GOAL_ATTACH,
        )
        assert any(
            chunk.payload.get("event_type") == "goal.snapshot" for chunk in chunks
        )
        assert all(not provider.sent for provider in chain.providers)
        assert "safe_boundary" not in parent.get_state(_ATTEMPT_KEY)
        assert goal_runtime(parent_for(recovery_for(route)), route).cold_unconfirmed
    finally:
        runtime.attempt = None
        monkeypatch.setattr(recovery, "save_host_state", original_save)
        await adapter.cleanup()
