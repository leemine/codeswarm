# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Compose one identity-bound Browser gateway for an External child."""

from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

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
from openjiuwen.core.foundation.tool import McpServerConfig
from jiuwenswarm.common.playwright_mcp_runtime import (
    resolve_playwright_mcp_launch,
)
from jiuwenswarm.common.runtime_workspace import RuntimeWorkspacePaths
from jiuwenswarm.runtime.harness.external_browser_artifacts import (
    BrowserArtifactSink,
    BrowserDecisionId,
    ExternalBrowserArtifactGateway,
)

BROWSER_POLICY_REVISION = "r1-10-v1"
logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ExternalBrowserResources:
    """Task-scoped Browser objects owned by one External child Session."""

    gateway: BrowserExecutionToolGateway | ExternalBrowserArtifactGateway
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
    outputs_base = (
        paths.outputs_dir.resolve()
        if paths.outputs_dir is not None
        else private_root / "browser-outputs"
    )
    outputs_root = outputs_base / "browser" / task_id
    return BrowserExecutionFileRoots(
        workspace=str(workspace),
        uploads_root=str(private_root / "browser-inputs" / task_id),
        outputs_root=str(outputs_root),
        audit_root=str(private_root / "browser-audit" / task_id),
    )


def _bind_product_playwright_runtime(
    mcp_config: McpServerConfig,
    *,
    user_workspace_dir: Path,
) -> None:
    """Use the product-managed offline runtime without process-global env writes."""

    launch = resolve_playwright_mcp_launch(
        user_workspace_dir=user_workspace_dir,
    )
    if launch.source == "bundled":
        existing_args = [str(arg) for arg in mcp_config.params.get("args", ())]
        runtime_flags = [
            arg
            for arg in existing_args
            if arg.startswith("--") and arg not in {"--yes"}
        ]
        mcp_config.params["command"] = launch.command
        mcp_config.params["args"] = [*launch.args, *runtime_flags]
    logger.info(
        "External Browser Playwright MCP launch: source=%s version=%s runtime=%s",
        launch.source,
        launch.version,
        launch.runtime_display_path or "external",
    )


def _bind_task_output_dir(mcp_config: McpServerConfig, *, outputs_root: Path) -> None:
    """Bind MCP-managed generated files to one task output root."""

    existing_args = [str(arg) for arg in mcp_config.params.get("args", ())]
    filtered: list[str] = []
    skip_next = False
    for arg in existing_args:
        if skip_next:
            skip_next = False
            continue
        if arg == "--output-dir":
            skip_next = True
            continue
        if arg.startswith("--output-dir="):
            continue
        filtered.append(arg)
    mcp_config.params["args"] = [*filtered, "--output-dir", str(outputs_root)]


def _bind_managed_download_init(
    mcp_config: McpServerConfig,
    *,
    internal_workspace_dir: Path,
    outputs_root: Path,
    task_id: str,
) -> Path:
    """Configure downloads on the existing core-managed CDP context."""

    internal_root = internal_workspace_dir.resolve()
    config_root = internal_root / "browser-config"
    script_dir = config_root / task_id
    for directory in (config_root, script_dir):
        if directory.is_symlink():
            raise ValueError("External Browser config root is not controlled")
    script_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    if script_dir.is_symlink() or script_dir.resolve() != script_dir:
        raise ValueError("External Browser config root is not controlled")
    script_path = script_dir / "managed-download-init.cjs"
    if script_path.is_symlink():
        raise ValueError("External Browser init page must not be a symlink")
    download_path = json.dumps(str(outputs_root.resolve()))
    state_root = script_dir / "download-state"
    state_path = json.dumps(str(state_root))
    source = (
        '"use strict";\n'
        'const fs = require("node:fs");\n'
        'const path = require("node:path");\n'
        "exports.default = async ({ page }) => {\n"
        f"  const downloadPath = {download_path};\n"
        f"  const statePath = {state_path};\n"
        "  fs.mkdirSync(downloadPath, { recursive: true, mode: 0o700 });\n"
        "  fs.mkdirSync(statePath, { recursive: true, mode: 0o700 });\n"
        "  const browser = page.context().browser();\n"
        '  if (!browser) throw new Error("Managed Browser is unavailable");\n'
        '  const sessionKey = Symbol.for("jiuwenswarm.managedDownloadSession");\n'
        "  if (browser[sessionKey]) return;\n"
        "  const session = await browser.newBrowserCDPSession();\n"
        '  await session.send("Browser.setDownloadBehavior", {\n'
        '      behavior: "allowAndName",\n'
        "      downloadPath,\n"
        "      eventsEnabled: true,\n"
        "    });\n"
        "  const downloads = new Map();\n"
        '  session.on("Browser.downloadWillBegin", (event) => {\n'
        '    if (!/^[A-Za-z0-9_-]{1,128}$/.test(event.guid)) return;\n'
        "    let name = path.basename(String(event.suggestedFilename || \"\"))\n"
        "      .replace(/[\\u0000-\\u001f\\u007f]/g, \"_\").trim();\n"
        '    if (!name || name === "." || name === "..") name = `download-${event.guid}`;\n'
        "    downloads.set(event.guid, name.slice(0, 200));\n"
        '    fs.writeFileSync(path.join(statePath, `${event.guid}.pending`), "");\n'
        "  });\n"
        '  session.on("Browser.downloadProgress", (event) => {\n'
        '    if (!/^[A-Za-z0-9_-]{1,128}$/.test(event.guid)) return;\n'
        "    const pending = path.join(statePath, `${event.guid}.pending`);\n"
        '    if (event.state === "canceled") {\n'
        "      downloads.delete(event.guid);\n"
        '      try { fs.renameSync(pending, path.join(statePath, `${event.guid}.canceled`)); } catch {}\n'
        "      return;\n"
        "    }\n"
        '    if (event.state !== "completed") return;\n'
        "    const name = downloads.get(event.guid);\n"
        "    downloads.delete(event.guid);\n"
        "    if (!name) return;\n"
        "    const source = path.join(downloadPath, event.guid);\n"
        "    let target = path.join(downloadPath, name);\n"
        "    try {\n"
        "      if (fs.existsSync(target)) target = path.join(downloadPath, `${event.guid}-${name}`);\n"
        "      fs.renameSync(source, target);\n"
        '      fs.writeFileSync(path.join(statePath, `${event.guid}.completed`), path.basename(target));\n'
        "      fs.unlinkSync(pending);\n"
        "    } catch {\n"
        '      try { fs.renameSync(pending, path.join(statePath, `${event.guid}.failed`)); } catch {}\n'
        "    }\n"
        "  });\n"
        "  browser[sessionKey] = session;\n"
        "};\n"
    )
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(script_path, flags, 0o600)
    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(source)

    params = dict(mcp_config.params)
    env = dict(params.get("env", {}))
    env["PLAYWRIGHT_MCP_INIT_PAGE"] = str(script_path)
    params["env"] = env
    mcp_config.params = params
    return state_root


def build_external_browser_identity(
    *,
    request: SubagentBuildRequest,
    child_binding: ExecutionBinding,
    parent_subject_id: str,
    parent_session_id: str,
    channel_id: str,
    runtime_paths: RuntimeWorkspacePaths,
) -> BrowserExecutionIdentity:
    """Resolve and validate identity without writing files or creating runtimes."""

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
    return identity


def cleanup_external_browser_configuration(
    runtime_paths: RuntimeWorkspacePaths, identity: BrowserExecutionIdentity,
) -> None:
    """Remove only this exited task's generated init script and GUID markers."""
    config_root = runtime_paths.internal_workspace_dir.resolve() / "browser-config"
    task_root = config_root / identity.task.task_id
    if config_root.is_symlink() or task_root.is_symlink():
        raise ValueError("Browser configuration cleanup scope changed")
    if not task_root.exists():
        return
    state_root = task_root / "download-state"
    if state_root.is_symlink():
        raise ValueError("Browser download cleanup scope changed")
    if state_root.exists():
        for marker in state_root.iterdir():
            if marker.is_symlink() or not marker.is_file() or marker.suffix not in {".pending", ".failed", ".canceled", ".completed"}:
                raise ValueError("Browser download cleanup contains an unknown entry")
            marker.unlink()
        state_root.rmdir()
    script = task_root / "managed-download-init.cjs"
    if script.is_symlink():
        raise ValueError("Browser init cleanup scope changed")
    script.unlink(missing_ok=True)
    task_root.rmdir()


def build_external_browser_resources(
    *,
    request: SubagentBuildRequest,
    child_binding: ExecutionBinding,
    parent_subject_id: str,
    parent_session_id: str,
    channel_id: str,
    runtime_paths: RuntimeWorkspacePaths,
    admit: BrowserToolAdmission,
    artifact_sink: BrowserArtifactSink | None = None,
    decision_id_for: BrowserDecisionId | None = None,
) -> ExternalBrowserResources:
    """Build Browser identity and gateway before Provider/MCP side effects."""

    identity = build_external_browser_identity(
        request=request, child_binding=child_binding,
        parent_subject_id=parent_subject_id, parent_session_id=parent_session_id,
        channel_id=channel_id, runtime_paths=runtime_paths,
    )
    resolved = resolve_browser_capabilities(request.browser_capabilities)
    profile, instance, task = identity.profile, identity.instance, identity.task
    task_id = task.task_id
    workspace = instance.workspace
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
    _bind_product_playwright_runtime(
        mcp_config,
        user_workspace_dir=(
            runtime_paths.internal_workspace_dir.resolve() / "browser-runtime"
        ),
    )
    _bind_task_output_dir(
        mcp_config,
        outputs_root=Path(file_roots.outputs_root),
    )
    download_state_root = _bind_managed_download_init(
        mcp_config,
        internal_workspace_dir=runtime_paths.internal_workspace_dir,
        outputs_root=Path(file_roots.outputs_root),
        task_id=task_id,
    )
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
        f"Admitted Browser capabilities: {selected}. "
        "Write user-visible downloads, PDFs, traces, videos, screenshots, and "
        f"exports only below `{Path(file_roots.outputs_root).relative_to(Path(workspace))}`."
    )
    core_gateway = BrowserExecutionToolGateway(runtime, admit=admit, stop_on_close=True)
    gateway: BrowserExecutionToolGateway | ExternalBrowserArtifactGateway = core_gateway
    if artifact_sink is not None or decision_id_for is not None:
        if artifact_sink is None or decision_id_for is None:
            raise ValueError(
                "Browser Artifact sink and decision identity must be configured together"
            )
        gateway = ExternalBrowserArtifactGateway(
            core_gateway,
            workspace_root=Path(workspace),
            uploads_root=Path(file_roots.uploads_root),
            outputs_root=Path(file_roots.outputs_root),
            download_state_root=download_state_root,
            project_artifact=runtime.service.project_output_artifact,
            page_state=runtime.export_page_state,
            decision_id_for=decision_id_for,
            sink=artifact_sink,
        )
    return ExternalBrowserResources(
        gateway=gateway,
        system_prompt=prompt,
    )


__all__ = [
    "BROWSER_POLICY_REVISION",
    "ExternalBrowserResources",
    "build_external_browser_resources",
    "build_external_browser_identity",
]
