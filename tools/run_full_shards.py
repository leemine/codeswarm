#!/usr/bin/env python3
"""Run a shardplan JSON with bounded concurrency and resumable JUnit archives."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time
import xml.etree.ElementTree as ET


def save_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def junit_counts(path: Path) -> tuple[dict[str, int], list[dict[str, str]]]:
    counts = {"passed": 0, "failed": 0, "error": 0, "skipped": 0}
    collection_skips: list[dict[str, str]] = []
    if not path.exists():
        return counts, collection_skips
    root = ET.parse(path).getroot()
    for case in root.iter("testcase"):
        if case.find("failure") is not None:
            counts["failed"] += 1
        elif case.find("error") is not None:
            counts["error"] += 1
        elif (skip := case.find("skipped")) is not None:
            if skip.attrib.get("message") == "collection skipped":
                collection_skips.append({"module": case.attrib.get("classname", ""), "reason": skip.text or ""})
            else:
                counts["skipped"] += 1
        else:
            counts["passed"] += 1
    return counts, collection_skips


def parse_collect(output: str) -> set[str]:
    return {line.strip() for line in output.splitlines()
            if line.startswith("tests/") and "::" in line}


def reconcile_ids(missing: list[str], extra: list[str]) -> tuple[list[str], list[str], list[dict[str, str]]]:
    """Never infer parameter equivalence from a shared test-function name."""
    return missing, extra, []


def run_exit_code(summary: dict) -> int:
    return int(not summary["closed"] or summary["not_run"] != 0 or any(
        item["returncode"] != 0 or item["junit_parse_error"]
        for item in summary["results"].values()))


def run_shard(root: Path, python: Path, archive: Path, shard: dict,
              canonical_cases: set[str], timeout: int, pytest_args: list[str]) -> dict:
    shard_id = shard["id"]
    junit = archive / "junit" / f"{shard_id}.xml"
    log = archive / "logs" / f"{shard_id}.log"
    # AF_UNIX sockets in tests have a 108-byte path limit on Linux.
    home_dir = tempfile.TemporaryDirectory(prefix=f"t-{shard_id}-")
    home = Path(home_dir.name)
    env = os.environ.copy()
    for key in list(env):
        if any(part in key.upper() for part in ("_KEY", "_TOKEN", "_SECRET", "PASSWORD", "CREDENTIAL")):
            env.pop(key)
    env.update({
        "HOME": str(home),
        "XDG_CACHE_HOME": str(home / "cache"),
        "XDG_CONFIG_HOME": str(home / "config"),
        "XDG_DATA_HOME": str(home / "data"),
        "TMPDIR": str(home / "tmp"),
        "PYTHONDONTWRITEBYTECODE": "1",
    })
    (home / "tmp").mkdir()
    collect_command = [str(python), "-m", "pytest", *shard["files"], *pytest_args, "--collect-only", "-q",
                       "-o", "addopts=", "-p", "no:cacheprovider"]
    collect_log = archive / "collect" / f"{shard_id}.log"
    try:
        collected = subprocess.run(collect_command, cwd=root, env=env, text=True,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   timeout=min(timeout, 300), check=False)
        collect_output = collected.stdout
        collect_returncode = collected.returncode
    except subprocess.TimeoutExpired as exc:
        collect_output = (exc.stdout or b"").decode(errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        collect_returncode = -1
    collect_log.write_text(collect_output, encoding="utf-8")
    actual_collected = parse_collect(collect_output)
    raw_missing = sorted(canonical_cases - actual_collected)
    raw_extra = sorted(actual_collected - canonical_cases)
    missing_collection, extra_collection, id_drift = reconcile_ids(raw_missing, raw_extra)
    command = [
        str(python), "-m", "pytest", *shard["files"], *pytest_args, "-q",
        "-o", "addopts=", "-p", "no:cacheprovider", "--timeout=30",
        "--timeout-method=signal", "--tb=short", f"--junitxml={junit}",
    ]
    start = time.monotonic()
    with log.open("w", encoding="utf-8") as stream:
        stream.write("Command: " + json.dumps(command, ensure_ascii=False) + "\n")
        stream.flush()
        process = subprocess.Popen(command, cwd=root, env=env, stdout=stream,
                                   stderr=subprocess.STDOUT, start_new_session=True)
        timed_out = False
        try:
            returncode = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            os.killpg(process.pid, signal.SIGKILL)
            returncode = process.wait()
            stream.write(f"\nSHARD TIMEOUT after {timeout} seconds\n")
    try:
        counts, collection_skips = junit_counts(junit)
        parse_error = None
    except ET.ParseError as exc:
        counts = {"passed": 0, "failed": 0, "error": 0, "skipped": 0}
        collection_skips = []
        parse_error = str(exc)
    junit_cases = sum(counts.values())
    closed = (collect_returncode == 0 and parse_error is None and junit.exists()
              and not extra_collection and junit_cases == len(actual_collected)
              and junit_cases + len(missing_collection) == len(canonical_cases))
    result = {
        "id": shard_id, "planned": len(canonical_cases), "files": shard["files"],
        "counts": counts, "returncode": returncode, "timed_out": timed_out,
        "duration_seconds": round(time.monotonic() - start, 3),
        "junit": str(junit.relative_to(archive)) if junit.exists() else None,
        "log": str(log.relative_to(archive)), "junit_parse_error": parse_error,
        "collect_log": str(collect_log.relative_to(archive)),
        "collect_returncode": collect_returncode,
        "collected_cases": len(actual_collected),
        "collection_skips": collection_skips,
        "not_run_cases": missing_collection,
        "extra_collected_cases": extra_collection,
        "id_drift_cases": id_drift,
        "junit_cases": junit_cases,
        "closed": closed,
    }
    home_dir.cleanup()
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--collect-log", type=Path, required=True)
    parser.add_argument("--python", type=Path, required=True)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--shard-timeout", type=int, default=1800)
    parser.add_argument("--pytest-arg", action="append", default=[], help="explicit pytest option retained when addopts is cleared")
    parser.add_argument("--only", action="append", default=[], help="run only this shard ID")
    parser.add_argument("--reconcile-only", action="store_true", help="reconcile archived IDs without rerunning tests")
    args = parser.parse_args()
    if args.workers < 1 or args.shard_timeout < 1:
        parser.error("workers and shard-timeout must be positive")
    root = args.repo.resolve()
    python = args.python.absolute()
    archive = args.archive.resolve()
    plan = json.loads(args.plan.read_text(encoding="utf-8"))
    canonical_nodes = parse_collect(args.collect_log.read_text(encoding="utf-8"))
    if len(canonical_nodes) != plan["collected_cases"]:
        parser.error("canonical collect log and shard plan disagree")
    archive.mkdir(parents=True, exist_ok=True)
    (archive / "junit").mkdir(exist_ok=True)
    (archive / "logs").mkdir(exist_ok=True)
    (archive / "collect").mkdir(exist_ok=True)
    save_json(archive / "plan.json", plan)
    summary_path = archive / "summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8")) if summary_path.exists() else {
        "schema_version": 1, "repo": root.name, "created_at": datetime.now(timezone.utc).isoformat(),
        "git_sha": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip(),
        "python": str(python), "canonical_collect_log": str(args.collect_log.resolve()),
        "planned_cases": plan["collected_cases"], "planned_shards": plan["shard_count"],
        "results": {},
    }
    if args.reconcile_only:
        if len(summary["results"]) != plan["shard_count"]:
            parser.error("cannot reconcile an incomplete archive")
        for item in summary["results"].values():
            missing, extra, drift = reconcile_ids(item["not_run_cases"], item["extra_collected_cases"])
            item["not_run_cases"] = missing
            item["extra_collected_cases"] = extra
            item["id_drift_cases"] = item.get("id_drift_cases", []) + drift
            item["closed"] = (item["collect_returncode"] == 0 and not item["junit_parse_error"]
                              and item["junit"] is not None and not extra
                              and item["junit_cases"] == item["collected_cases"]
                              and item["junit_cases"] + len(missing) == item["planned"])
        summary["closed"] = all(item["closed"] for item in summary["results"].values())
        summary["not_run"] = sum(len(item["not_run_cases"]) for item in summary["results"].values())
        summary["id_drift"] = sum(len(item["id_drift_cases"]) for item in summary["results"].values())
        summary["collection_skip_events"] = sum(len(item["collection_skips"]) for item in summary["results"].values())
        save_json(summary_path, summary)
        print(f"{root.name}: closed={summary['closed']} not_run={summary['not_run']} id_drift={summary['id_drift']}")
        return run_exit_code(summary)
    selected = set(args.only)
    if selected and selected - {shard["id"] for shard in plan["shards"]}:
        parser.error(f"unknown shard IDs: {sorted(selected - {shard['id'] for shard in plan['shards']})}")
    pending = [shard for shard in plan["shards"]
               if (not selected or shard["id"] in selected) and shard["id"] not in summary["results"]]
    print(f"{root.name}: {len(pending)} pending shards, {args.workers} workers", flush=True)
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(run_shard, root, python, archive, shard,
                               {node for node in canonical_nodes if node.split("::", 1)[0] in set(shard["files"])},
                               args.shard_timeout, args.pytest_arg): shard
                   for shard in pending}
        for future in as_completed(futures):
            result = future.result()
            summary["results"][result["id"]] = result
            summary["closed"] = (len(summary["results"]) == plan["shard_count"]
                                 and all(item["closed"] for item in summary["results"].values()))
            summary["not_run"] = sum(len(item["not_run_cases"]) for item in summary["results"].values())
            summary["id_drift"] = sum(len(item.get("id_drift_cases", [])) for item in summary["results"].values())
            summary["collection_skip_events"] = sum(len(item["collection_skips"]) for item in summary["results"].values())
            save_json(summary_path, summary)
            counts = result["counts"]
            print(f"{root.name} {result['id']}: {counts} rc={result['returncode']} "
                  f"timeout={result['timed_out']} ({len(summary['results'])}/{plan['shard_count']})",
                  flush=True)
    summary["completed_at"] = datetime.now(timezone.utc).isoformat()
    save_json(summary_path, summary)
    return run_exit_code(summary)


if __name__ == "__main__":
    raise SystemExit(main())
