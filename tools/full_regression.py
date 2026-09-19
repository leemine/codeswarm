#!/usr/bin/env python3
"""Collect, shard, run, reconcile, and archive the full Python test tree."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import tempfile

from shardplan import plan


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PYTEST_ARGS = ["--asyncio-mode=auto"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", type=Path, default=ROOT / ".venv/bin/python")
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--target-cases", type=int, default=250)
    parser.add_argument("--shard-timeout", type=int, default=1800)
    parser.add_argument("--pytest-arg", action="append", default=[])
    parser.add_argument("--test-path", action="append", default=[], help="limit the collect scope (for validation)")
    args = parser.parse_args()
    if args.workers < 1 or args.target_cases < 1 or args.shard_timeout < 1:
        parser.error("workers, target-cases, and shard-timeout must be positive")
    # Preserve the venv symlink entrypoint; resolving it selects the bare base interpreter.
    python = args.python.absolute()
    if not python.exists():
        parser.error(f"Python executable does not exist: {python}")
    archive = (args.archive or ROOT / "artifacts/test-runs" /
               f"full-python-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}").resolve()
    if (archive / "summary.json").exists():
        parser.error(f"archive already has a run: {archive}")
    archive.mkdir(parents=True, exist_ok=True)
    pytest_args = [*DEFAULT_PYTEST_ARGS, *args.pytest_arg]
    lock = ROOT / "uv.lock"
    fingerprint = {
        "git_sha": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "python": str(python),
        "python_version": subprocess.check_output([str(python), "--version"], text=True).strip(),
        "platform": platform.platform(),
        "uv_lock_sha256": hashlib.sha256(lock.read_bytes()).hexdigest() if lock.exists() else None,
        "pytest_args": pytest_args,
    }
    (archive / "environment.json").write_text(json.dumps(fingerprint, indent=2) + "\n", encoding="utf-8")
    with tempfile.TemporaryDirectory(prefix="t-collect-") as home:
        env = os.environ.copy()
        for key in list(env):
            if any(part in key.upper() for part in ("_KEY", "_TOKEN", "_SECRET", "PASSWORD", "CREDENTIAL")):
                env.pop(key)
        env["HOME"] = home
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        command = [str(python), "-m", "pytest", *(args.test_path or ["tests"]), *pytest_args,
                   "--collect-only", "-q", "-o", "addopts=", "-p", "no:cacheprovider"]
        try:
            collected = subprocess.run(command, cwd=ROOT, env=env, text=True,
                                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                       timeout=600, check=False)
        except subprocess.TimeoutExpired as exc:
            (archive / "collect.log").write_text(str(exc) + "\n", encoding="utf-8")
            return 2
    collect_log = archive / "collect.log"
    collect_log.write_text(collected.stdout, encoding="utf-8")
    if collected.returncode != 0:
        print(f"Collection failed; see {collect_log}")
        return 2
    shard_plan = plan(collected.stdout.splitlines(), args.target_cases)
    if not shard_plan["collected_cases"]:
        print(f"No cases collected; see {collect_log}")
        return 2
    plan_path = archive / "shards.json"
    plan_path.write_text(json.dumps(shard_plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    run_command = [str(python), str(ROOT / "tools/run_full_shards.py"),
                   "--repo", str(ROOT), "--plan", str(plan_path),
                   "--collect-log", str(collect_log), "--python", str(python),
                   "--archive", str(archive), "--workers", str(args.workers),
                   "--shard-timeout", str(args.shard_timeout)]
    for option in pytest_args:
        run_command.append(f"--pytest-arg={option}")
    result = subprocess.run(run_command, cwd=ROOT, check=False)
    if (archive / "summary.json").exists():
        analyzed = subprocess.run([str(python), str(ROOT / "tools/analyze_full_shards.py"), str(archive)],
                                  cwd=ROOT, check=False)
    else:
        analyzed = None
    print(f"Full Python archive: {archive}")
    return result.returncode or (analyzed.returncode if analyzed is not None else 2)


if __name__ == "__main__":
    raise SystemExit(main())
