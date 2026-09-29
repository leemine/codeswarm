# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Project Browser-created files through the existing product file service."""

from __future__ import annotations

import asyncio
import os
import stat
import uuid
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any

from openjiuwen.core.single_agent.schema.agent_result import Artifact
from openjiuwen.harness.tools.browser_move.playwright_runtime import (
    BrowserArtifactKind,
    BrowserExecutionIdentity,
    BrowserExecutionToolGateway,
)
from openjiuwen.harness_protocol import (
    ToolDefinition,
    ToolExecutionResult,
    ToolInvocation,
)

BrowserArtifactSink = Callable[[Artifact, Path], Awaitable[None]]
BrowserDecisionId = Callable[[BrowserExecutionIdentity, ToolInvocation], str]
BrowserArtifactProjector = Callable[..., Artifact]
BrowserPageState = Callable[[], Mapping[str, Any]]

_MAX_CHANGED_OUTPUTS = 32
_DOWNLOAD_START_GRACE_S = 0.5
_DOWNLOAD_SETTLE_TIMEOUT_S = 60.0
_SCREENSHOT_SUFFIXES = frozenset({".gif", ".jpeg", ".jpg", ".png", ".webp"})
_VIDEO_SUFFIXES = frozenset({".avi", ".m4v", ".mkv", ".mov", ".mp4", ".webm"})
_INPUT_FILENAME_TOOLS = frozenset(
    {
        "browser_run_code_unsafe",
        "browser_set_storage_state",
    }
)
_INPUT_PATHS_TOOLS = frozenset({"browser_drop", "browser_file_upload"})
_DOWNLOAD_TRIGGER_TOOLS = frozenset(
    {
        "browser_click",
        "browser_drag",
        "browser_evaluate",
        "browser_file_upload",
        "browser_fill_form",
        "browser_handle_dialog",
        "browser_hover",
        "browser_mouse_click_xy",
        "browser_mouse_down",
        "browser_mouse_drag_xy",
        "browser_mouse_up",
        "browser_mouse_wheel",
        "browser_navigate",
        "browser_navigate_back",
        "browser_press_key",
        "browser_run_code_unsafe",
        "browser_select_option",
        "browser_set_storage_state",
        "browser_tabs",
        "browser_type",
        "browser_wait_for",
    }
)


def _snapshot_outputs(root: Path) -> dict[Path, tuple[int, int, int, int]]:
    """Return non-symlink regular files without following directory links."""

    if not root.exists():
        return {}
    snapshot: dict[Path, tuple[int, int, int, int]] = {}
    for directory, directories, filenames in os.walk(root, followlinks=False):
        base = Path(directory)
        directories[:] = [
            name for name in directories if not (base / name).is_symlink()
        ]
        for filename in filenames:
            path = base / filename
            try:
                details = path.lstat()
            except OSError:
                continue
            if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode):
                continue
            snapshot[path] = (
                details.st_dev,
                details.st_ino,
                details.st_size,
                details.st_mtime_ns,
            )
    return snapshot


def _artifact_kind(path: Path, *, tool_name: str) -> BrowserArtifactKind:
    suffix = path.suffix.lower()
    normalized_tool = tool_name.strip().lower()
    parts = {part.lower() for part in path.parts}
    if normalized_tool == "browser_take_screenshot" or suffix in _SCREENSHOT_SUFFIXES:
        return BrowserArtifactKind.SCREENSHOT
    if normalized_tool == "browser_pdf_save" or suffix == ".pdf":
        return BrowserArtifactKind.PDF
    if "trace" in parts or "trace" in normalized_tool:
        return BrowserArtifactKind.TRACE
    if suffix in _VIDEO_SUFFIXES or "video" in normalized_tool:
        return BrowserArtifactKind.VIDEO
    if any("download" in part for part in parts):
        return BrowserArtifactKind.DOWNLOAD
    if not suffix:
        try:
            uuid.UUID(path.name)
        except ValueError:
            pass
        else:
            return BrowserArtifactKind.DOWNLOAD
    return BrowserArtifactKind.EXPORT


def _is_internal_mcp_output(path: Path, *, root: Path) -> bool:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return False
    return (
        len(relative.parts) == 1
        and relative.name.startswith("page-")
        and relative.suffix.lower() in {".yaml", ".yml"}
    )


def _download_markers(root: Path) -> tuple[frozenset[str], frozenset[str]]:
    pending: set[str] = set()
    terminal: set[str] = set()
    if not root.exists():
        return frozenset(), frozenset()
    for path in root.iterdir():
        try:
            details = path.lstat()
        except OSError:
            continue
        if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode):
            continue
        if path.suffix == ".pending":
            pending.add(path.name)
        elif path.suffix in {".canceled", ".failed"}:
            terminal.add(path.name)
    return frozenset(pending), frozenset(terminal)


def _controlled_path(
    value: object,
    *,
    workspace_root: Path,
    root: Path,
) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Browser file path must be a non-empty string")
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = workspace_root / candidate
    lexical = Path(os.path.abspath(candidate))
    if not _inside(lexical, root):
        raise ValueError("Browser file is outside the controlled root")
    current = root
    for part in lexical.relative_to(root).parts:
        current /= part
        if current.is_symlink():
            raise ValueError("Browser file path must not contain symlinks")
    resolved = lexical.resolve()
    if not _inside(resolved, root):
        raise ValueError("Browser file is outside the controlled root")
    return resolved


def _inside(path: Path, root: Path) -> bool:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return False
    return bool(relative.parts)


def _validate_input_file(value: object, *, workspace_root: Path, root: Path) -> None:
    path = _controlled_path(
        value,
        workspace_root=workspace_root,
        root=root,
    )
    if not path.is_file():
        raise ValueError("Browser input file is outside the controlled root")


def _validate_output_file(value: object, *, workspace_root: Path, root: Path) -> None:
    _controlled_path(
        value,
        workspace_root=workspace_root,
        root=root,
    )


class ExternalBrowserArtifactGateway:
    """Observe one task-scoped output root around serialized Browser calls.

    The wrapped core gateway remains the only Browser executor. This adapter
    merely discovers files created or changed by a successful invocation,
    asks the core projector to validate/hash them, and hands the resulting
    existing ``Artifact`` model to the product file service.
    """

    def __init__(
        self,
        gateway: BrowserExecutionToolGateway,
        *,
        workspace_root: Path,
        uploads_root: Path,
        outputs_root: Path,
        download_state_root: Path,
        project_artifact: BrowserArtifactProjector,
        page_state: BrowserPageState,
        decision_id_for: BrowserDecisionId,
        sink: BrowserArtifactSink,
    ) -> None:
        if not all(
            callable(value)
            for value in (project_artifact, page_state, decision_id_for, sink)
        ):
            raise TypeError("Browser Artifact callbacks must be callable")
        self._gateway = gateway
        self._identity = gateway.execution_identity
        self._workspace_root = workspace_root.resolve()
        self._uploads_root = uploads_root.resolve()
        self._outputs_root = outputs_root.resolve()
        self._download_state_root = download_state_root.resolve()
        self._project_artifact = project_artifact
        self._page_state = page_state
        self._decision_id_for = decision_id_for
        self._sink = sink
        self._delivered: set[str] = set()
        self._pending_artifacts: dict[str, tuple[Artifact, Path]] = {}
        self._closed = False
        self._invoke_lock = asyncio.Lock()

    @property
    def execution_identity(self) -> BrowserExecutionIdentity:
        return self._identity

    @property
    def closed(self) -> bool:
        return self._closed

    async def definitions(self) -> tuple[ToolDefinition, ...]:
        return await self._gateway.definitions()

    async def invoke(self, invocation: ToolInvocation) -> ToolExecutionResult:
        async with self._invoke_lock:
            try:
                await self._flush_artifacts()
            except Exception:
                return ToolExecutionResult(content="Browser Artifact delivery is pending", is_error=True)
            try:
                self._validate_file_scope(invocation)
            except (OSError, TypeError, ValueError):
                return ToolExecutionResult(
                    content="Browser file scope validation failed",
                    is_error=True,
                )
            before = _snapshot_outputs(self._outputs_root)
            marker_before = _download_markers(self._download_state_root)
            if marker_before[0]:
                return ToolExecutionResult(
                    content="Browser download is still pending",
                    is_error=True,
                )
            result = await self._gateway.invoke(invocation)
            if result.is_error:
                return result
            settlement_error = await self._wait_for_downloads(
                invocation,
                before_markers=marker_before,
            )
            if settlement_error is not None:
                return settlement_error
            after = _snapshot_outputs(self._outputs_root)
            changed = sorted(
                path
                for path, stamp in after.items()
                if before.get(path) != stamp
                and not _is_internal_mcp_output(path, root=self._outputs_root)
            )
            if len(changed) > _MAX_CHANGED_OUTPUTS:
                return ToolExecutionResult(
                    content="Browser Artifact projection failed: output limit exceeded",
                    is_error=True,
                )
            if not changed:
                return result
            page_state = self._page_state()
            source_url = str(page_state.get("url") or "about:blank")
            permission_id = self._decision_id_for(self._identity, invocation)
            try:
                for path in changed:
                    artifact = self._project_artifact(
                        path,
                        kind=_artifact_kind(path, tool_name=invocation.name),
                        source_url=source_url,
                        tool_name=invocation.name,
                        permission_decision_id=permission_id,
                    )
                    artifact_id = str(artifact.artifactId or "").strip()
                    if not artifact_id:
                        raise ValueError("Browser Artifact id is required")
                    if artifact_id in self._delivered:
                        continue
                    self._pending_artifacts[artifact_id] = (artifact, path)
                await self._flush_artifacts()
            except Exception as exc:  # noqa: BLE001 - stable product boundary
                return ToolExecutionResult(
                    content=(
                        "Browser Artifact projection failed: "
                        f"{type(exc).__name__}"
                    ),
                    is_error=True,
                )
            return result

    async def _wait_for_downloads(
        self,
        invocation: ToolInvocation,
        *,
        before_markers: tuple[frozenset[str], frozenset[str]],
    ) -> ToolExecutionResult | None:
        if invocation.name.strip().lower() not in _DOWNLOAD_TRIGGER_TOOLS:
            return None
        loop = asyncio.get_running_loop()
        started = loop.time()
        while True:
            pending, terminal = _download_markers(self._download_state_root)
            if terminal - before_markers[1]:
                return ToolExecutionResult(
                    content="Browser download failed",
                    is_error=True,
                )
            if not pending and (loop.time() - started) >= _DOWNLOAD_START_GRACE_S:
                return None
            if (loop.time() - started) >= _DOWNLOAD_SETTLE_TIMEOUT_S:
                return ToolExecutionResult(
                    content="Browser download did not settle",
                    is_error=True,
                )
            await asyncio.sleep(0.05)

    def _validate_file_scope(self, invocation: ToolInvocation) -> None:
        arguments = invocation.arguments
        if not isinstance(arguments, Mapping):
            return
        tool_name = invocation.name.strip().lower()
        if tool_name in _INPUT_PATHS_TOOLS and "paths" in arguments:
            paths = arguments["paths"]
            if not isinstance(paths, (list, tuple)):
                raise TypeError("Browser input paths must be a list")
            for value in paths:
                _validate_input_file(
                    value,
                    workspace_root=self._workspace_root,
                    root=self._uploads_root,
                )
        filename = arguments.get("filename")
        if filename is None:
            return
        if tool_name in _INPUT_FILENAME_TOOLS:
            _validate_input_file(
                filename,
                workspace_root=self._workspace_root,
                root=self._uploads_root,
            )
            return
        _validate_output_file(
            filename,
            workspace_root=self._workspace_root,
            root=self._outputs_root,
        )

    async def _flush_artifacts(self) -> None:
        for artifact_id, (artifact, path) in tuple(self._pending_artifacts.items()):
            await self._sink(artifact, path)
            self._delivered.add(artifact_id)
            self._pending_artifacts.pop(artifact_id)

    async def close(self) -> None:
        async with self._invoke_lock:
            await self._gateway.close()
            # The production core gateway confirms Chrome exit before return.
            # Mark interrupted downloads terminal only after that confirmation.
            if self._download_state_root.exists():
                for path in self._download_state_root.glob("*.pending"):
                    if path.is_symlink():
                        raise ValueError("Browser download marker must not be a symlink")
                    path.rename(path.with_suffix(".canceled"))
            await self._flush_artifacts()
            self._closed = True


__all__ = [
    "BrowserArtifactSink",
    "BrowserDecisionId",
    "ExternalBrowserArtifactGateway",
]
