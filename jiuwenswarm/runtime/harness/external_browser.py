# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Compose one identity-bound Browser gateway for an External child."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from openjiuwen.harness.engine import ExecutionBinding
from openjiuwen.harness.subagent_runtime import SubagentBuildRequest
from openjiuwen.harness.tools.browser_move.playwright_runtime import (
    BrowserBackend,
    BrowserExecutionFileRoots,
    BrowserExecutionIdentity,
    BrowserExecutionToolGateway,
    BrowserInstanceIdentity,
    BrowserProfileIdentity,
    BrowserTaskIdentity,
    BrowserToolAdmission,
)
from openjiuwen.harness.tools.browser_move.playwright_runtime.browser_capabilities import (
    CORE_BROWSER_CAPABILITY_NAME,
    POLICY_ONLY_BROWSER_CAPABILITY_NAMES,
    browser_tool_allowlist_fingerprint,
    resolve_browser_capabilities,
)
from openjiuwen.harness.tools.browser_move.playwright_runtime.config import (
    BrowserInstanceConfig,
    build_browser_guardrails,
    build_playwright_mcp_config,
)
from openjiuwen.harness.tools.browser_move.playwright_runtime.runtime import (
    BrowserAgentRuntime,
)

from jiuwenswarm.common.runtime_workspace import RuntimeWorkspacePaths

BROWSER_POLICY_REVISION = "r1-10-v1"


@dataclass(frozen=True, slots=True)
class ExternalBrowserResources:
    """Task-scoped Browser objects owned by one External child Session."""

    gateway: BrowserExecutionToolGateway
    system_prompt: str


def _stable_id(prefix: str, *values: str) -> str:
    payload = "\0".join(values).encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()[:32]
    return f"{prefix}-{digest}"


def _child_file_roots(
    paths: RuntimeWorkspacePaths,
    *,
    task_id: str,
) -> BrowserExecutionFileRoots:
    workspace = paths.runtime_workspace_root.resolve()
    private_root = workspace / ".jiuwenswarm"
    outputs_root = (
        paths.outputs_dir.resolve()
        if paths.outputs_dir is not None
        else private_root / "browser-outputs" / task_id
    )
    return BrowserExecutionFileRoots(
        workspace=str(workspace),
        uploads_root=str(private_root / "browser-inputs" / task_id),
        outputs_root=str(outputs_root),
        audit_root=str(private_root / "browser-audit" / task_id),
    )


def build_external_browser_resources(
    *,
    request: SubagentBuildRequest,
    child_binding: ExecutionBinding,
    parent_subject_id: str,
    parent_session_id: str,
    channel_id: str,
    runtime_paths: RuntimeWorkspacePaths,
    admit: BrowserToolAdmission,
) -> ExternalBrowserResources:
    """Build Browser identity and gateway before Provider/MCP side effects."""

    if child_binding.host_session_id != request.subagent_id:
        raise ValueError("Browser child Binding does not match the subagent")
    if child_binding.workspace != str(runtime_paths.runtime_workspace_root.resolve()):
        raise ValueError("Browser child workspace does not match runtime paths")

    resolved = resolve_browser_capabilities(request.browser_capabilities)
    if resolved.rejected_names:
        raise ValueError(
            "Unsupported Browser capabilities: " + ", ".join(resolved.rejected_names)
        )

    profile_id = _stable_id("browser-profile", channel_id, parent_subject_id)
    instance_id = _stable_id(
        "browser-instance",
        parent_session_id,
        request.subagent_id,
    )
    task_id = _stable_id("browser-task", request.subagent_id)
    request_id = _stable_id("browser-request", request.subagent_id)
    workspace = str(runtime_paths.runtime_workspace_root.resolve())
    profile = BrowserProfileIdentity(
        profile_id=profile_id,
        owner_subject_id=parent_subject_id,
        backend=BrowserBackend.MANAGED,
        policy_revision=BROWSER_POLICY_REVISION,
    )
    instance = BrowserInstanceIdentity(
        instance_id=instance_id,
        profile_id=profile.profile_id,
        profile_generation=profile.generation,
        parent_session_id=parent_session_id,
        subagent_id=request.subagent_id,
        workspace=workspace,
    )
    task = BrowserTaskIdentity(
        task_id=task_id,
        instance_id=instance.instance_id,
        instance_generation=instance.generation,
        child_turn_id=request.subagent_id,
        request_id=request_id,
        capability_fingerprint=browser_tool_allowlist_fingerprint(
            resolved.allowed_tool_names
        ),
    )
    identity = BrowserExecutionIdentity(
        profile=profile,
        instance=instance,
        task=task,
    )
    identity.validate_scope(
        owner_subject_id=parent_subject_id,
        parent_session_id=parent_session_id,
        subagent_id=request.subagent_id,
        workspace=workspace,
    )
    file_roots = _child_file_roots(runtime_paths, task_id=task_id)

    profile_home = (
        runtime_paths.internal_workspace_dir.resolve()
        / ".browser-profiles"
        / profile.profile_id
    )
    instance_config = BrowserInstanceConfig(
        key=instance.instance_id,
        driver_mode=BrowserBackend.MANAGED.value,
        user_data_dir=str(profile_home),
        profile_name=profile.profile_id,
    )
    mcp_config = build_playwright_mcp_config(
        instance_config,
        required_capability=[
            name
            for name in resolved.selected_names
            if name != CORE_BROWSER_CAPABILITY_NAME
            and name not in POLICY_ONLY_BROWSER_CAPABILITY_NAMES
        ],
    ).model_copy(deep=True)
    mcp_config.params["cwd"] = workspace
    runtime = BrowserAgentRuntime(
        provider=child_binding.provider_id,
        api_key="",
        api_base="",
        model_name="external-browser-tool-host",
        mcp_cfg=mcp_config,
        guardrails=build_browser_guardrails(),
        instance=instance_config,
        allowed_tool_names=resolved.allowed_tool_names,
        execution_identity=identity,
        file_roots=file_roots,
    )
    selected = ", ".join(resolved.selected_names)
    prompt = (
        "You are the dedicated Browser child for this delegated task. "
        "Use only the host-provided Browser tools and their current PageState; "
        "do not start another agent or Browser worker. "
        f"Admitted Browser capabilities: {selected}."
    )
    return ExternalBrowserResources(
        gateway=BrowserExecutionToolGateway(runtime, admit=admit),
        system_prompt=prompt,
    )


__all__ = [
    "BROWSER_POLICY_REVISION",
    "ExternalBrowserResources",
    "build_external_browser_resources",
]
