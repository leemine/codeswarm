# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Identity and resource construction for the External Browser child."""

from __future__ import annotations

from pathlib import Path

import pytest

from openjiuwen.harness.engine import ExecutionBinding
from openjiuwen.harness.subagent_runtime import SubagentBuildRequest
from openjiuwen.harness.tools.browser_move.playwright_runtime.browser_capabilities import (
    browser_tool_allowlist_fingerprint,
    resolve_browser_capabilities,
)
from openjiuwen.harness_protocol import AgentExecutionSpec

from jiuwenswarm.common.runtime_workspace import RuntimeWorkspacePaths
from jiuwenswarm.runtime.harness.external_browser import (
    build_external_browser_resources,
)


def _paths(tmp_path: Path) -> RuntimeWorkspacePaths:
    root = (tmp_path / "project").resolve()
    cwd = root / "task"
    cwd.mkdir(parents=True)
    return RuntimeWorkspacePaths(
        internal_workspace_dir=(root / ".internal").resolve(),
        runtime_workspace_root=root,
        cwd=cwd,
        project_root=root,
        outputs_dir=(root / "outputs").resolve(),
    )


def _binding(paths: RuntimeWorkspacePaths) -> ExecutionBinding:
    return ExecutionBinding.create(
        AgentExecutionSpec("codex", "parent-r1"),
        subject_id="subagent:parent_sub_browser_deadbeef",
        host_session_id="parent_sub_browser_deadbeef",
        workspace=str(paths.runtime_workspace_root),
    )


def _request() -> SubagentBuildRequest:
    return SubagentBuildRequest(
        subagent_id="parent_sub_browser_deadbeef",
        subagent_type="browser_agent",
        display_name="Browser",
        role="Use only the admitted browser scope",
        browser_capabilities=("vision",),
    )


def test_builds_identity_bound_same_provider_gateway_without_starting_browser(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    calls = []

    def admit(identity, invocation):
        calls.append((identity, invocation))
        return False

    resources = build_external_browser_resources(
        request=_request(),
        child_binding=_binding(paths),
        parent_subject_id="alice",
        parent_session_id="parent",
        channel_id="web",
        runtime_paths=paths,
        admit=admit,
    )

    identity = resources.gateway.execution_identity
    assert identity.profile.owner_subject_id == "alice"
    assert identity.instance.parent_session_id == "parent"
    assert identity.instance.subagent_id == "parent_sub_browser_deadbeef"
    assert identity.instance.workspace == str(paths.runtime_workspace_root)
    expected_capabilities = resolve_browser_capabilities(("vision",))
    assert identity.task.capability_fingerprint == browser_tool_allowlist_fingerprint(
        expected_capabilities.allowed_tool_names
    )
    assert resources.gateway.closed is False
    assert calls == []
    assert not (paths.internal_workspace_dir / ".browser-profiles").exists()
    assert "do not start another agent or Browser worker" in resources.system_prompt


def test_rejects_child_binding_outside_requested_subagent_before_side_effects(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    wrong_binding = ExecutionBinding.create(
        AgentExecutionSpec("codex", "parent-r1"),
        subject_id="subagent:other",
        host_session_id="other",
        workspace=str(paths.runtime_workspace_root),
    )

    with pytest.raises(ValueError, match="does not match the subagent"):
        build_external_browser_resources(
            request=_request(),
            child_binding=wrong_binding,
            parent_subject_id="alice",
            parent_session_id="parent",
            channel_id="web",
            runtime_paths=paths,
            admit=lambda _identity, _invocation: True,
        )


def test_rejects_unknown_capability_before_browser_construction(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    request = _request()
    request = SubagentBuildRequest(
        subagent_id=request.subagent_id,
        subagent_type=request.subagent_type,
        display_name=request.display_name,
        role=request.role,
        browser_capabilities=("unknown-browser-capability",),
    )

    with pytest.raises(ValueError, match="unknown-browser-capability"):
        build_external_browser_resources(
            request=request,
            child_binding=_binding(paths),
            parent_subject_id="alice",
            parent_session_id="parent",
            channel_id="web",
            runtime_paths=paths,
            admit=lambda _identity, _invocation: True,
        )
