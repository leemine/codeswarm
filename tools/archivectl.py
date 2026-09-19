#!/usr/bin/env python3
"""Inspect, compare, and enforce retention for local testctl archives."""

from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path
import shutil
from testctl import aggregate, empty_counts, parse_junit, write_summary_markdown


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ARCHIVE = REPO_ROOT / "artifacts" / "test-runs"


def load_summary(run_dir: Path) -> dict | None:
    path = run_dir / "summary.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def runs(archive: Path) -> list[tuple[Path, dict]]:
    if not archive.exists():
        return []
    found = []
    for path in sorted(archive.iterdir()):
        if path.is_dir() and (summary := load_summary(path)) is not None:
            found.append((path, summary))
    return found


def parse_time(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def list_runs(archive: Path) -> int:
    payload = [
        {
            "run_id": summary.get("run_id", path.name),
            "created_at": summary.get("created_at"),
            "profile": summary.get("profile"),
            "status": summary.get("status"),
            "retention_days": summary.get("retention_days"),
            "path": str(path),
        }
        for path, summary in runs(archive)
    ]
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def prune(archive: Path, execute: bool, now: dt.datetime) -> int:
    expired = []
    for path, summary in runs(archive):
        if path.is_symlink() or path.resolve().parent != archive.resolve() or summary.get("run_id") != path.name:
            raise SystemExit(f"unsafe archive entry, refusing prune: {path}")
        created = parse_time(summary["created_at"])
        configured_retention = summary.get("retention_days", 30)
        if configured_retention is None:
            continue
        retention_days = int(configured_retention)
        if retention_days <= 0:
            raise SystemExit(f"invalid retention_days in {path}: {retention_days}")
        expires_at = created + dt.timedelta(days=retention_days)
        if expires_at <= now:
            expired.append(
                {
                    "run_id": summary.get("run_id", path.name),
                    "path": str(path),
                    "expires_at": expires_at.isoformat(),
                }
            )
            if execute:
                shutil.rmtree(path)
    print(json.dumps({"execute": execute, "expired": expired}, ensure_ascii=False, indent=2))
    return 0


def compare(base: Path, candidate: Path) -> int:
    left = load_summary(base)
    right = load_summary(candidate)
    if left is None or right is None:
        raise SystemExit("both run directories must contain summary.json")
    keys = ("passed", "failed", "error", "timeout", "skipped", "blocked", "not_run", "flaky_pass")
    delta = {key: int(right["totals"].get(key, 0)) - int(left["totals"].get(key, 0)) for key in keys}
    payload = {
        "base_run_id": left.get("run_id"),
        "candidate_run_id": right.get("run_id"),
        "base_status": left.get("status"),
        "candidate_status": right.get("status"),
        "delta": delta,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    regressions = delta["failed"] + delta["error"] + delta["timeout"] + delta["blocked"]
    return 1 if regressions > 0 else 0


def recover(run_dir: Path, execute: bool) -> int:
    if (run_dir / "summary.json").exists():
        raise SystemExit(f"refusing to overwrite completed run: {run_dir}")
    manifest_file = run_dir / "manifest.json"
    plan_file = run_dir / "plan.json"
    if not manifest_file.exists() or not plan_file.exists():
        raise SystemExit("interrupted run must contain manifest.json and plan.json")
    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    plan = json.loads(plan_file.read_text(encoding="utf-8"))
    inventory_file = run_dir / "inventory.json"
    inventory = json.loads(inventory_file.read_text(encoding="utf-8")) if inventory_file.exists() else {"suites": []}
    collected = {entry["suite_id"]: entry for entry in inventory["suites"]}
    suites_by_id = {suite["id"]: suite for suite in manifest["source_manifest"]["suites"]}
    results = []
    for entry in plan["suites"]:
        suite_id = entry["suite_id"]
        suite = suites_by_id[suite_id]
        counts = empty_counts()
        junit = run_dir / "junit" / f"{suite_id}.xml"
        cases = parse_junit(junit) if junit.exists() else []
        if entry["decision"] == "blocked":
            status = "blocked"
            counts[status] = 1
        elif junit.exists() and cases:
            for case in cases:
                counts[case["state"]] += 1
            status = next((state for state in ("failed", "error", "timeout") if counts[state]), "passed")
            expected = collected.get(suite_id, {})
            if expected.get("granularity") == "test" and expected.get("count", 0) > len(cases):
                counts["not_run"] = expected["count"] - len(cases)
                status = "not_run"
        else:
            status = "not_run"
            expected = collected.get(suite_id, {})
            counts[status] = expected.get("count", 1) if expected.get("granularity") == "test" else 1
        results.append(
            {
                "suite_id": suite_id,
                "owner": suite.get("owner"),
                "tier": suite.get("tier"),
                "required": entry.get("required", True),
                "status": status,
                "counts": counts,
                "cases": cases,
                "recovered_from_partial_archive": True,
            }
        )
    summary = aggregate(
        manifest["run_id"],
        plan.get("profile"),
        results,
        manifest.get("parent_run_id"),
        int(manifest["source_manifest"]["profiles"].get(plan.get("profile"), {}).get("retention_days", 30)),
    )
    summary["recovered_from_partial_archive"] = True
    summary["network_isolation"] = "unknown-interrupted-run"
    if execute:
        (run_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        write_summary_markdown(summary, run_dir / "summary.md")
    print(json.dumps({"execute": execute, "run_id": summary["run_id"], "status": summary["status"], "totals": summary["totals"]}, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, default=DEFAULT_ARCHIVE)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list")
    prune_parser = commands.add_parser("prune")
    prune_parser.add_argument("--execute", action="store_true")
    prune_parser.add_argument("--now", help="ISO timestamp override for deterministic validation")
    compare_parser = commands.add_parser("compare")
    compare_parser.add_argument("base", type=Path)
    compare_parser.add_argument("candidate", type=Path)
    recover_parser = commands.add_parser("recover")
    recover_parser.add_argument("run_dir", type=Path)
    recover_parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if args.command == "list":
        return list_runs(args.archive)
    if args.command == "prune":
        now = parse_time(args.now) if args.now else dt.datetime.now(dt.timezone.utc)
        return prune(args.archive, args.execute, now)
    if args.command == "recover":
        return recover(args.run_dir, args.execute)
    return compare(args.base, args.candidate)


if __name__ == "__main__":
    raise SystemExit(main())
