"""Capture only declared files after Runtime exit, without following workspace links."""

from __future__ import annotations

import hashlib
import difflib
import os
from pathlib import Path
import stat

from .store import CatalogError
from ..models import digest, relative_path

LIMIT = 1024 * 1024


def read_delivery(root: Path, name: str):
    relative_path(name)
    descriptors = []
    try:
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        descriptors.append(fd)
        parts = name.split("/")
        for part in parts[:-1]:
            fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            descriptors.append(fd)
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        descriptors.append(fd)
        before = os.fstat(fd)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_size > LIMIT
        ):
            raise CatalogError("INVALID_DELIVERY_FILE")
        chunks = []
        size = 0
        while data := os.read(fd, min(65536, LIMIT + 1 - size)):
            size += len(data)
            if size > LIMIT:
                raise CatalogError("DELIVERY_TOO_LARGE")
            chunks.append(data)
        after = os.fstat(fd)
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise CatalogError("DELIVERY_CHANGED_DURING_EXPORT")
        return b"".join(chunks), stat.S_IMODE(before.st_mode) & 0o777
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise CatalogError("UNSAFE_DELIVERY_PATH") from exc
    finally:
        for fd in reversed(descriptors):
            os.close(fd)


def capture(workspace: Path, task, destination: Path):
    """Apply a frozen baseline plus allowlisted changes to a fresh staging tree."""
    destination.mkdir(mode=0o700)
    initial = {item.path: item for item in task.files}
    for item in task.files:
        path = destination / item.path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(item.content, encoding="utf-8")
        path.chmod(0o755 if item.executable else 0o644)
    files = []
    total = 0
    for name in task.deliverables:
        value = read_delivery(workspace, name)
        path = destination / name
        if value is None:
            if name not in initial:
                raise CatalogError("MISSING_DELIVERY")
            path.unlink()
            files.append({"path": name, "status": "deleted", "mode": None})
            continue
        data, mode = value
        total += len(data)
        if total > 4 * LIMIT:
            raise CatalogError("DELIVERY_TOO_LARGE")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        path.chmod(mode)
        before = initial[name].content if name in initial else ""
        delta = "".join(
            difflib.unified_diff(
                before.splitlines(keepends=True),
                data.decode("utf-8", errors="replace").splitlines(keepends=True),
                fromfile="before/" + name,
                tofile="after/" + name,
            )
        )
        files.append(
            {
                "path": name,
                "status": "modified" if name in initial else "added",
                "bytes": len(data),
                "mode": mode,
                "sha256": hashlib.sha256(data).hexdigest(),
                "diff": delta[:32768],
                "diff_truncated": len(delta) > 32768,
            }
        )
    manifest = {
        "baseline_digest": digest(
            [item.model_dump(mode="json") for item in task.files]
        ),
        "files": files,
        "excluded_workspace_files": True,
    }
    return {**manifest, "sha256": digest(manifest)}
