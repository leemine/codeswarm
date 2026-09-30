"""R1-11A admission, immutable identity and durable recovery boundaries."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from openjiuwen.harness_protocol import ExecutionAuthorization, WorkspaceAccess

from jiuwenswarm.runtime.harness.surface import (
    EffectiveSurfaceSnapshot,
    SurfaceAdmissionError,
    build_surface_identity,
    canonical_surface_mode,
    compile_surface_policy,
    creation_surface,
    validate_surface_metadata,
    validate_surface_request,
)
from jiuwenswarm.runtime.harness.recovery_store import (
    SessionExecutionRecovery,
    ExecutionRecoveryUnavailableError,
)
from tests.unit_tests.runtime.harness.test_execution_recovery import _binding, _paths
from tests.unit_tests.runtime.harness import (
    test_execution_recovery as recovery_fixtures,
)

recovery_env = recovery_fixtures.recovery_env


def _metadata():
    return {
        "session_id": "session-1",
        "channel_id": "web",
        "user_id": "alice",
        "mode": "agent.code.normal",
        "work_mode": "code",
        "execution_profile_id": "codex-profile",
    }


def _identity(tmp_path, metadata=None):
    return build_surface_identity(
        metadata=metadata or _metadata(),
        binding=_binding(tmp_path),
        paths=_paths(tmp_path),
        channel_id="web",
    )


def _recovery(identity):
    return SessionExecutionRecovery(
        session_id=identity.binding.host_session_id,
        execution_profile_id=identity.execution_profile_id,
        binding=identity.binding,
        runtime_paths=identity.paths,
        surface_identity=identity,
    )


@pytest.mark.parametrize(
    "mode,work_mode,expected",
    [
        ("agent", None, "agent.work.normal"),
        ("code.plan", None, "agent.code.plan"),
        ("agent.plan", "code", "agent.code.plan"),
        ("team", "code", "team.code.normal"),
        ("agent.code.normal", None, "agent.code.normal"),
    ],
)
def test_legacy_modes_derive_only_from_explicit_facts(mode, work_mode, expected):
    assert canonical_surface_mode({"mode": mode, "work_mode": work_mode}) == expected


@pytest.mark.parametrize(
    "metadata",
    [
        {},
        {"mode": False},
        {"mode": 42},
        {"mode": "unknown"},
        {"mode": "new-provider-mode"},
        {"mode": "agent.code.normal", "work_mode": "work"},
        {"mode": "agent", "work_mode": "invalid"},
    ],
)
def test_ambiguous_legacy_fails_closed(metadata):
    with pytest.raises(SurfaceAdmissionError):
        canonical_surface_mode(metadata)


@pytest.mark.parametrize(
    "field,value",
    [
        ("mode", "agent.work.normal"),
        ("mode", "team.code.normal"),
        ("work_mode", "work"),
        ("project_id", "another-project"),
        ("project_dir", "/another-root"),
        ("user_id", "mallory"),
        ("session_id", "another-session"),
        ("channel_id", "tui"),
        ("execution_profile_id", "opencode-profile"),
        ("execution_config_revision", "new-revision"),
        ("execution_config_fingerprint", "new-authorization"),
        ("team_name", "another-team"),
    ],
)
def test_creation_fields_cannot_be_rewritten(field, value):
    metadata = _metadata()
    metadata["surface_creation"] = creation_surface(metadata)
    metadata[field] = value
    with pytest.raises(SurfaceAdmissionError):
        validate_surface_metadata(metadata)


def test_state_is_not_surface_identity(tmp_path):
    metadata = _metadata()
    metadata["surface_creation"] = creation_surface(metadata)
    before = _identity(tmp_path, metadata)
    metadata["mode"] = "agent.code.plan"
    assert _identity(tmp_path, metadata) == before
    assert (
        validate_surface_request(metadata, {"mode": "code.normal"})
        == "agent.code.normal"
    )
    with pytest.raises(SurfaceAdmissionError, match="work_mode is immutable"):
        validate_surface_request(metadata, {"work_mode": "work"})
    with pytest.raises(SurfaceAdmissionError, match="topology is immutable"):
        validate_surface_request(metadata, {"mode": "team"})


@pytest.mark.parametrize(
    "field,value",
    [
        ("session_id", "session-2"),
        ("user_id", "mallory"),
        ("channel_id", "tui"),
        ("project_dir", "/other-workspace"),
    ],
)
def test_legacy_scope_must_match_admitted_binding(tmp_path, field, value):
    metadata = _metadata()
    metadata[field] = value
    with pytest.raises(SurfaceAdmissionError):
        _identity(tmp_path, metadata)


def test_snapshot_frozen_and_policy_revision_not_recovery_identity(
    tmp_path, recovery_env
):
    identity = _identity(tmp_path)
    snapshot = EffectiveSurfaceSnapshot(identity, "agent.code.normal")
    with pytest.raises(FrozenInstanceError):
        snapshot.policy_revision = "mutated"
    recovery = _recovery(identity)
    old_archive = recovery.path.read_bytes()
    new_snapshot = replace(snapshot, policy_revision="new-cold-start-policy")
    _recovery(new_snapshot.identity)
    assert recovery.path.read_bytes() == old_archive
    with pytest.raises(SurfaceAdmissionError, match="runtime policy is not compiled"):
        snapshot.validate_mode("code.plan")


@pytest.mark.parametrize(
    "field,value",
    [
        ("work_mode", "work"),
        ("project_id", "another"),
        ("topology", "team"),
        ("team_name", "another"),
        ("channel_id", "tui"),
    ],
)
def test_cold_recovery_rejects_surface_drift(tmp_path, recovery_env, field, value):
    identity = _identity(tmp_path)
    recovery = _recovery(identity)
    original = recovery.path.read_bytes()
    with pytest.raises(
        ExecutionRecoveryUnavailableError, match="Surface identity changed"
    ):
        _recovery(replace(identity, **{field: value}))
    assert recovery.path.read_bytes() == original


def test_legacy_archive_upgrades_once_under_existing_lock(tmp_path, recovery_env):
    identity = _identity(tmp_path)
    recovery = SessionExecutionRecovery(
        session_id="session-1",
        execution_profile_id="codex-profile",
        binding=identity.binding,
        runtime_paths=identity.paths,
    )
    assert json.loads(recovery.path.read_text())["schema_version"] == 1
    _recovery(identity)
    archive = json.loads(recovery.path.read_text())
    assert archive["schema_version"] == 2
    assert archive["surface_identity"] == identity.record()
    del archive["surface_identity"]
    recovery.path.write_text(json.dumps(archive))
    with pytest.raises(
        ExecutionRecoveryUnavailableError, match="Surface identity changed"
    ):
        _recovery(identity)


def test_concurrent_admission_has_one_surface_winner(tmp_path, recovery_env):
    identity = _identity(tmp_path)
    rival = replace(identity, work_mode="work", creation_mode="agent.work.normal")

    def admit(candidate):
        try:
            return _recovery(candidate).path
        except ExecutionRecoveryUnavailableError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(admit, (identity, rival)))
    assert sum(result is not None for result in results) == 1
    record = json.loads(next(p for p in results if p).read_text())
    assert record["surface_identity"] in (identity.record(), rival.record())


async def test_adapter_compiles_plan_read_only_before_allocating_provider(tmp_path, monkeypatch):
    from jiuwenswarm.server.runtime.agent_adapter.engine_adapter import (
        EngineAgentAdapter,
    )
    from tests.unit_tests.runtime.harness.test_external_execution_route import _route

    route = _route(tmp_path)
    metadata = _metadata()
    identity = build_surface_identity(
        metadata=metadata,
        binding=route.bound.binding,
        paths=route.runtime_paths,
        channel_id="web",
    )
    snapshot = EffectiveSurfaceSnapshot(identity, "agent.code.plan")
    adapter = EngineAgentAdapter(replace(route, surface=snapshot))
    calls = []
    monkeypatch.setattr(adapter, "_build_session", lambda: calls.append("allocated"))
    await adapter.create_instance(mode="code", sub_mode="plan")
    assert calls == ["allocated"]
    assert adapter._surface.runtime_policy.workspace_access is WorkspaceAccess.READ_ONLY


async def test_request_surface_mismatch_rejected_before_metadata_write(
    tmp_path, monkeypatch
):
    from jiuwenswarm.runtime.request import prepare_chat_turn

    metadata = _metadata()
    metadata["surface_creation"] = creation_surface(metadata)
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.session_metadata.get_session_metadata",
        lambda *_args, **_kwargs: metadata.copy(),
    )
    monkeypatch.setattr(
        "jiuwenswarm.common.config.get_config",
        lambda: {
            "execution": {
                "default_profile_id": "codex-profile",
                "profiles": {
                    "codex-profile": {"provider_id": "codex", "config_revision": "r1"}
                },
            }
        },
    )
    writes = []
    manager = SimpleNamespace(wait_for_session_prewarm=AsyncMock())
    request = SimpleNamespace(
        session_id="session-1",
        channel_id="web",
        user_id="alice",
        params={"mode": "agent", "work_mode": "work"},
    )
    with pytest.raises(SurfaceAdmissionError, match="work_mode is immutable"):
        await prepare_chat_turn(
            manager, request, "web", metadata_sync=lambda *a, **k: writes.append(k)
        )
    assert not writes
    manager.wait_for_session_prewarm.assert_not_awaited()


@pytest.mark.parametrize("field", ["cwd", "outputs_dir"])
def test_surface_paths_cannot_escape_bound_workspace(tmp_path, field):
    with pytest.raises(SurfaceAdmissionError, match="escape the workspace"):
        build_surface_identity(
            metadata=_metadata(),
            binding=_binding(tmp_path),
            paths=replace(_paths(tmp_path), **{field: tmp_path / "outside"}),
            channel_id="web",
        )


@pytest.mark.parametrize("params", [{"project_id": "other"}, {"project_dir": "/other"}])
def test_surface_project_request_rejected_before_rebinding(params):
    with pytest.raises(SurfaceAdmissionError, match="project is immutable"):
        validate_surface_request(_metadata(), params)


def test_child_recovery_inherits_surface_with_its_own_binding(tmp_path, recovery_env):
    identity = _identity(tmp_path)
    parent = _recovery(identity)
    child_binding = replace(identity.binding, host_session_id="child-1")
    child = parent.child(child_binding, identity.paths)
    record = json.loads(child.path.read_text())
    assert record["binding"]["host_session_id"] == "child-1"
    assert record["parent_session_id"] == "session-1"
    assert record["surface_identity"] == identity.record()
    with pytest.raises(ExecutionRecoveryUnavailableError):
        parent.child(replace(child_binding, subject_id="mallory"), identity.paths)


def test_effective_surface_is_visible_in_existing_harness_context(tmp_path):
    from jiuwenswarm.runtime.harness.context_bridge import build_external_context

    identity = _identity(tmp_path)
    snapshot = compile_surface_policy(
        EffectiveSurfaceSnapshot(identity, "agent.code.normal"),
        authorization=ExecutionAuthorization(),
        include_personal_context=False,
    )
    context = build_external_context(
        paths=identity.paths,
        host_session_id="session-1",
        channel_id="web",
        provider_id="codex",
        surface=snapshot,
    )
    assert context.metadata["surface"]["work_mode"] == "code"
    assert context.metadata["surface"]["topology"] == "single"
    assert context.metadata["surface_policy_revision"] == snapshot.policy_revision
    assert context.runtime_policy is snapshot.runtime_policy
    assert context.metadata["surface_policy_fingerprint"] == snapshot.runtime_policy.fingerprint
