#!/usr/bin/env python3
"""Run Web test:* scripts independently and emit one JUnit case per script."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import time
import xml.etree.ElementTree as ET


def scripts(package: Path) -> list[str]:
    payload = json.loads(package.read_text(encoding="utf-8"))
    return sorted(name for name in payload.get("scripts", {}) if name.startswith("test:"))


def run_one(name: str, cwd: Path, timeout: int) -> dict:
    start = time.monotonic()
    process = subprocess.Popen(
        ["npm", "run", name],
        cwd=cwd,
        env=os.environ.copy(),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=os.name == "posix",
    )
    try:
        output, _ = process.communicate(timeout=timeout)
        state = "passed" if process.returncode == 0 else "failed"
    except subprocess.TimeoutExpired:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
        output, _ = process.communicate()
        state = "timeout"
    return {"name": name, "state": state, "output": output, "time": time.monotonic() - start}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--junit", type=Path)
    parser.add_argument("--only", action="append", default=[])
    parser.add_argument("--script-timeout", type=int, default=120)
    args = parser.parse_args()
    package = args.package.resolve()
    selected = scripts(package)
    if args.only:
        unknown = set(args.only) - set(selected)
        if unknown:
            parser.error(f"unknown test scripts: {sorted(unknown)}")
        selected = [name for name in selected if name in args.only]
    if args.list:
        print(json.dumps(selected, ensure_ascii=False))
        return 0
    if not args.junit:
        parser.error("--junit is required unless --list is used")
    args.junit.parent.mkdir(parents=True, exist_ok=True)
    logs = args.junit.parent / "web-script-logs"
    logs.mkdir(parents=True, exist_ok=True)
    suite = ET.Element("testsuite", name="codeswarm.web.scripts")
    failed = 0
    for name in selected:
        result = run_one(name, package.parent, args.script_timeout)
        log_name = re.sub(r"[^a-zA-Z0-9_.-]", "_", name) + ".log"
        (logs / log_name).write_text(result["output"], encoding="utf-8")
        case = ET.SubElement(suite, "testcase", classname="web.scripts", name=name, time=f"{result['time']:.6f}")
        if result["state"] != "passed":
            failed += 1
            ET.SubElement(
                case,
                "failure",
                message=f"{'Failed: Timeout (' if result['state'] == 'timeout' else 'Script failed: '}{name}",
            ).text = result["output"][-4000:]
        print(f"{name}: {result['state']}", flush=True)
    suite.set("tests", str(len(selected)))
    suite.set("failures", str(failed))
    ET.ElementTree(suite).write(args.junit, encoding="utf-8", xml_declaration=True)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
