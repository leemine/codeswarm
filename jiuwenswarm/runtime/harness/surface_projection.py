# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Project normalized harness items onto existing Surface product models."""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
import stat
import threading
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from openjiuwen.core.single_agent.schema.agent_result import Artifact, Part
from openjiuwen.harness_protocol import (
    HarnessEvent,
    ItemEventKind,
    ItemLifecycleEvent,
    TurnEventKind,
    TurnLifecycleEvent,
    json_value_to_builtin,
)

from jiuwenswarm.server.runtime.session.history_io import run_history_io

ArtifactSink = Callable[[Artifact, Path], Awaitable[None]]

_MAX_CHANGED_OUTPUTS = 32
_MAX_ACTIVITIES_PER_TURN = 2048
_MAX_TRACKED_FILES_PER_TURN = 256
_MAX_TRACKED_FILE_BYTES = 5 * 1024 * 1024
_MAX_TRACKED_TEXT_BYTES_PER_TURN = 16 * 1024 * 1024
_MAX_PATHS_PER_ITEM = 32
_FAILED_STATUSES = frozenset(
    {"blocked", "declined", "denied", "error", "failed", "failure", "rejected"}
)
_FILE_CHANGE_TOOLS = frozenset(
    {
        "apply_patch",
        "delete_file",
        "edit",
        "edit_file",
        "move_file",
        "patch",
        "rename_file",
        "write",
        "write_file",
    }
)
_NAVIGATION_TOOLS = frozenset(
    {
        "find",
        "glob",
        "grep",
        "list",
        "list_files",
        "lsp",
        "read",
        "read_file",
        "search",
        "symbol",
    }
)
_TERMINAL_TOOLS = frozenset(
    {"bash", "command", "command_execution", "powershell", "shell", "terminal"}
)
_TEST_COMMAND = re.compile(
    r"(?:^|[;&|\s])(?:cargo\s+test|dotnet\s+test|go\s+test|gradle\w*\s+test|"
    r"mvn\w*\s+test|npm\s+(?:run\s+)?test|pnpm\s+(?:run\s+)?test|"
    r"pytest|python(?:3(?:\.\d+)?)?\s+-m\s+(?:pytest|unittest)|yarn\s+test)(?:\s|$)",
    re.IGNORECASE,
)
_REVIEW_COMMAND = re.compile(
    r"(?:^|[;&|\s])(?:gh\s+pr\s+(?:diff|review|checks)|git\s+show)(?:\s|$)",
    re.IGNORECASE,
)
_DIFF_COMMAND = re.compile(
    r"(?:^|[;&|\s])git\s+(?:diff|status)(?:\s|$)", re.IGNORECASE
)
_FILE_HISTORY_LOCK = threading.Lock()


class SurfaceActivityKind(str, Enum):
    ARTIFACT = "artifact"
    BROWSER = "browser"
    CODE_NAVIGATION = "code_navigation"
    DIFF = "diff"
    FILE_CHANGE = "file_change"
    REVIEW = "review"
    SUBAGENT = "subagent"
    TERMINAL = "terminal"
    TEST = "test"
    WEB = "web"


@dataclass(frozen=True, slots=True)
class SurfaceActivity:
    """Provider-neutral product meaning for one normalized tool item."""

    item_id: str
    kind: SurfaceActivityKind
    phase: str
    tool_name: str
    surface: str
    status: str = ""
    paths: tuple[str, ...] = ()
    schema_version: int = 1

    def record(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "kind": self.kind.value,
            "phase": self.phase,
            "surface": self.surface,
            "tool_name": self.tool_name,
            "item_id": self.item_id,
            **({"status": self.status} if self.status else {}),
            **({"paths": list(self.paths)} if self.paths else {}),
        }


@dataclass(frozen=True, slots=True)
class SurfaceProjectionSummary:
    """Terminal audit attached to the authoritative Provider outcome."""

    status: str
    activities: int = 0
    file_operations: int = 0
    artifacts: int = 0
    errors: tuple[str, ...] = ()
    schema_version: int = 1

    def record(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "status": self.status,
            "activities": self.activities,
            "file_operations": self.file_operations,
            "artifacts": self.artifacts,
            **({"errors": list(self.errors)} if self.errors else {}),
        }


@dataclass(slots=True)
class _FileMutation:
    path: Path
    old_content: str | None
    new_content: str | None
    projection_id: str
    timestamp: float


@dataclass(slots=True)
class _TurnState:
    output_snapshot: dict[Path, tuple[int, int, int, int]] = field(default_factory=dict)
    activities: dict[str, SurfaceActivity] = field(default_factory=dict)
    started_files: dict[str, dict[Path, str | None]] = field(default_factory=dict)
    mutations: dict[str, _FileMutation] = field(default_factory=dict)
    summary: SurfaceProjectionSummary | None = None
    tracked_files: int = 0
    tracked_text_bytes: int = 0
    budget_exhausted: bool = False


class SurfaceResultProjection:
    """One-cycle typed projection over the existing event/history consumers.

    Tool completion only stages mutations. File history and Work artifacts are
    committed after an authoritative successful Provider terminal.
    """

    def __init__(
        self,
        session_id: str,
        *,
        work_mode: str,
        workspace_root: Path,
        cwd: Path,
        outputs_dir: Path | None,
        provider_id: str,
        artifact_sink: ArtifactSink,
        require_artifact_attribution: bool = False,
    ) -> None:
        if work_mode not in {"work", "code"}:
            raise ValueError("Surface projection requires work or code mode")
        self._session_id = session_id
        self._work_mode = work_mode
        self._workspace_root = workspace_root.resolve()
        self._cwd = cwd.resolve()
        self._outputs_dir = outputs_dir.resolve() if outputs_dir is not None else None
        self._provider_id = provider_id
        self._artifact_sink = artifact_sink
        self._require_artifact_attribution = require_artifact_attribution
        self._turns: dict[str, _TurnState] = {}
        # Retain the immutable projected activity records until the existing
        # output owner consumes the terminal marker. The Provider event pump
        # may enqueue tool-result and terminal outputs before the UI consumer
        # has read either one.
        self._completed: dict[str, _TurnState] = {}

    def register_turn(self, turn_id: str) -> None:
        if not turn_id or turn_id in self._turns or turn_id in self._completed:
            return
        snapshot = (
            _snapshot_regular_files(self._outputs_dir)
            if self._work_mode == "work" and self._outputs_dir is not None
            else {}
        )
        self._turns[turn_id] = _TurnState(output_snapshot=snapshot)

    async def observe(self, envelope: HarnessEvent) -> None:
        turn_id = envelope.turn_id
        if not turn_id or turn_id in self._completed:
            return
        state = self._turns.get(turn_id)
        if state is None:
            self.register_turn(turn_id)
            state = self._turns.get(turn_id)
        if state is None:
            return
        event = envelope.event
        if isinstance(event, ItemLifecycleEvent):
            self._observe_item(state, envelope, event)
            return
        if not isinstance(event, TurnLifecycleEvent):
            return
        if event.kind is TurnEventKind.FINISHED:
            await self._finalize_success(turn_id, state)
        elif event.kind in {TurnEventKind.ABORTED, TurnEventKind.FAILED}:
            state.summary = SurfaceProjectionSummary(
                status="discarded",
                activities=len(state.activities),
            )
            self._completed[turn_id] = state
            self._turns.pop(turn_id, None)

    def activity(self, turn_id: str, item_id: str) -> SurfaceActivity | None:
        state = self._turns.get(turn_id) or self._completed.get(turn_id)
        return state.activities.get(item_id) if state is not None else None

    def summary(self, turn_id: str) -> SurfaceProjectionSummary | None:
        state = self._turns.get(turn_id)
        if state is not None and state.summary is not None:
            return state.summary
        completed = self._completed.get(turn_id)
        return completed.summary if completed is not None else None

    def forget(self, turn_id: str) -> None:
        self._turns.pop(turn_id, None)
        self._completed.pop(turn_id, None)

    def close(self) -> None:
        self._turns.clear()
        self._completed.clear()

    def _observe_item(
        self,
        state: _TurnState,
        envelope: HarnessEvent,
        event: ItemLifecycleEvent,
    ) -> None:
        if event.item_type != "tool" or not envelope.item_id:
            return
        data = json_value_to_builtin(event.data)
        if not isinstance(data, dict):
            return
        if (
            envelope.item_id not in state.activities
            and len(state.activities) >= _MAX_ACTIVITIES_PER_TURN
        ):
            state.budget_exhausted = True
            return
        tool_name = str(data.get("name") or data.get("tool_name") or "unknown")
        arguments = data.get("arguments")
        status = _item_status(data)
        kind = classify_surface_activity(tool_name, arguments)
        paths = _candidate_paths(arguments)
        activity = SurfaceActivity(
            item_id=envelope.item_id,
            kind=kind,
            phase=event.kind.value,
            tool_name=tool_name,
            surface=self._work_mode,
            status=status,
            paths=tuple(path.as_posix() for path in paths),
        )
        state.activities[envelope.item_id] = activity
        if self._work_mode != "code" or kind is not SurfaceActivityKind.FILE_CHANGE:
            return
        if event.kind is ItemEventKind.STARTED:
            baseline: dict[Path, str | None] = {}
            for raw_path in paths[:_MAX_PATHS_PER_ITEM]:
                if state.tracked_files >= _MAX_TRACKED_FILES_PER_TURN:
                    state.budget_exhausted = True
                    break
                controlled = self._controlled_path(raw_path)
                if controlled is None:
                    continue
                readable, content = _read_text_snapshot(controlled)
                if readable:
                    content_bytes = _text_bytes(content)
                    if (
                        state.tracked_text_bytes + content_bytes
                        > _MAX_TRACKED_TEXT_BYTES_PER_TURN
                    ):
                        state.budget_exhausted = True
                        continue
                    state.tracked_text_bytes += content_bytes
                    baseline[controlled] = content
                    state.tracked_files += 1
            if baseline:
                state.started_files[envelope.item_id] = baseline
            return
        if event.kind is not ItemEventKind.COMPLETED or _item_failed(data):
            return
        baseline = state.started_files.pop(envelope.item_id, {})
        for path, old_content in baseline.items():
            readable, new_content = _read_text_snapshot(path)
            if not readable or old_content == new_content:
                continue
            content_bytes = _text_bytes(new_content)
            if (
                state.tracked_text_bytes + content_bytes
                > _MAX_TRACKED_TEXT_BYTES_PER_TURN
            ):
                state.budget_exhausted = True
                continue
            state.tracked_text_bytes += content_bytes
            projection_id = _stable_id(
                "surface-file-op",
                self._session_id,
                envelope.turn_id or "",
                envelope.item_id,
                path.as_posix(),
                hashlib.sha256((new_content or "").encode("utf-8")).hexdigest(),
            )
            state.mutations[projection_id] = _FileMutation(
                path=path,
                old_content=old_content,
                new_content=new_content,
                projection_id=projection_id,
                timestamp=envelope.timestamp,
            )

    async def _finalize_success(self, turn_id: str, state: _TurnState) -> None:
        if state.summary is not None:
            return
        errors: list[str] = (
            ["surface_projection_budget_exhausted"]
            if state.budget_exhausted
            else []
        )
        file_operations = 0
        artifacts = 0
        if self._work_mode == "code" and state.mutations:
            try:
                file_operations = await run_history_io(
                    _append_file_operations,
                    self._file_history_path(),
                    tuple(state.mutations.values()),
                )
            except Exception:
                errors.append("file_history_unconfirmed")
        if self._work_mode == "work" and self._outputs_dir is not None:
            try:
                changed = self._changed_outputs(state.output_snapshot)
                if self._require_artifact_attribution:
                    # Team members share the output directory. A directory
                    # delta alone cannot identify which member produced a file.
                    attributed = {
                        self._controlled_path(Path(path))
                        for activity in state.activities.values()
                        if activity.phase == ItemEventKind.COMPLETED.value
                        and activity.kind in {SurfaceActivityKind.FILE_CHANGE, SurfaceActivityKind.ARTIFACT}
                        and activity.status not in _FAILED_STATUSES
                        for path in activity.paths
                    }
                    changed = tuple(path for path in changed if path in attributed)
                for path in changed:
                    await self._artifact_sink(
                        _project_output_artifact(
                            path,
                            workspace_root=self._workspace_root,
                            provider_id=self._provider_id,
                            session_id=self._session_id,
                            turn_id=turn_id,
                        ),
                        path,
                    )
                    artifacts += 1
            except Exception:
                errors.append("artifact_persistence_unconfirmed")
        state.summary = SurfaceProjectionSummary(
            status="unconfirmed" if errors else "confirmed",
            activities=len(state.activities),
            file_operations=file_operations,
            artifacts=artifacts,
            errors=tuple(errors),
        )
        self._completed[turn_id] = state
        self._turns.pop(turn_id, None)

    def _changed_outputs(
        self,
        before: Mapping[Path, tuple[int, int, int, int]],
    ) -> tuple[Path, ...]:
        root = self._outputs_dir
        if root is None:
            return ()
        after = _snapshot_regular_files(root)
        changed = tuple(sorted(path for path, stamp in after.items() if before.get(path) != stamp))
        if len(changed) > _MAX_CHANGED_OUTPUTS:
            raise ValueError("Surface Artifact output limit exceeded")
        return changed

    def _controlled_path(self, raw_path: Path) -> Path | None:
        candidate = raw_path if raw_path.is_absolute() else self._cwd / raw_path
        lexical = Path(os.path.abspath(os.path.normpath(str(candidate))))
        try:
            lexical.relative_to(self._workspace_root)
        except ValueError:
            return None
        current = self._workspace_root
        for part in lexical.relative_to(self._workspace_root).parts[:-1]:
            current /= part
            if current.is_symlink():
                return None
        if lexical.is_symlink():
            return None
        try:
            resolved = lexical.resolve(strict=lexical.exists())
            resolved.relative_to(self._workspace_root)
        except (OSError, ValueError):
            return None
        return resolved

    def _file_history_path(self) -> Path:
        return (
            self._workspace_root
            / ".agent_history"
            / f"file_ops_external_{self._session_id}.json"
        )


def classify_surface_activity(
    tool_name: str,
    arguments: object = None,
) -> SurfaceActivityKind:
    """Classify only normalized tool names/arguments, never raw Provider events."""

    name = tool_name.strip().lower().rsplit(".", 1)[-1]
    if name.startswith("browser_") or name.startswith("browser-"):
        return SurfaceActivityKind.BROWSER
    if name.startswith("subagent_") or name.startswith("subagent-"):
        return SurfaceActivityKind.SUBAGENT
    if name in _FILE_CHANGE_TOOLS:
        return SurfaceActivityKind.FILE_CHANGE
    if name in _NAVIGATION_TOOLS or name.startswith("lsp_"):
        return SurfaceActivityKind.CODE_NAVIGATION
    if name in {"send_file_to_user", "upload_file", "upload_photo"}:
        return SurfaceActivityKind.ARTIFACT
    if "review" in name:
        return SurfaceActivityKind.REVIEW
    if "test" in name:
        return SurfaceActivityKind.TEST
    if name in {"web_fetch", "web_search", "fetch", "search_web"}:
        return SurfaceActivityKind.WEB
    if name in _TERMINAL_TOOLS:
        command = _command_text(arguments)
        if _TEST_COMMAND.search(command):
            return SurfaceActivityKind.TEST
        if _REVIEW_COMMAND.search(command):
            return SurfaceActivityKind.REVIEW
        if _DIFF_COMMAND.search(command):
            return SurfaceActivityKind.DIFF
        return SurfaceActivityKind.TERMINAL
    return SurfaceActivityKind.TERMINAL


def _command_text(arguments: object) -> str:
    if isinstance(arguments, Mapping):
        value = arguments.get("command") or arguments.get("cmd")
    else:
        value = None
    if isinstance(value, (list, tuple)):
        return " ".join(str(part) for part in value)
    return str(value or "")


def _candidate_paths(arguments: object) -> list[Path]:
    if not isinstance(arguments, Mapping):
        return []
    raw: list[object] = []
    changes = arguments.get("changes")
    if isinstance(changes, (list, tuple)):
        for change in changes:
            if not isinstance(change, Mapping):
                continue
            raw.append(change.get("path"))
            kind = change.get("kind")
            if isinstance(kind, Mapping):
                raw.append(kind.get("move_path") or kind.get("movePath"))
    for key in ("file_path", "filePath", "filename", "path"):
        raw.append(arguments.get(key))
    paths = arguments.get("paths")
    if isinstance(paths, (list, tuple)):
        raw.extend(paths)
    result: list[Path] = []
    seen: set[str] = set()
    for value in raw:
        if not isinstance(value, str) or not value.strip():
            continue
        normalized = value.strip()
        if normalized in seen:
            continue
        seen.add(normalized)
        result.append(Path(normalized))
    return result[:_MAX_PATHS_PER_ITEM]


def _item_status(data: Mapping[str, Any]) -> str:
    value = data.get("status")
    if not isinstance(value, str):
        provider = data.get("opencode")
        value = provider.get("status") if isinstance(provider, Mapping) else ""
    return str(value or "").strip().lower()


def _item_failed(data: Mapping[str, Any]) -> bool:
    status = _item_status(data)
    return status in _FAILED_STATUSES or bool(data.get("error"))


def _snapshot_regular_files(root: Path | None) -> dict[Path, tuple[int, int, int, int]]:
    if root is None or not root.exists():
        return {}
    snapshot: dict[Path, tuple[int, int, int, int]] = {}
    for directory, directories, filenames in os.walk(root, followlinks=False):
        base = Path(directory)
        directories[:] = [name for name in directories if not (base / name).is_symlink()]
        for filename in filenames:
            path = base / filename
            try:
                details = path.lstat()
            except OSError:
                continue
            if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode):
                continue
            snapshot[path.resolve()] = (
                details.st_dev,
                details.st_ino,
                details.st_size,
                details.st_mtime_ns,
            )
    return snapshot


def _read_text_snapshot(path: Path) -> tuple[bool, str | None]:
    if not path.exists():
        return True, None
    try:
        details = path.lstat()
        if (
            stat.S_ISLNK(details.st_mode)
            or not stat.S_ISREG(details.st_mode)
            or details.st_size > _MAX_TRACKED_FILE_BYTES
        ):
            return False, None
        raw = path.read_bytes()
        if b"\x00" in raw:
            return False, None
        return True, raw.decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return False, None


def _text_bytes(content: str | None) -> int:
    return len(content.encode("utf-8")) if content is not None else 0


def _append_file_operations(path: Path, mutations: tuple[_FileMutation, ...]) -> int:
    """Append compatible file_ops rows idempotently by projection id."""

    with _FILE_HISTORY_LOCK:
        history: dict[str, list[dict[str, Any]]] = {}
        if path.exists():
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(loaded, dict):
                raise ValueError("file operation history is not an object")
            history = loaded
        appended = 0
        for mutation in mutations:
            key = mutation.path.as_posix()
            entries = history.setdefault(key, [])
            if not isinstance(entries, list):
                raise ValueError("file operation history entry is not a list")
            if any(row.get("projection_id") == mutation.projection_id for row in entries if isinstance(row, dict)):
                continue
            entries.append(
                {
                    "action": (
                        "delete"
                        if mutation.new_content is None
                        else "write"
                        if mutation.old_content is None
                        else "edit"
                    ),
                    "timestamp": datetime.fromtimestamp(
                        mutation.timestamp, tz=timezone.utc
                    ).isoformat(),
                    "old_content": mutation.old_content,
                    "new_content": mutation.new_content,
                    "projection_id": mutation.projection_id,
                }
            )
            if len(entries) > 100:
                history[key] = entries[-100:]
            appended += 1
        if appended:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(path.suffix + ".tmp")
            temporary.write_text(
                json.dumps(history, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(temporary, path)
        return appended


def _project_output_artifact(
    path: Path,
    *,
    workspace_root: Path,
    provider_id: str,
    session_id: str,
    turn_id: str,
) -> Artifact:
    lexical = Path(os.path.abspath(os.path.normpath(str(path))))
    resolved = path.resolve(strict=True)
    if lexical != resolved or resolved.is_symlink() or not resolved.is_file():
        raise ValueError("Surface Artifact must be a non-symlink regular file")
    relative = resolved.relative_to(workspace_root).as_posix()
    digest = hashlib.sha256()
    size = 0
    with resolved.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            size += len(chunk)
            digest.update(chunk)
    sha256 = digest.hexdigest()
    artifact_id = _stable_id(
        "surface-output", session_id, turn_id, relative, sha256
    )
    mime_type = mimetypes.guess_type(resolved.name)[0] or "application/octet-stream"
    kind = (
        "media"
        if mime_type.startswith(("image/", "audio/", "video/"))
        else "document"
        if mime_type.startswith("text/")
        or mime_type
        in {
            "application/pdf",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        }
        else "file"
    )
    metadata: dict[str, Any] = {
        "kind": kind,
        "producer": "surface_projection",
        "provider_id": provider_id,
        "session_id": session_id,
        "turn_id": turn_id,
        "workspace_relative_path": relative,
        "mime_type": mime_type,
        "size_bytes": size,
        "sha256": sha256,
    }
    return Artifact(
        artifactId=f"surface-output-{artifact_id}",
        name=resolved.name,
        description=f"Work Surface {kind} output",
        parts=[
            Part(
                url=relative,
                filename=resolved.name,
                media_type=mime_type,
                metadata={"sha256": sha256, "size_bytes": size},
            )
        ],
        metadata=metadata,
    )


def _stable_id(*parts: str) -> str:
    return hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()


__all__ = [
    "SurfaceActivity",
    "SurfaceActivityKind",
    "SurfaceProjectionSummary",
    "SurfaceResultProjection",
    "classify_surface_activity",
]
