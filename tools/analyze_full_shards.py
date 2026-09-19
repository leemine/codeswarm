#!/usr/bin/env python3
"""Classify archived pytest failures without changing the tests under review."""

from __future__ import annotations

import argparse
from collections import Counter
import csv
import json
from pathlib import Path
import re
import xml.etree.ElementTree as ET


def classify(message: str) -> str:
    lower = message.lower()
    if "timeout (>" in lower and "pytest-timeout" in lower:
        return "single_case_timeout"
    if "async def functions are not natively supported" in lower:
        return "async_test_configuration"
    if "async_generator" in lower:
        return "async_fixture_resolution"
    if "no current event loop" in lower:
        return "event_loop_fixture"
    if "module not found" in lower or "modulenotfounderror" in lower or "import pyarrow" in lower or "usable engine" in lower:
        return "missing_dependency"
    if "aigw binary is missing" in lower or "connection refused" in lower or "cannot connect" in lower:
        return "missing_service_or_binary"
    if "sandbox gateway" in lower or "af_unix path too long" in lower:
        return "sandbox_environment"
    if "keyerror: 'cn'" in lower:
        return "configuration_or_fixture"
    if "assert" in lower or "did not raise" in lower or "regex pattern did not match" in lower:
        return "assertion_needs_investigation"
    return "other_exception"


def owner(repo: str, case_id: str) -> str:
    parts = case_id.split("::", 1)[0].split(".")
    for name in ("agentserver", "channel", "server", "runtime", "core", "extensions",
                 "harness", "agent_evolving", "dev_tools", "symphony", "gateway", "rsi"):
        if name in parts:
            return f"{repo}/{name}"
    return f"{repo}/test-infrastructure"


def inspect(archive: Path) -> dict:
    summary = json.loads((archive / "summary.json").read_text(encoding="utf-8"))
    records: list[dict[str, str]] = []
    for shard_id, result in sorted(summary["results"].items()):
        junit = result.get("junit")
        if junit:
            for case in ET.parse(archive / junit).getroot().iter("testcase"):
                issue = case.find("failure")
                state = "failed"
                if issue is None:
                    issue = case.find("error")
                    state = "error"
                if issue is None:
                    continue
                message = (issue.attrib.get("message") or issue.text or "").strip()
                if not message and issue.text:
                    message = issue.text.strip()
                if message.startswith("Failed: Timeout ("):
                    state = "timeout"
                case_id = f"{case.attrib.get('classname', '')}::{case.attrib.get('name', '')}"
                records.append({
                    "repo": summary["repo"], "shard": shard_id, "case_id": case_id,
                    "state": state, "category": classify(message),
                    "evidence": re.sub(r"\s+", " ", message)[:400],
                    "junit": junit, "owner_suggestion": owner(summary["repo"], case_id),
                })
        skips = result.get("collection_skips", [])
        skip_reason = "; ".join(item.get("reason", "") for item in skips)
        for nodeid in result.get("not_run_cases", []):
            records.append({
                "repo": summary["repo"], "shard": shard_id, "case_id": nodeid,
                "state": "not_run", "category": "collection_dependency",
                "evidence": re.sub(r"\s+", " ", skip_reason)[:400],
                "junit": junit or "", "owner_suggestion": owner(summary["repo"], nodeid.replace("/", ".")),
            })
        for drift in result.get("id_drift_cases", []):
            records.append({
                "repo": summary["repo"], "shard": shard_id, "case_id": drift["canonical"],
                "state": "id_drift", "category": "dynamic_parameter_id",
                "evidence": f"isolated ID: {drift['isolated']}",
                "junit": junit or "", "owner_suggestion": f"{summary['repo']}/test-infrastructure",
            })
        if result["returncode"] != 0 and not junit:
            records.append({
                "repo": summary["repo"], "shard": shard_id, "case_id": f"{shard_id}::collection",
                "state": "collection_error", "category": "collection_or_runner_failure",
                "evidence": f"pytest returncode {result['returncode']}; see {result['log']}",
                "junit": "", "owner_suggestion": f"{summary['repo']}/test-infrastructure",
            })
    by_state = Counter(record["state"] for record in records)
    by_category = Counter(record["category"] for record in records)
    by_owner = Counter(record["owner_suggestion"] for record in records)
    return {
        "git_sha": summary["git_sha"], "repo": summary["repo"],
        "planned_cases": summary["planned_cases"], "shards": summary["planned_shards"],
        "closed": summary["closed"], "not_run": summary["not_run"],
        "id_drift": summary.get("id_drift", 0),
        "collection_skip_events": summary["collection_skip_events"],
        "by_state": dict(by_state), "by_category": dict(by_category),
        "by_owner_suggestion": dict(by_owner), "records": records,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    args = parser.parse_args()
    payload = inspect(args.archive)
    (args.archive / "failure_inventory.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with (args.archive / "failure_inventory.csv").open("w", encoding="utf-8", newline="") as stream:
        fields = ["repo", "shard", "case_id", "state", "category", "evidence", "junit", "owner_suggestion"]
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(payload["records"])
    lines = [
        f"# {payload['repo']} full-shard triage",
        "",
        f"- Commit: `{payload['git_sha']}`",
        f"- Planned: {payload['planned_cases']} cases in {payload['shards']} shards",
        f"- Accounting closed: {payload['closed']}",
        f"- Not run: {payload['not_run']} cases; collection-skip events: {payload['collection_skip_events']}",
        f"- Dynamically changed parameter IDs: {payload['id_drift']}",
        "",
        "| Category | Cases/events | Example |",
        "|---|---:|---|",
    ]
    for category, count in sorted(payload["by_category"].items(), key=lambda item: (-item[1], item[0])):
        example = next(record for record in payload["records"] if record["category"] == category)
        example_id = example["case_id"].replace("|", "\\|")
        lines.append(f"| `{category}` | {count} | `{example_id}` |")
    lines.extend(["", "Owner suggestions are based on test paths, not assigned people.",
                  "See `failure_inventory.csv` for every affected case and its JUnit evidence.", ""])
    (args.archive / "triage_summary.md").write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps({key: value for key, value in payload.items() if key != "records"},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
