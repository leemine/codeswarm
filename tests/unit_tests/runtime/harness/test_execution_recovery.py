# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""R1-04E durable External Session recovery contracts."""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path

import portalocker
import pytest

from openjiuwen.core.session import InteractionOutput
from openjiuwen.core.session.stream.base import OutputSchema
from openjiuwen.harness.engine import ExecutionBinding, HarnessEngine
from openjiuwen.harness_protocol import (
    AgentExecutionSpec,
    CheckpointConflictError,
    CheckpointReason,
    HarnessCapability,
    HarnessCard,
    HarnessCheckpoint,
    HarnessContext,
    HarnessState,
    ResumePolicy,
    TurnEventKind,
)

from jiuwenswarm.common.auth import session_store
from jiuwenswarm.common.runtime_workspace import RuntimeWorkspacePaths
from jiuwenswarm.runtime.harness import recovery_store
from jiuwenswarm.runtime.harness.recovery_store import (
    ExecutionRecoveryUnavailableError,
    SessionExecutionRecovery,
)
from jiuwenswarm.runtime.harness.execution_session import ExecutionSession
from openjiuwen.harness_providers.io_adapter import ProjectedOutput


def _paths(tmp_path: Path) -> RuntimeWorkspacePaths:
    workspace = (tmp_path / "workspace").resolve()
    workspace.mkdir(exist_ok=True)
    return RuntimeWorkspacePaths(
        internal_workspace_dir=workspace,
        runtime_workspace_root=workspace,
        cwd=workspace,
        project_root=workspace,
    )


def _binding(tmp_path: Path, *, subject_id: str = "alice") -> ExecutionBinding:
    return ExecutionBinding.create(
        AgentExecutionSpec("codex", "r1", provider_config={"api_key": "not-persisted"}),
        subject_id=subject_id,
        host_session_id="session-1",
        workspace=str(_paths(tmp_path).runtime_workspace_root),
    )


def _card(*, version: str = "1.0") -> HarnessCard:
    return HarnessCard(
        name="codex",
        implementation_version=version,
        capabilities=frozenset({HarnessCapability.CHECKPOINT}),
    )


@pytest.fixture
def recovery_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    sessions = tmp_path / "sessions"
    auth = tmp_path / "auth"

    def resolve(session_id: str, create: bool = False):
        path = sessions / session_id
        if create:
            path.mkdir(parents=True, exist_ok=True)
        return path, None

    monkeypatch.setattr(recovery_store, "resolve_session_dir", resolve)
    monkeypatch.setattr(
        recovery_store,
        "get_read_history_path",
        lambda session_id: sessions / session_id / "history.jsonl",
    )
    def resolve_subagent(parent_id: str, subagent_id: str, create: bool = False):
        path = sessions / parent_id / "subagents" / subagent_id / "history.jsonl"
        if create:
            path.parent.mkdir(parents=True, exist_ok=True)
        return path, None

    monkeypatch.setattr(
        recovery_store,
        "resolve_subagent_history_path",
        resolve_subagent,
    )
    def auth_dir() -> Path:
        auth.mkdir(parents=True, exist_ok=True)
        return auth

    monkeypatch.setattr(session_store, "auth_dir", auth_dir)
    return sessions


def test_archive_transactions_use_a_private_cross_process_lock(
    tmp_path: Path,
    recovery_env: Path,
) -> None:
    del recovery_env
    recovery = SessionExecutionRecovery(
        session_id="session-1",
        execution_profile_id="codex-profile",
        binding=_binding(tmp_path),
        runtime_paths=_paths(tmp_path),
    )
    lock_path = recovery.path.with_name(f".{recovery.path.name}.lock")
    started = threading.Event()
    finished = threading.Event()

    def update() -> None:
        started.set()
        recovery.save_host_state({"revision": 1})
        finished.set()

    thread = threading.Thread(target=update)
    with portalocker.Lock(str(lock_path), mode="a", timeout=1):
        thread.start()
        assert started.wait(timeout=1)
        assert not finished.wait(timeout=0.1)
    thread.join(timeout=2)

    assert not thread.is_alive()
    assert finished.is_set()
    assert lock_path.stat().st_mode & 0o777 == 0o600
    assert recovery.load_host_state() == {"revision": 1}


@pytest.mark.asyncio
async def test_checkpoint_is_encrypted_and_cold_restore_reuses_exact_scope(
    tmp_path: Path,
    recovery_env: Path,
) -> None:
    binding = _binding(tmp_path)
    paths = _paths(tmp_path)
    recovery = SessionExecutionRecovery(
        session_id="session-1",
        execution_profile_id="codex-profile",
        binding=binding,
        runtime_paths=paths,
    )
    first = recovery.prepare(_card(), agent_id="external:codex:session-1")
    assert first.resume_policy is ResumePolicy.NEW
    assert first.checkpoint is None
    recovery.save_host_state(
        {"subagents": {"records": {"child": {"role": "secret-role"}}}}
    )
    assert "secret-role" not in recovery.path.read_text(encoding="utf-8")
    concurrent_admission = SessionExecutionRecovery(
        session_id="session-1",
        execution_profile_id="codex-profile",
        binding=binding,
        runtime_paths=paths,
    ).prepare(_card(), agent_id="external:codex:session-1")
    assert concurrent_admission.resume_policy is ResumePolicy.NEW

    history = recovery_env / "session-1" / "history.jsonl"
    history.write_text('{"delivery_id":"already-durable"}\n', encoding="utf-8")
    checkpoint = HarnessCheckpoint(
        provider="codex",
        schema_version="1",
        agent_id="external:codex:session-1",
        host_session_id="session-1",
        checkpoint_id="checkpoint-1",
        sequence=1,
        data={"thread_id": "thread-secret-value", "token": "credential-like"},
        provider_session_id="thread-secret-value",
    )
    receipt = await recovery.save(checkpoint, reason=CheckpointReason.TURN_COMPLETED)

    raw = recovery.path.read_text(encoding="utf-8")
    assert "not-persisted" not in raw
    assert "thread-secret-value" not in raw
    assert "credential-like" not in raw
    archive = json.loads(raw)
    assert archive["binding"]["subject_id"] == "alice"
    assert archive["replay_position"] == {
        "history_file": "history.jsonl",
        "byte_offset": history.stat().st_size,
    }

    await recovery.mark_pending_interaction(
        "secret-interaction-id",
        turn_id="secret-active-turn",
    )
    raw = recovery.path.read_text(encoding="utf-8")
    assert "secret-interaction-id" not in raw
    assert "secret-active-turn" not in raw
    blocked = SessionExecutionRecovery(
        session_id="session-1",
        execution_profile_id="codex-profile",
        binding=binding,
        runtime_paths=paths,
    )
    with pytest.raises(
        ExecutionRecoveryUnavailableError,
        match="interaction Turn did not reach",
    ):
        blocked.prepare(_card(), agent_id="external:codex:session-1")
    await recovery.clear_pending_interactions()

    restored = SessionExecutionRecovery(
        session_id="session-1",
        execution_profile_id="codex-profile",
        binding=binding,
        runtime_paths=paths,
    ).prepare(_card(), agent_id="external:codex:session-1")
    assert restored.resume_policy is ResumePolicy.REQUIRE_RESUME
    assert restored.checkpoint is not None
    assert restored.checkpoint.data["thread_id"] == "thread-secret-value"
    assert restored.checkpoint.revision == receipt.storage_revision
    assert restored.checkpoint_sink.load_host_state()["subagents"]["records"] == {
        "child": {"role": "secret-role"}
    }


@pytest.mark.asyncio
async def test_child_checkpoint_lives_under_parent_and_is_restorable(
    tmp_path: Path,
    recovery_env: Path,
) -> None:
    parent_binding = _binding(tmp_path)
    paths = _paths(tmp_path)
    parent = SessionExecutionRecovery(
        session_id="session-1",
        execution_profile_id="codex-profile",
        binding=parent_binding,
        runtime_paths=paths,
    )
    child_id = "session-1_sub_explore_deadbeef"
    child_binding = ExecutionBinding.create(
        AgentExecutionSpec("codex", "r1", provider_config={"api_key": "not-persisted"}),
        subject_id=f"subagent:{child_id}",
        host_session_id=child_id,
        workspace=str(paths.runtime_workspace_root),
    )
    child = parent.child(child_binding, paths)
    assert child is not None
    assert child.path == (
        recovery_env
        / "session-1"
        / "subagents"
        / child_id
        / "execution-recovery.json"
    )
    child.prepare(_card(), agent_id=f"subagent:{child_id}")
    checkpoint = HarnessCheckpoint(
        provider="codex",
        schema_version="1",
        agent_id=f"subagent:{child_id}",
        host_session_id=child_id,
        checkpoint_id="child-checkpoint-1",
        sequence=1,
        data={"thread_id": "child-thread"},
    )
    await child.save(checkpoint, reason=CheckpointReason.SESSION_ACTIVATED)

    cold = parent.child(child_binding, paths, create_if_missing=False)
    assert cold is not None and cold.has_checkpoint()
    plan = cold.prepare(_card(), agent_id=f"subagent:{child_id}")
    assert plan.resume_policy is ResumePolicy.REQUIRE_RESUME
    assert plan.checkpoint is not None
    assert plan.checkpoint.data["thread_id"] == "child-thread"


@pytest.mark.asyncio
async def test_checkpoint_save_is_idempotent_and_rejects_stale_writes(
    tmp_path: Path,
    recovery_env: Path,
) -> None:
    del recovery_env
    recovery = SessionExecutionRecovery(
        session_id="session-1",
        execution_profile_id="codex-profile",
        binding=_binding(tmp_path),
        runtime_paths=_paths(tmp_path),
    )
    recovery.prepare(_card(), agent_id="external:codex:session-1")
    checkpoint = HarnessCheckpoint(
        provider="codex",
        schema_version="1",
        agent_id="external:codex:session-1",
        host_session_id="session-1",
        checkpoint_id="checkpoint-1",
        sequence=1,
        data={"thread_id": "thread-1"},
    )
    first = await recovery.save(checkpoint, reason=CheckpointReason.SESSION_ACTIVATED)
    duplicate = await recovery.save(checkpoint, reason=CheckpointReason.SESSION_ACTIVATED)
    assert duplicate == first

    changed_duplicate = HarnessCheckpoint(
        provider="codex",
        schema_version="1",
        agent_id="external:codex:session-1",
        host_session_id="session-1",
        checkpoint_id="checkpoint-1",
        sequence=1,
        data={"thread_id": "different"},
    )
    with pytest.raises(CheckpointConflictError, match="different data"):
        await recovery.save(
            changed_duplicate,
            reason=CheckpointReason.SESSION_ACTIVATED,
        )

    stale = HarnessCheckpoint(
        provider="codex",
        schema_version="1",
        agent_id="external:codex:session-1",
        host_session_id="session-1",
        checkpoint_id="checkpoint-2",
        sequence=2,
        data={"thread_id": "thread-1"},
    )
    with pytest.raises(CheckpointConflictError, match="revision changed"):
        await recovery.save(
            stale,
            reason=CheckpointReason.STATE_CHANGED,
            expected_storage_revision="wrong",
        )


@pytest.mark.asyncio
async def test_execution_session_supplies_new_then_required_resume_context(
    tmp_path: Path,
    recovery_env: Path,
) -> None:
    del recovery_env
    binding = _binding(tmp_path)
    paths = _paths(tmp_path)

    class Cursor:
        def __init__(self) -> None:
            self.closed = asyncio.Event()

        def __aiter__(self):
            return self

        async def __anext__(self):
            await self.closed.wait()
            raise StopAsyncIteration

        async def aclose(self) -> None:
            self.closed.set()

    class Harness:
        card = _card()
        provider_session_id = None

        def __init__(self) -> None:
            self.state = HarnessState.TERMINATED
            self.cursor = Cursor()
            self.context: HarnessContext | None = None

        async def start(self, context: HarnessContext) -> None:
            self.context = context
            self.state = HarnessState.IDLE

        async def stop(self) -> None:
            self.state = HarnessState.TERMINATED
            self.cursor.closed.set()

        def events(self):
            return self.cursor

    recovery = SessionExecutionRecovery(
        session_id="session-1",
        execution_profile_id="codex-profile",
        binding=binding,
        runtime_paths=paths,
    )
    first_harness = Harness()
    first = ExecutionSession(
        HarnessEngine(binding, first_harness),
        paths,
        recovery=recovery,
    )
    context = HarnessContext(
        agent_name="external",
        agent_id="external:codex:session-1",
        host_session_id="session-1",
        system_prompt="",
        cwd=str(paths.cwd),
    )
    await first.start(context)
    assert first_harness.context is not None
    assert first_harness.context.resume_policy is ResumePolicy.NEW
    assert first_harness.context.checkpoint_sink is recovery
    await first.stop()

    checkpoint = HarnessCheckpoint(
        provider="codex",
        schema_version="1",
        agent_id="external:codex:session-1",
        host_session_id="session-1",
        checkpoint_id="checkpoint-1",
        sequence=1,
        data={"thread_id": "thread-1"},
    )
    await recovery.save(checkpoint, reason=CheckpointReason.SESSION_ACTIVATED)
    cold = SessionExecutionRecovery(
        session_id="session-1",
        execution_profile_id="codex-profile",
        binding=binding,
        runtime_paths=paths,
    )
    second_harness = Harness()
    second = ExecutionSession(
        HarnessEngine(binding, second_harness),
        paths,
        recovery=cold,
    )
    await second.start(context)
    assert second_harness.context is not None
    assert second_harness.context.resume_policy is ResumePolicy.REQUIRE_RESUME
    assert second_harness.context.checkpoint is not None
    assert second_harness.context.checkpoint.data["thread_id"] == "thread-1"
    await second.stop()


@pytest.mark.asyncio
async def test_interaction_turn_blocks_cold_resume_until_terminal(
    tmp_path: Path,
    recovery_env: Path,
) -> None:
    del recovery_env
    binding = _binding(tmp_path)
    paths = _paths(tmp_path)
    recovery = SessionExecutionRecovery(
        session_id="session-1",
        execution_profile_id="codex-profile",
        binding=binding,
        runtime_paths=paths,
    )
    recovery.prepare(_card(), agent_id="external:codex:session-1")
    await recovery.save(
        HarnessCheckpoint(
            provider="codex",
            schema_version="1",
            agent_id="external:codex:session-1",
            host_session_id="session-1",
            checkpoint_id="checkpoint-1",
            sequence=1,
            data={"thread_id": "thread-1"},
        ),
        reason=CheckpointReason.SESSION_ACTIVATED,
    )
    session = object.__new__(ExecutionSession)
    session._recovery = recovery
    await session._observe_projected_output(
        ProjectedOutput(
            turn_id="turn-1",
            chunk=OutputSchema(
                type="__interaction__",
                index=0,
                payload=InteractionOutput(id="question-1", value={"prompt": "continue?"}),
            ),
        )
    )

    cold = SessionExecutionRecovery(
        session_id="session-1",
        execution_profile_id="codex-profile",
        binding=binding,
        runtime_paths=paths,
    )
    with pytest.raises(ExecutionRecoveryUnavailableError, match="interaction Turn"):
        cold.prepare(_card(), agent_id="external:codex:session-1")

    await session._observe_projected_output(
        ProjectedOutput(turn_id="turn-1", terminal=TurnEventKind.FINISHED)
    )
    plan = cold.prepare(_card(), agent_id="external:codex:session-1")
    assert plan.resume_policy is ResumePolicy.REQUIRE_RESUME


@pytest.mark.asyncio
async def test_scope_version_checkpoint_and_replay_mismatch_are_history_only(
    tmp_path: Path,
    recovery_env: Path,
) -> None:
    paths = _paths(tmp_path)
    initial = SessionExecutionRecovery(
        session_id="session-1",
        execution_profile_id="codex-profile",
        binding=_binding(tmp_path),
        runtime_paths=paths,
    )
    initial.prepare(_card(), agent_id="external:codex:session-1")
    archive = json.loads(initial.path.read_text(encoding="utf-8"))
    archive["initializing_process"] = "previous-process"
    initial.path.write_text(json.dumps(archive), encoding="utf-8")

    with pytest.raises(ExecutionRecoveryUnavailableError, match="Binding changed"):
        SessionExecutionRecovery(
            session_id="session-1",
            execution_profile_id="codex-profile",
            binding=_binding(tmp_path, subject_id="mallory"),
            runtime_paths=paths,
        )

    same = SessionExecutionRecovery(
        session_id="session-1",
        execution_profile_id="codex-profile",
        binding=_binding(tmp_path),
        runtime_paths=paths,
    )
    with pytest.raises(ExecutionRecoveryUnavailableError, match="version is incompatible"):
        same.prepare(_card(version="2.0"), agent_id="external:codex:session-1")
    with pytest.raises(ExecutionRecoveryUnavailableError, match="checkpoint is missing"):
        same.prepare(_card(), agent_id="external:codex:session-1")

    checkpoint = HarnessCheckpoint(
        provider="codex",
        schema_version="1",
        agent_id="external:codex:session-1",
        host_session_id="session-1",
        checkpoint_id="checkpoint-1",
        sequence=1,
        data={"thread_id": "thread-1"},
    )
    await same.save(checkpoint, reason=CheckpointReason.SESSION_ACTIVATED)
    archive = json.loads(same.path.read_text(encoding="utf-8"))
    archive["replay_position"]["byte_offset"] = 10
    same.path.write_text(json.dumps(archive), encoding="utf-8")
    with pytest.raises(ExecutionRecoveryUnavailableError, match="history was truncated"):
        same.prepare(_card(), agent_id="external:codex:session-1")


@pytest.mark.asyncio
@pytest.mark.parametrize("full_access,old_digest", [
    (False, "72282159ebaa0f09e27125b00b4a9c87136226711684164b1ce652fdec2ce0e1"),
    (True, "6b829962018a9c9becd2a36ccb34d104549f21bc93620a099e38b238573f4688"),
])
async def test_legacy_archive_accepts_unchanged_profile_but_rejects_authorization_migration(
    tmp_path, recovery_env, full_access, old_digest,
):
    from dataclasses import replace
    from openjiuwen.harness_protocol import ExecutionAuthorization
    from jiuwenswarm.runtime.harness.config_source import load_execution_catalog

    paths = _paths(tmp_path)
    old = ExecutionBinding("alice", "session-1", str(paths.runtime_workspace_root), "codex", "old-r1", old_digest)
    recovery = SessionExecutionRecovery(session_id="session-1", execution_profile_id="legacy", binding=old,
                                        runtime_paths=paths)
    recovery.prepare(_card(), agent_id="external:codex:session-1")
    await recovery.save(HarnessCheckpoint(
        provider="codex", schema_version="1", agent_id="external:codex:session-1", host_session_id="session-1",
        checkpoint_id="old-checkpoint", sequence=1, data={"thread_id": "legacy-thread"},
    ), reason=CheckpointReason.TURN_COMPLETED)
    catalog = load_execution_catalog({
        "permissions": {"enabled": not full_access},
        "execution": {"default_profile_id": "legacy", "profiles": {
            "legacy": {"provider_id": "codex", "config_revision": "old-r1",
                       "provider_config": {"model": {"model": "fixture"}}},
        }},
    })
    spec = catalog.source().resolve()
    binding = ExecutionBinding.create(spec, subject_id="alice", host_session_id="session-1",
                                       workspace=str(paths.runtime_workspace_root))
    assert binding == old
    restored = SessionExecutionRecovery(session_id="session-1", execution_profile_id="legacy", binding=binding,
                                         runtime_paths=paths).prepare(_card(), agent_id="external:codex:session-1")
    assert restored.resume_policy is ResumePolicy.REQUIRE_RESUME
    assert restored.checkpoint.data["thread_id"] == "legacy-thread"
    changed = ExecutionBinding.create(replace(spec, authorization=ExecutionAuthorization(full_access)),
                                      subject_id="alice", host_session_id="session-1",
                                      workspace=str(paths.runtime_workspace_root))
    with pytest.raises(ExecutionRecoveryUnavailableError):
        SessionExecutionRecovery(session_id="session-1", execution_profile_id="legacy", binding=changed,
                                 runtime_paths=paths).prepare(_card(), agent_id="external:codex:session-1")
