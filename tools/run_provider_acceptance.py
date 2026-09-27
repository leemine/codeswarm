#!/usr/bin/env python3
"""Run explicitly selected real acceptance gates and publish sanitized evidence.

No credentials are read from user configuration. Remote suites require the
three HEARTBEAT_REMOTE_* environment inputs. The default full-python job still
collects these opt-in tests without making remote model calls.
"""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import html
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import tomllib
import xml.etree.ElementTree as ET

from testctl import parse_pytest_collect

ROOT = Path(__file__).resolve().parents[1]
SELECTIONS = {
    "local-cli": (
        [
            "tests/system_tests/test_external_goal_codex_local.py",
            "tests/system_tests/test_external_codex_heartbeat_preemption_local.py",
        ],
        2,
    ),
    "goal": (
        [
            "tests/system_tests/test_goal_channels_remote.py",
            "tests/system_tests/test_native_goal_acceptance.py",
            "tests/system_tests/test_goal_user_priority_remote.py",
        ],
        12,
    ),
    "heartbeat": (["tests/system_tests/test_heartbeat_channels_remote.py"], 4),
    "ui": (["tests/system_tests/test_goal_browser_ui_remote.py"], 2),
}


def selection(suite):
    if suite == "all":
        names = ("goal", "heartbeat", "ui")
        return (
            [path for name in names for path in SELECTIONS[name][0]],
            sum(SELECTIONS[name][1] for name in names),
        )
    return SELECTIONS[suite]


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def provenance(paths):
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    declared = project["tool"]["uv"]["sources"]["openjiuwen"]
    locked = tomllib.loads((ROOT / "uv.lock").read_text())
    package = next(p for p in locked["package"] if p["name"] == "openjiuwen")
    direct = json.loads(
        importlib.metadata.distribution("openjiuwen").read_text("direct_url.json")
    )
    origin = Path(importlib.util.find_spec("openjiuwen").origin).resolve()
    assert package["source"]["git"].endswith("#" + declared["rev"]), (
        "core lock mismatch"
    )
    assert direct["vcs_info"]["commit_id"] == declared["rev"], (
        "installed core revision mismatch"
    )
    assert direct["url"].removesuffix(".git") == declared["git"].removesuffix(".git")
    assert origin.is_relative_to(Path(sys.prefix).resolve()), "local core override"
    assert not direct.get("dir_info", {}).get("editable"), "editable core override"
    tracked = (
        subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT)
        .decode()
        .split("\0")
    )
    hashed = {p for p in tracked if p and (ROOT / p).is_file()}
    hashed.update(
        p
        for p in subprocess.check_output(
            ["git", "ls-files", "--others", "--exclude-standard", "-z"], cwd=ROOT
        )
        .decode()
        .split("\0")
        if p and (ROOT / p).is_file()
    )
    hashed.update(paths)
    hashed.add("tools/run_provider_acceptance.py")
    return {
        "head": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip(),
        "dirty": subprocess.check_output(
            ["git", "status", "--porcelain"], cwd=ROOT, text=True
        ),
        "core_commit": declared["rev"],
        "core_origin": str(origin),
        "core_direct_url": direct,
        "lock_sha256": digest(ROOT / "uv.lock"),
        "python": sys.executable,
        "codex_version": importlib.metadata.version("openai-codex"),
        "files": {p: digest(ROOT / p) for p in sorted(hashed)},
        "frontend_dist": {
            str(p.relative_to(ROOT)): digest(p)
            for p in sorted(
                (ROOT / "jiuwenswarm/channels/web/frontend/dist").rglob("*")
            )
            if p.is_file()
        },
        "installed_core": {
            str(p.relative_to(origin.parent)): digest(p)
            for p in sorted(origin.parent.rglob("*.py"))
        },
    }


def reconcile(planned, junit, expected):
    cases = ET.parse(junit).findall(".//testcase")
    executed = [
        case.get("classname", "") + "::" + case.get("name", "") for case in cases
    ]
    # Pytest JUnit classnames use dotted file/class paths, collection uses ::.
    canonical = []
    for node in planned:
        file, *parts = node.split("::")
        canonical.append(
            file.removesuffix(".py").replace("/", ".")
            + ("." + ".".join(parts[:-1]) if len(parts) > 1 else "")
            + "::"
            + parts[-1]
        )
    states = Counter(
        next(
            (
                child.tag
                for child in case
                if child.tag in {"failure", "error", "skipped"}
            ),
            "passed",
        )
        for case in cases
    )
    closed = (
        len(planned) == expected
        and len(cases) == expected
        and Counter(executed) == Counter(canonical)
        and states == Counter(passed=expected)
    )
    return {
        "expected": expected,
        "collected": len(planned),
        "executed": len(cases),
        "states": dict(states),
        "closed": closed,
        "missing": list((Counter(canonical) - Counter(executed)).elements()),
        "unexpected": list((Counter(executed) - Counter(canonical)).elements()),
    }


def owned_processes(scope):
    owned = []
    for path in Path("/proc").glob("[0-9]*"):
        if int(path.name) == os.getpid():
            continue
        try:
            stat = (path / "stat").read_text().rsplit(")", 1)[1].split()
            if stat[0] in {"Z", "X"}:
                continue
            marker = b"ACCEPTANCE_RUN_ID=" + str(scope).encode()
            if (path / "cwd").resolve().is_relative_to(scope) or marker in (
                path / "environ"
            ).read_bytes().split(b"\0"):
                owned.append((int(path.name), stat[19]))
        except (OSError, RuntimeError, IndexError):
            continue
    return owned


def cleanup(scope):
    remaining = owned_processes(scope)
    rescued = list(remaining)
    for sig in (signal.SIGTERM, signal.SIGKILL):
        for pid, start in remaining:
            try:
                stat = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
                if stat[19] == start:
                    os.kill(pid, sig)
            except (OSError, IndexError):
                pass
        deadline = time.monotonic() + 5
        while remaining and time.monotonic() < deadline:
            time.sleep(0.1)
            remaining = owned_processes(scope)
        if not remaining:
            break
    return {"rescued": rescued, "remaining": remaining}


def run(command, env, scope, name, timeout):
    log = scope / (name + ".log")
    with log.open("w") as stream:
        proc = subprocess.Popen(
            command,
            cwd=ROOT,
            env=env,
            stdout=stream,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            rc = proc.wait(timeout=timeout)
        finally:
            if proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait(timeout=10)
    return rc, log


def redacted(content, secret):
    if isinstance(content, dict):
        return {redacted(k, secret): redacted(v, secret) for k, v in content.items()}
    if isinstance(content, list):
        return [redacted(v, secret) for v in content]
    if isinstance(content, str) and secret:
        for variant in sorted(
            {
                secret,
                json.dumps(secret)[1:-1],
                repr(secret)[1:-1],
                html.escape(secret),
                html.escape(secret, quote=False),
            },
            key=len,
            reverse=True,
        ):
            content = content.replace(variant, "[REDACTED]")
    return content


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", choices=[*SELECTIONS, "all"], required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--allow-dirty", action="store_true", help="local development only"
    )
    args = parser.parse_args()
    paths, expected = selection(args.suite)
    remote = args.suite != "local-cli"
    if remote:
        missing = [
            key
            for key in (
                "HEARTBEAT_REMOTE_API_BASE",
                "HEARTBEAT_REMOTE_API_KEY",
                "HEARTBEAT_REMOTE_MODEL",
            )
            if not os.environ.get(key, "").strip()
        ]
        if missing:
            parser.error("explicit remote acceptance requires: " + ", ".join(missing))
    before = provenance(paths)
    if before["dirty"] and not args.allow_dirty:
        parser.error(
            "acceptance requires a clean checkout (local development: --allow-dirty)"
        )
    if (
        args.suite in {"ui", "all"}
        and not (ROOT / "jiuwenswarm/channels/web/frontend/dist/index.html").exists()
    ):
        parser.error("build the original Web frontend before UI acceptance")
    output = args.output.resolve()
    if output.is_relative_to(ROOT):
        ignored = (
            subprocess.run(
                [
                    "git",
                    "check-ignore",
                    "--quiet",
                    str(output.relative_to(ROOT) / "summary.json"),
                ],
                cwd=ROOT,
                check=False,
            ).returncode
            == 0
        )
        if not ignored:
            parser.error(
                "checkout output must be gitignored; use artifacts/test-runs/provider-acceptance"
            )
    output.mkdir(parents=True, exist_ok=False)
    secret = os.environ.get("HEARTBEAT_REMOTE_API_KEY", "")

    def publish(path, content):
        content = redacted(content, secret)
        text = content if isinstance(content, str) else json.dumps(content, indent=2)
        (output / path).write_text(text + "\n")

    publish("source-before.json", before)
    result = {"suite": args.suite, "expected": expected, "passed": False}
    try:
        with tempfile.TemporaryDirectory(prefix="provider-acceptance-") as folder:
            scope = Path(folder)
            try:
                env = {
                    k: v
                    for k, v in os.environ.items()
                    if not any(
                        word in k.upper()
                        for word in (
                            "_KEY",
                            "_TOKEN",
                            "_SECRET",
                            "PASSWORD",
                            "CREDENTIAL",
                        )
                    )
                }
                env.update(
                    {
                        "HOME": str(scope / "home"),
                        "CODEX_HOME": str(scope / "codex-home"),
                        "JIUWENSWARM_DATA_DIR": str(scope / "data"),
                        "JIUWENSWARM_CONFIG_DIR": str(scope / "config"),
                        "JIUWENSWARM_TASKS_DIR": str(scope / "tasks"),
                        "ACCEPTANCE_RUN_ID": str(scope),
                        "XDG_CONFIG_HOME": str(scope / "home/.config"),
                        "PYTHONPATH": str(ROOT),
                        "PYTHONDONTWRITEBYTECODE": "1",
                        "RUN_GOAL_REMOTE": "1" if remote else "0",
                        "RUN_HEARTBEAT_REMOTE": "1" if remote else "0",
                        "RUN_GOAL_BROWSER_REMOTE": "1" if remote else "0",
                    }
                )
                for name in ("home", "codex-home", "data", "config", "tasks"):
                    (scope / name).mkdir()
                env.pop("PYTEST_ADDOPTS", None)
                env.pop("GOAL_BROWSER_ARTIFACT_DIR", None)
                base = [
                    sys.executable,
                    "-m",
                    "pytest",
                    *paths,
                    "-q",
                    "-o",
                    "addopts=",
                    "--asyncio-mode=auto",
                    "-p",
                    "no:cacheprovider",
                    "--no-cov",
                ]
                rc, log = run([*base, "--collect-only"], env, scope, "collect", 180)
                planned = parse_pytest_collect(log.read_text())
                publish("collect.log", log.read_text())
                publish("plan.json", {"node_ids": planned, "expected": expected})
                assert rc == 0 and len(planned) == expected, (
                    "acceptance collection/count mismatch"
                )
                if remote:
                    env["HEARTBEAT_REMOTE_API_KEY"] = secret
                junit = scope / "results.xml"
                command = [
                    *base,
                    "--timeout=480",
                    "--basetemp=" + str(scope / "cases"),
                    "--junitxml=" + str(junit),
                ]
                publish(
                    "command.json",
                    {"argv": command, "cwd": str(ROOT), "timeout_seconds": 2400},
                )
                try:
                    rc, log = run(command, env, scope, "run", 2400)
                finally:
                    if (scope / "run.log").exists():
                        publish(
                            "run.log", (scope / "run.log").read_text(errors="replace")
                        )
                    if junit.exists():
                        publish("results.xml", junit.read_text())
                result.update(reconcile(planned, junit, expected), exit_code=rc)
                after = provenance(paths)
                publish("source-after.json", after)
                result["source_unchanged"] = before == after
                result["tests_passed"] = (
                    rc == 0 and result["closed"] and before == after
                )
            finally:
                clean = cleanup(scope)
                publish("runner-cleanup.json", clean)
                # Publish only small sanitized fixture conclusions, never config,
                # provider homes, raw event transcripts or shell snapshots.
                evidence = {}
                for path in (scope / "cases").rglob("*.json"):
                    if path.name in {
                        "result.json",
                        "cleanup.json",
                        "ui-result.json",
                        "browser-result.json",
                        "frontend-cleanup.json",
                        "browser-cleanup.json",
                        "owned-process-cleanup.json",
                        "exit-observations.json",
                        "user-entry-observations.json",
                        "goal-after-interrupt.json",
                        "resume-result.json",
                        "end-observations.json",
                    }:
                        evidence[str(path.relative_to(scope / "cases"))] = json.loads(
                            path.read_text()
                        )
                publish("case-evidence.json", evidence)
                result["cleanup_passed"] = (
                    not clean["rescued"] and not clean["remaining"]
                )
                result["passed"] = (
                    result.get("tests_passed", False) and result["cleanup_passed"]
                )

    finally:
        matches = [
            p.name
            for p in output.iterdir()
            if p.is_file() and redacted(p.read_text(), secret) != p.read_text()
        ]
        for name in matches:
            (output / name).unlink()
        result["credential_matches"] = matches
        result["passed"] = result["passed"] and not matches
        publish("summary.json", result)
        # The workflow uploads only archives with this seal. Failed tests may
        # publish safe diagnostics; a sanitization failure cannot upload raw data.
        if all(
            redacted(p.read_text(), secret) == p.read_text()
            for p in output.iterdir()
            if p.is_file()
        ):
            publish("archive-safe.json", {"credential_matches": 0})
    print(json.dumps({"output": str(output), **result}))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    raise SystemExit(main())
