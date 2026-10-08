"""Bounded delivery evidence; original Session history stays with the Runtime."""

from __future__ import annotations

import difflib
import hashlib
from pathlib import Path

from .adapters.store import CatalogError
from .adapters.verification import workspace_file


def delivery_evidence(workspace: Path, task):
    initial = {item.path: item.content for item in task.files}
    files = []
    for name in dict.fromkeys([*initial, *task.deliverables]):
        path = workspace_file(workspace, name)
        before = initial.get(name, "")
        if not path.exists():
            files.append(
                {"path": name, "status": "deleted" if name in initial else "missing"}
            )
            continue
        if not path.is_file() or path.stat().st_size > 262144:
            raise CatalogError("DELIVERY_TOO_LARGE_OR_NOT_FILE")
        data = path.read_bytes()
        after = data.decode("utf-8", errors="replace")
        delta = "".join(
            difflib.unified_diff(
                before.splitlines(keepends=True),
                after.splitlines(keepends=True),
                fromfile="before/" + name,
                tofile="after/" + name,
            )
        )
        files.append(
            {
                "path": name,
                "status": "unchanged"
                if before == after and name in initial
                else "modified"
                if name in initial
                else "added",
                "sha256": hashlib.sha256(data).hexdigest(),
                "bytes": len(data),
                "diff": delta[:32768],
                "diff_truncated": len(delta) > 32768,
            }
        )
    return files


def statistics(experiment):
    # Independent Trial denominator is frozen; retries never replace the first sample.
    counts = {}
    for trial in experiment["trials"]:
        first = trial["attempts"][0]
        result = first["body"].get("outcome", first["phase"])
        if first["phase"] == "unknown":
            result = "unknown"
        counts[result] = counts.get(result, 0) + 1
    total = len(experiment["trials"])
    return {
        "planned_trials": total,
        "first_attempt_outcomes": counts,
        "passed": counts.get("passed", 0),
        "denominator": total,
        "all_settled": all(
            t["attempts"][0]["phase"] == "settled" for t in experiment["trials"]
        ),
        "usage": None,
        "usage_coverage": "unknown",
        "cost": None,
    }


def safe_diagnostic(message):
    """Reuse host log redaction and remove configured secret values before storage."""
    import os
    from jiuwenswarm.common.config import get_config
    from jiuwenswarm.common.utils import _sanitize_log_text

    text = str(message)

    def redact(value):
        nonlocal text
        if isinstance(value, dict):
            for key, item in value.items():
                if any(
                    part in str(key).lower()
                    for part in ("api_key", "secret", "token", "password")
                ) and isinstance(item, str):
                    for candidate in (item, os.path.expandvars(item)):
                        if len(candidate) > 3:
                            text = text.replace(candidate, "[REDACTED]")
                else:
                    redact(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                redact(item)

    redact(get_config())
    return _sanitize_log_text(text)[:2000]


def implementation_source():
    import subprocess
    import jiuwenswarm
    from .models import digest

    package = Path(jiuwenswarm.__file__).parent
    plugin = Path(__file__).parents[1]
    files = list(plugin.rglob("*.py")) + [
        package / path
        for path in (
            "runtime/service.py",
            "runtime/plan.py",
            "server/runtime/agent_adapter/interface.py",
        )
    ]
    fingerprints = {
        str(path.relative_to(package)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in files
        if path.is_file()
    }
    source = {"sha256": digest(fingerprints), "package_path": str(package)}
    root = package.parent
    if (root / ".git").exists():
        try:
            source["git_head"] = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=root, timeout=3, text=True
            ).strip()
            source["dirty"] = bool(
                subprocess.check_output(
                    ["git", "status", "--porcelain"], cwd=root, timeout=3, text=True
                ).strip()
            )
        except (subprocess.SubprocessError, OSError):
            source["git_head"] = None
    return source
