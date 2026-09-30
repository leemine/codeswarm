"""R1-11B cold-start context, workspace and runtime-policy compiler."""

from dataclasses import replace

import pytest

from openjiuwen.harness_protocol import ExecutionAuthorization, WorkspaceAccess

from jiuwenswarm.runtime.harness.context_bridge import (
    build_external_context,
    build_external_context_snapshot,
    build_external_input,
)
from jiuwenswarm.runtime.harness.surface import (
    EffectiveSurfaceSnapshot,
    compile_surface_policy,
)
from tests.unit_tests.runtime.harness.test_surface_identity import _identity, _metadata


def _compiled(tmp_path, *, work_mode="code", state="normal", full_access=False, personal=False):
    metadata = _metadata()
    metadata["work_mode"] = work_mode
    metadata["mode"] = f"agent.{work_mode}.{state}"
    identity = _identity(tmp_path, metadata)
    return compile_surface_policy(
        EffectiveSurfaceSnapshot(identity, metadata["mode"]),
        authorization=ExecutionAuthorization(full_access=full_access),
        include_personal_context=personal,
    )


@pytest.mark.parametrize(
    "work_mode,expected,absent",
    [
        ("work", {"documents", "web", "artifacts"}, {"terminal", "git", "lsp"}),
        ("code", {"filesystem", "terminal", "git", "test", "review", "lsp"}, {"documents"}),
    ],
)
def test_work_and_code_compile_distinct_requirement_snapshots(tmp_path, work_mode, expected, absent):
    snapshot = _compiled(tmp_path, work_mode=work_mode)
    policy = snapshot.runtime_policy
    assert policy.workspace_access is WorkspaceAccess.WORKSPACE_WRITE
    assert expected <= set(policy.required_capabilities)
    assert not absent & set(policy.required_capabilities)
    assert policy.surface.value == work_mode
    assert snapshot.identity.work_mode == work_mode


def test_plan_always_narrows_full_access_authorization(tmp_path):
    snapshot = _compiled(tmp_path, state="plan", full_access=True)
    assert snapshot.runtime_policy.workspace_access is WorkspaceAccess.READ_ONLY
    assert snapshot.runtime_policy.execution_state.value == "plan"


def test_policy_update_changes_only_next_snapshot_not_session_identity(tmp_path):
    original = _compiled(tmp_path, personal=False)
    updated = compile_surface_policy(
        original,
        authorization=ExecutionAuthorization(),
        include_personal_context=True,
    )
    assert original.identity == updated.identity
    assert "personal_context" not in original.runtime_policy.context_sources
    assert "personal_context" in updated.runtime_policy.context_sources
    assert original.runtime_policy.fingerprint != updated.runtime_policy.fingerprint


@pytest.mark.asyncio
async def test_context_bytes_freeze_for_cycle_and_reload_after_cold_start(tmp_path, monkeypatch):
    surface = _compiled(tmp_path, personal=True)
    root = surface.identity.paths.runtime_workspace_root
    rules = root / "JIUWENSWARM.md"
    rules.write_text("first project rule", encoding="utf-8")
    monkeypatch.setattr(
        "jiuwenswarm.runtime.harness.context_bridge._personal_context",
        lambda: "first personal context",
    )
    frozen = build_external_context_snapshot(paths=surface.identity.paths, surface=surface)
    context = build_external_context(
        paths=surface.identity.paths,
        host_session_id="session-1",
        channel_id="web",
        provider_id="codex",
        surface=surface,
        context_snapshot=frozen,
    )
    assert "Code Surface" in context.system_prompt
    assert "first project rule" in context.system_prompt
    assert "first personal context" in context.system_prompt
    assert context.runtime_policy is surface.runtime_policy

    rules.write_text("second project rule", encoding="utf-8")
    external_input = await build_external_input(
        query="inspect",
        request_id="request-1",
        session_id="session-1",
        params={},
        paths=surface.identity.paths,
        include_personal_context=True,
        surface=surface,
        context_snapshot=frozen,
    )
    assert external_input.content == "inspect"
    assert "second project rule" not in context.system_prompt
    assert external_input.metadata["context_sections"] == ("project", "personal")

    refreshed = build_external_context_snapshot(paths=surface.identity.paths, surface=surface)
    refreshed_context = build_external_context(
        paths=surface.identity.paths,
        host_session_id="session-1",
        channel_id="web",
        provider_id="codex",
        surface=surface,
        context_snapshot=refreshed,
    )
    assert "second project rule" in refreshed_context.system_prompt
    assert "first project rule" not in refreshed_context.system_prompt


def test_missing_enabled_context_source_is_auditable(tmp_path, monkeypatch):
    surface = _compiled(tmp_path, work_mode="work", personal=True)
    monkeypatch.setattr(
        "jiuwenswarm.runtime.harness.context_bridge._personal_context",
        lambda: "",
    )
    frozen = build_external_context_snapshot(paths=surface.identity.paths, surface=surface)
    context = build_external_context(
        paths=surface.identity.paths,
        host_session_id="session-1",
        channel_id="web",
        provider_id="opencode",
        surface=surface,
        context_snapshot=frozen,
    )
    assert "personal_context" in context.metadata["context_sources_unavailable"]
    assert "Work Surface" in context.system_prompt


def test_surface_context_cannot_be_reused_for_another_workspace(tmp_path):
    surface = _compiled(tmp_path)
    other_paths = replace(
        surface.identity.paths,
        cwd=tmp_path / "outside",
        runtime_workspace_root=tmp_path / "outside",
        project_root=tmp_path / "outside",
    )
    (tmp_path / "outside").mkdir()
    frozen = build_external_context_snapshot(paths=surface.identity.paths, surface=surface)
    # HarnessContext still carries the exact caller-supplied paths, so an
    # ExecutionSession bound to the original identity rejects this context.
    with pytest.raises(ValueError, match="paths differ"):
        build_external_context(
            paths=other_paths,
            host_session_id="session-1",
            channel_id="web",
            provider_id="codex",
            surface=surface,
            context_snapshot=frozen,
        )
