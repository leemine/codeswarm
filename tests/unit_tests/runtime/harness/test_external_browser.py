# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Identity and resource construction for the External Browser child."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from openjiuwen.harness.engine import ExecutionBinding
from openjiuwen.harness.subagent_runtime import SubagentBuildRequest
from openjiuwen.harness.tools.browser_move.playwright_runtime.browser_capabilities import (
    browser_tool_allowlist_fingerprint,
    resolve_browser_capabilities,
)
from openjiuwen.harness_protocol import AgentExecutionSpec
from openjiuwen.core.foundation.tool import McpServerConfig

from jiuwenswarm.common.playwright_mcp_runtime import PlaywrightMcpLaunch
from jiuwenswarm.common.runtime_workspace import RuntimeWorkspacePaths
from jiuwenswarm.runtime.harness.external_browser import (
    _bind_product_playwright_runtime,
    build_external_browser_resources,
)
from jiuwenswarm.runtime.harness.external_browser_artifacts import (
    ExternalBrowserArtifactGateway,
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
    output_root = paths.outputs_dir / "browser" / identity.task.task_id
    assert output_root.relative_to(paths.runtime_workspace_root).as_posix() in (
        resources.system_prompt
    )


def test_wraps_core_gateway_for_product_artifact_delivery(tmp_path: Path) -> None:
    paths = _paths(tmp_path)

    async def sink(_artifact, _path) -> None:
        return None

    resources = build_external_browser_resources(
        request=_request(),
        child_binding=_binding(paths),
        parent_subject_id="alice",
        parent_session_id="parent",
        channel_id="web",
        runtime_paths=paths,
        admit=lambda _identity, _invocation: True,
        artifact_sink=sink,
        decision_id_for=lambda _identity, _invocation: "decision-1",
    )

    assert isinstance(resources.gateway, ExternalBrowserArtifactGateway)
    assert resources.gateway.execution_identity.task.task_id.startswith("browser-task-")
    core_gateway = resources.gateway._gateway
    args = core_gateway._runtime.service.mcp_cfg.params["args"]
    env = core_gateway._runtime.service.mcp_cfg.params["env"]
    output_root = paths.outputs_dir / "browser" / resources.gateway.execution_identity.task.task_id
    assert args[-2:] == ["--output-dir", str(output_root)]
    init_page = Path(env["PLAYWRIGHT_MCP_INIT_PAGE"])
    assert init_page.parent == (
        paths.internal_workspace_dir
        / "browser-config"
        / resources.gateway.execution_identity.task.task_id
    )
    source = init_page.read_text(encoding="utf-8")
    assert init_page.stat().st_mode & 0o777 == 0o600
    assert json.dumps(str(output_root)) in source
    assert 'behavior: "allowAndName"' in source
    assert "eventsEnabled: true" in source


def test_external_browser_uses_bundled_product_playwright_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenswarm.runtime.harness import external_browser as module

    config = McpServerConfig(
        server_id="playwright_official_stdio__task",
        server_name="playwright-official-task",
        server_path="stdio://playwright",
        client_type="stdio",
        params={
            "command": "npx",
            "args": [
                "-y",
                "@playwright/mcp@0.0.78",
                "--caps=pdf,vision",
            ],
        },
    )
    monkeypatch.setattr(
        module,
        "resolve_playwright_mcp_launch",
        lambda **_kwargs: PlaywrightMcpLaunch(
            source="bundled",
            command="/usr/bin/node",
            args=("/managed/playwright/cli.js",),
            version="0.0.78",
            runtime_display_path="runtime/playwright-mcp/0.0.78/hash",
        ),
    )

    _bind_product_playwright_runtime(
        config,
        user_workspace_dir=tmp_path / "runtime",
    )

    assert config.params["command"] == "/usr/bin/node"
    assert config.params["args"] == [
        "/managed/playwright/cli.js",
        "--caps=pdf,vision",
    ]


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


def test_rejects_symlinked_browser_config_root_before_writing_init_page(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    paths.internal_workspace_dir.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (paths.internal_workspace_dir / "browser-config").symlink_to(
        outside,
        target_is_directory=True,
    )

    with pytest.raises(ValueError, match="config root is not controlled"):
        build_external_browser_resources(
            request=_request(),
            child_binding=_binding(paths),
            parent_subject_id="alice",
            parent_session_id="parent",
            channel_id="web",
            runtime_paths=paths,
            admit=lambda _identity, _invocation: True,
        )

    assert list(outside.iterdir()) == []
