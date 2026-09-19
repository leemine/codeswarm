#!/usr/bin/env python3
"""Wrap a legacy aggregate test command in a coarse JUnit testcase."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import signal
import subprocess
import time
import xml.etree.ElementTree as ET


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--junit", type=Path, required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("a command is required after --")
    start = time.monotonic()
    process = subprocess.Popen(
        command,
        env=os.environ.copy(),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=os.name == "posix",
    )
    try:
        output, _ = process.communicate(timeout=args.timeout)
        state = "passed" if process.returncode == 0 else "failed"
    except subprocess.TimeoutExpired:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
        output, _ = process.communicate()
        state = "timeout"
    args.junit.parent.mkdir(parents=True, exist_ok=True)
    (args.junit.parent / f"{args.name}.log").write_text(output, encoding="utf-8")
    suite = ET.Element("testsuite", name=args.name, tests="1", failures="0" if state == "passed" else "1")
    case = ET.SubElement(suite, "testcase", classname="legacy.aggregate", name=args.name, time=f"{time.monotonic()-start:.6f}")
    if state != "passed":
        ET.SubElement(case, "failure", message="Failed: Timeout (" if state == "timeout" else "Aggregate command failed").text = output[-4000:]
    ET.ElementTree(suite).write(args.junit, encoding="utf-8", xml_declaration=True)
    print(f"{args.name}: {state}")
    return 0 if state == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
