#!/usr/bin/env python3
"""Create deterministic file-level pytest shards from collect-only node IDs."""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path


def plan(lines: list[str], target_cases: int) -> dict:
    if target_cases <= 0:
        raise ValueError("target_cases must be positive")
    files = Counter(
        line.split("::", 1)[0]
        for line in lines
        if line.startswith("tests/") and "::" in line
    )
    ordered = sorted(files.items(), key=lambda item: (-item[1], item[0]))
    shards: list[dict] = []
    for path, count in ordered:
        candidates = [shard for shard in shards if shard["case_count"] + count <= target_cases]
        if candidates:
            shard = min(candidates, key=lambda item: (item["case_count"], item["id"]))
        else:
            shard = {"id": f"shard-{len(shards) + 1:03}", "case_count": 0, "files": []}
            shards.append(shard)
        shard["files"].append(path)
        shard["case_count"] += count
    for shard in shards:
        shard["files"].sort()
    return {
        "schema_version": 1,
        "strategy": "file-level-count-first-fit-v1",
        "target_cases": target_cases,
        "collected_cases": sum(files.values()),
        "file_count": len(files),
        "shard_count": len(shards),
        "oversized_files": [{"file": path, "cases": count} for path, count in ordered if count > target_cases],
        "shards": shards,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("collect_log", type=Path)
    parser.add_argument("--target-cases", type=int, default=250)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    payload = plan(args.collect_log.read_text(encoding="utf-8").splitlines(), args.target_cases)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in payload.items() if key != "shards"}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
