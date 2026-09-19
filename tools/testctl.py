#!/usr/bin/env python3
"""Small, dependency-free test orchestration MVP.

The runner intentionally uses only the Python standard library so that
``doctor`` can explain a missing project environment before test dependencies
are installed.  The manifest and summary protocols are versioned separately
from this implementation.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
import xml.etree.ElementTree as ET


SCHEMA_VERSION = 1
REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = REPO_ROOT / "tools" / "test-manifest.json"
DEFAULT_ARCHIVE = REPO_ROOT / "artifacts" / "test-runs"
FINAL_STATES = ("passed", "failed", "error", "timeout", "skipped", "blocked", "not_run", "flaky_pass")
SECRET_ENV_NAMES = {
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "DEEPSEEK_API_KEY",
    "DASHSCOPE_API_KEY",
    "AZURE_OPENAI_API_KEY",
}


class TestCtlError(RuntimeError):
    pass


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def load_manifest(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise TestCtlError(f"manifest not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise TestCtlError(f"invalid manifest JSON: {exc}") from exc
    if data.get("schema_version") != SCHEMA_VERSION:
        raise TestCtlError(
            f"unsupported schema_version={data.get('schema_version')!r}; expected {SCHEMA_VERSION}"
        )
    suites = data.get("suites")
    profiles = data.get("profiles")
    if not isinstance(suites, list) or not isinstance(profiles, dict):
        raise TestCtlError("manifest requires suites[] and profiles{}")
    ids = [suite.get("id") for suite in suites]
    if any(not value for value in ids) or len(ids) != len(set(ids)):
        raise TestCtlError("suite ids must be present and unique")
    known = set(ids)
    for name, profile in profiles.items():
        unknown = set(profile.get("suites", [])) - known
        if unknown:
            raise TestCtlError(f"profile {name!r} references unknown suites: {sorted(unknown)}")
    return data


def suite_map(manifest: dict) -> dict[str, dict]:
    return {suite["id"]: suite for suite in manifest["suites"]}


def selected_suites(manifest: dict, profile: str | None, suite_ids: list[str]) -> list[dict]:
    by_id = suite_map(manifest)
    if suite_ids:
        unknown = set(suite_ids) - set(by_id)
        if unknown:
            raise TestCtlError(f"unknown suites: {sorted(unknown)}")
        return [by_id[suite_id] for suite_id in suite_ids]
    if not profile:
        raise TestCtlError("provide --profile or at least one --suite")
    if profile not in manifest["profiles"]:
        raise TestCtlError(f"unknown profile: {profile}")
    return [by_id[suite_id] for suite_id in manifest["profiles"][profile]["suites"]]


def choose_python() -> str:
    override = os.environ.get("TESTCTL_PYTHON")
    if override:
        return str(Path(override).expanduser().absolute())
    local = REPO_ROOT / ".venv" / "bin" / "python"
    if local.exists():
        return str(local)
    return shutil.which("python3") or shutil.which("python") or "python3"


def choose_uv() -> str:
    override = os.environ.get("TESTCTL_UV")
    if override:
        return str(Path(override).expanduser().absolute())
    local = REPO_ROOT / ".venv" / "bin" / "uv"
    if local.exists():
        return str(local)
    return shutil.which("uv") or "uv"


def expand_command(values: list[str], *, junit: Path | None = None) -> list[str]:
    replacements = {
        "{python}": choose_python(),
        "{uv}": choose_uv(),
        "{repo}": str(REPO_ROOT),
        "{junit}": str(junit) if junit else "",
    }
    expanded: list[str] = []
    for value in values:
        for token, replacement in replacements.items():
            value = value.replace(token, replacement)
        expanded.append(value)
    return expanded


def executable_available(command: list[str], cwd: Path) -> tuple[bool, str]:
    executable = command[0]
    if os.path.sep in executable:
        candidate = Path(executable)
        if not candidate.is_absolute():
            candidate = cwd / candidate
        return candidate.exists() and os.access(candidate, os.X_OK), str(candidate)
    found = shutil.which(executable)
    return bool(found), found or executable


def git_metadata() -> dict:
    def capture(*args: str) -> str | None:
        result = subprocess.run(
            ["git", "-C", str(REPO_ROOT), *args],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return result.stdout.strip() if result.returncode == 0 else None

    return {
        "commit": capture("rev-parse", "HEAD"),
        "branch": capture("rev-parse", "--abbrev-ref", "HEAD"),
        "dirty": bool(capture("status", "--porcelain")),
    }


def environment_fingerprint() -> dict:
    python = choose_python()
    result = subprocess.run(
        [python, "-c", "import platform,sys; print(platform.python_version()); print(sys.executable)"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    lines = result.stdout.splitlines()
    lock_hashes = {}
    for relative in ("uv.lock", "jiuwenswarm/channels/browser/frontend/package-lock.json", "jiuwenswarm/channels/tui/frontend/package-lock.json"):
        path = REPO_ROOT / relative
        if path.exists():
            lock_hashes[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return {
        "python_version": lines[0] if lines else None,
        "python_executable": lines[1] if len(lines) > 1 else python,
        "node_version": command_version(["node", "--version"]),
        "npm_version": command_version(["npm", "--version"]),
        "platform": sys.platform,
        "lock_sha256": lock_hashes,
    }


def command_version(command: list[str]) -> str | None:
    result = subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False)
    return result.stdout.strip() if result.returncode == 0 else None


def python_compatibility(requirement: str | None) -> tuple[bool, str | None]:
    if not requirement:
        return True, None
    result = subprocess.run(
        [choose_python(), "-c", "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        return False, None
    actual = tuple(int(part) for part in result.stdout.strip().split("."))
    lower = re.search(r">=(\d+)\.(\d+)", requirement)
    upper = re.search(r"<(\d+)\.(\d+)", requirement)
    compatible = (not lower or actual >= tuple(map(int, lower.groups()))) and (
        not upper or actual < tuple(map(int, upper.groups()))
    )
    return compatible, result.stdout.strip()


def make_sandbox(run_id: str, suite_id: str) -> tuple[Path, dict[str, str]]:
    safe_id = re.sub(r"[^A-Za-z0-9_.-]", "_", suite_id)
    root = Path(tempfile.mkdtemp(prefix=f"testctl-{run_id}-{safe_id}-"))
    paths = {
        "HOME": root / "home",
        "XDG_CONFIG_HOME": root / "xdg" / "config",
        "XDG_CACHE_HOME": root / "xdg" / "cache",
        "XDG_DATA_HOME": root / "xdg" / "data",
        "TMPDIR": root / "tmp",
        "TESTCTL_STATE_DIR": root / "state",
    }
    for path in paths.values():
        path.mkdir(parents=True, exist_ok=True)
    return root, {name: str(path) for name, path in paths.items()}


def hermetic_env(sandbox_env: dict[str, str]) -> dict[str, str]:
    env = os.environ.copy()
    for name in SECRET_ENV_NAMES:
        env.pop(name, None)
    env.update(sandbox_env)
    env.update(
        {
            "CI": "1",
            "NO_COLOR": "1",
            "TESTCTL_NETWORK_POLICY": "hermetic-audit",
            "PYTHONUNBUFFERED": "1",
        }
    )
    return env


def network_mode() -> str:
    mode = os.environ.get("TESTCTL_NETWORK_MODE", "audit")
    if mode not in {"audit", "strict"}:
        raise TestCtlError(f"unknown TESTCTL_NETWORK_MODE={mode!r}; expected audit or strict")
    return mode


def strict_network_available() -> tuple[bool, str | None]:
    try:
        prefix = bwrap_prefix()
    except TestCtlError as exc:
        return False, str(exc)
    probe = subprocess.run(
        prefix + ["--unshare-net", "--ro-bind", "/", "/", "--bind", "/tmp", "/tmp", "--proc", "/proc", "--dev", "/dev", "--", "/bin/true"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    return probe.returncode == 0, probe.stderr.strip() if probe.returncode else None


def bwrap_prefix() -> list[str]:
    executable = shutil.which("bwrap")
    if not executable:
        raise TestCtlError("bubblewrap executable not found")
    if os.environ.get("TESTCTL_BWRAP_SUDO") == "1":
        sudo = shutil.which("sudo")
        if not sudo:
            raise TestCtlError("sudo executable not found for privileged bubblewrap")
        return [sudo, "-n", "-E", executable]
    return [executable]


def isolated_command(suite: dict, workdir: Path, command: list[str], junit_dir: Path | None = None) -> list[str]:
    if network_mode() != "strict":
        return command
    if suite.get("services") or "local_service" in suite.get("capabilities", []):
        raise TestCtlError(f"strict network namespace cannot host local service: {suite['id']}")
    wrapper = bwrap_prefix() + ["--unshare-net", "--ro-bind", "/", "/", "--bind", "/tmp", "/tmp"]
    if junit_dir is not None:
        wrapper.extend(["--bind", str(junit_dir), str(junit_dir)])
    if suite.get("runner") in {"vitest", "node-test", "web-scripts"} or suite.get("writable_workdir"):
        wrapper.extend(["--bind", str(workdir), str(workdir)])
    wrapper.extend(["--proc", "/proc", "--dev", "/dev", "--"])
    return wrapper + command


def run_process(command: list[str], cwd: Path, env: dict[str, str], timeout_seconds: int) -> dict:
    started = time.monotonic()
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=os.name == "posix",
    )
    try:
        output, _ = process.communicate(timeout=timeout_seconds)
        return {
            "returncode": process.returncode,
            "output": output,
            "timed_out": False,
            "duration_seconds": round(time.monotonic() - started, 6),
        }
    except subprocess.TimeoutExpired:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
        output, _ = process.communicate()
        return {
            "returncode": process.returncode,
            "output": output,
            "timed_out": True,
            "duration_seconds": round(time.monotonic() - started, 6),
        }


def start_services(suite: dict, cwd: Path, env: dict[str, str], sandbox: Path) -> tuple[list[subprocess.Popen], dict[str, str], str | None]:
    """Start suite-owned HTTP services with server-side port-0 binding.

    A service reports its selected port as ``TESTCTL_PORT=<port>`` on stdout.
    The runner probes its health path before exposing the port to the suite.
    """
    processes: list[subprocess.Popen] = []
    service_env: dict[str, str] = {}
    for service in suite.get("services", []):
        name = service["id"]
        log = sandbox / f"service-{name}.log"
        output = log.open("w", encoding="utf-8")
        command = expand_command(service["command"])
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=env,
            text=True,
            stdout=output,
            stderr=subprocess.STDOUT,
            start_new_session=os.name == "posix",
        )
        output.close()
        processes.append(process)
        deadline = time.monotonic() + float(service.get("ready_timeout_seconds", 15))
        port: int | None = None
        ready = False
        while time.monotonic() < deadline:
            if process.poll() is not None:
                break
            content = log.read_text(encoding="utf-8")
            match = re.search(r"^TESTCTL_PORT=(\d+)$", content, flags=re.MULTILINE)
            if match:
                port = int(match.group(1))
                try:
                    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=0.5)
                    connection.request("GET", service.get("health_path", "/health"))
                    response = connection.getresponse()
                    response.read()
                    connection.close()
                    ready = 200 <= response.status < 300
                except (OSError, http.client.HTTPException, TimeoutError):
                    pass
                if ready:
                    break
            time.sleep(0.05)
        if not ready:
            reason = f"service {name} not ready; exit={process.poll()}, port={port}; log={log}"
            return processes, service_env, reason
        key = service.get("port_env", f"TESTCTL_{name.upper()}_PORT")
        service_env[key] = str(port)
    return processes, service_env, None


def stop_services(processes: list[subprocess.Popen]) -> None:
    for process in reversed(processes):
        if process.poll() is not None:
            continue
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
            process.wait()
def parse_pytest_collect(output: str) -> list[str]:
    return sorted({line.strip() for line in output.splitlines() if "::" in line and not line.startswith("=")})


def parse_vitest_collect(output: str) -> list[str]:
    try:
        payload = json.loads(output)
    except json.JSONDecodeError:
        return []
    discovered: list[str] = []

    def walk(value: object) -> None:
        if isinstance(value, dict):
            name = value.get("name") or value.get("fullName")
            filepath = value.get("file") or value.get("filepath")
            if name and filepath:
                discovered.append(f"{filepath}::{name}")
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(payload)
    return sorted(set(discovered))


def parse_script_collect(output: str) -> list[str]:
    try:
        values = json.loads(output)
    except json.JSONDecodeError:
        return []
    return sorted(values) if isinstance(values, list) and all(isinstance(value, str) for value in values) else []


def discover_suite(suite: dict, run_id: str) -> dict:
    workdir = REPO_ROOT / suite.get("workdir", ".")
    root, sandbox_env = make_sandbox(run_id, f"discover-{suite['id']}")
    discover = suite.get("discover")
    if not discover:
        items = suite.get("inventory", [])
        return {
            "suite_id": suite["id"],
            "status": "passed",
            "granularity": suite.get("inventory_granularity", "declared"),
            "items": items,
            "count": len(items),
            "sandbox": str(root),
        }
    command = expand_command(discover["command"])
    available, resolved = executable_available(command, workdir)
    if not available:
        return {
            "suite_id": suite["id"],
            "status": "blocked",
            "reason": f"executable not found: {resolved}",
            "items": [],
            "count": 0,
            "sandbox": str(root),
        }
    result = run_process(isolated_command(suite, workdir, command), workdir, hermetic_env(sandbox_env), int(discover.get("timeout_seconds", 120)))
    parser = discover.get("parser")
    if parser == "pytest":
        items = parse_pytest_collect(result["output"])
    elif parser == "web-scripts-json":
        items = parse_script_collect(result["output"])
    else:
        items = parse_vitest_collect(result["output"])
    status = "timeout" if result["timed_out"] else ("passed" if result["returncode"] == 0 else "error")
    return {
        "suite_id": suite["id"],
        "status": status,
        "returncode": result["returncode"],
        "duration_seconds": result["duration_seconds"],
        "granularity": "test",
        "items": items,
        "count": len(items),
        "output_tail": result["output"][-4000:],
        "sandbox": str(root),
    }


def parse_junit(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as exc:
        raise TestCtlError(f"invalid JUnit XML {path}: {exc}") from exc
    cases: list[dict] = []
    for case in root.iter("testcase"):
        state = "passed"
        detail = None
        state_map = {"failure": "failed", "error": "error", "skipped": "skipped"}
        for candidate in ("failure", "error", "skipped"):
            child = case.find(candidate)
            if child is not None:
                state = state_map[candidate]
                detail = child.get("message") or (child.text or "").strip() or None
                if candidate == "failure" and "Failed: Timeout (" in ((child.text or "") + (detail or "")):
                    state = "timeout"
                break
        cases.append(
            {
                "id": "::".join(part for part in (case.get("classname"), case.get("name")) if part),
                "state": state,
                "duration_seconds": float(case.get("time", "0") or 0),
                "detail": detail,
            }
        )
    return cases


def empty_counts() -> dict[str, int]:
    return {state: 0 for state in FINAL_STATES}


def execute_suite(suite: dict, run_id: str, run_dir: Path) -> dict:
    suite_id = suite["id"]
    safe_id = re.sub(r"[^A-Za-z0-9_.-]", "_", suite_id)
    junit = run_dir / "junit" / f"{safe_id}.xml"
    log = run_dir / "logs" / f"{safe_id}.log"
    junit.parent.mkdir(parents=True, exist_ok=True)
    log.parent.mkdir(parents=True, exist_ok=True)
    sandbox_root, sandbox_env = make_sandbox(run_id, suite_id)
    workdir = REPO_ROOT / suite.get("workdir", ".")
    command = expand_command(suite["command"], junit=junit)
    available, resolved = executable_available(command, workdir)
    if not available:
        counts = empty_counts()
        counts["blocked"] = 1
        return {
            "suite_id": suite_id,
            "owner": suite.get("owner"),
            "tier": suite.get("tier"),
            "required": suite.get("required", True),
            "status": "blocked",
            "reason": f"executable not found: {resolved}",
            "counts": counts,
            "sandbox": str(sandbox_root),
        }
    suite_env = hermetic_env(sandbox_env)
    processes: list[subprocess.Popen] = []
    try:
        processes, service_env, service_error = start_services(suite, workdir, suite_env, sandbox_root)
        if service_error:
            counts = empty_counts()
            counts["error"] = 1
            log.write_text(service_error + "\n", encoding="utf-8")
            return {
                "suite_id": suite_id,
                "owner": suite.get("owner"),
                "tier": suite.get("tier"),
                "required": suite.get("required", True),
                "status": "error",
                "reason": service_error,
                "counts": counts,
                "sandbox": str(sandbox_root),
                "log": str(log.relative_to(run_dir)),
            }
        suite_env.update(service_env)
        result = run_process(isolated_command(suite, workdir, command, junit.parent), workdir, suite_env, int(suite.get("shard_timeout_seconds", 900)))
    finally:
        stop_services(processes)
    log.write_text(result["output"], encoding="utf-8")
    counts = empty_counts()
    cases: list[dict] = []
    parse_error = None
    try:
        cases = parse_junit(junit)
    except TestCtlError as exc:
        parse_error = str(exc)
    for case in cases:
        counts[case["state"]] += 1
    if result["timed_out"]:
        counts["timeout"] += 1
        status = "timeout"
    elif parse_error:
        counts["error"] += 1
        status = "error"
    elif result["returncode"] != 0 and not (counts["failed"] or counts["error"] or counts["timeout"]):
        counts["error"] += 1
        status = "error"
    elif counts["failed"]:
        status = "failed"
    elif counts["error"]:
        status = "error"
    elif counts["timeout"]:
        status = "timeout"
    else:
        status = "passed"
    return {
        "suite_id": suite_id,
        "owner": suite.get("owner"),
        "tier": suite.get("tier"),
        "capabilities": suite.get("capabilities", []),
        "required": suite.get("required", True),
        "status": status,
        "counts": counts,
        "duration_seconds": result["duration_seconds"],
        "returncode": result["returncode"],
        "command": command,
        "junit": str(junit.relative_to(run_dir)) if junit.exists() else None,
        "log": str(log.relative_to(run_dir)),
        "sandbox": str(sandbox_root),
        "junit_parse_error": parse_error,
        "cases": cases,
    }


def aggregate(run_id: str, profile: str | None, results: list[dict], parent_run_id: str | None, retention_days: int) -> dict:
    totals = empty_counts()
    for result in results:
        for state, value in result["counts"].items():
            totals[state] += value
    executed = sum(totals[state] for state in ("passed", "failed", "error", "timeout", "skipped", "flaky_pass"))
    planned = executed + totals["blocked"] + totals["not_run"]
    required_bad = [
        result["suite_id"]
        for result in results
        if result.get("required", True) and result["status"] not in {"passed"}
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "parent_run_id": parent_run_id,
        "profile": profile,
        "created_at": utc_now(),
        "retention_days": retention_days,
        "network_isolation": network_mode(),
        "status": "passed" if not required_bad else "failed",
        "required_suites_not_passed": required_bad,
        "totals": totals,
        "planned": planned,
        "executed": executed,
        "closed": planned == sum(totals.values()),
        "suites": results,
    }


def write_summary_markdown(summary: dict, destination: Path) -> None:
    lines = [
        f"# Test run {summary['run_id']}",
        "",
        f"- Status: **{summary['status']}**",
        f"- Profile: `{summary.get('profile')}`",
        f"- Parent run: `{summary.get('parent_run_id')}`",
        f"- Retention: {summary['retention_days']} days",
        f"- Network isolation: `{summary['network_isolation']}`",
        "",
        "| Suite | Tier | Status | Passed | Failed | Error | Timeout | Skipped | Blocked | Not run |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for suite in summary["suites"]:
        counts = suite["counts"]
        lines.append(
            f"| {suite['suite_id']} | {suite.get('tier')} | {suite['status']} | "
            f"{counts['passed']} | {counts['failed']} | {counts['error']} | "
            f"{counts['timeout']} | {counts['skipped']} | {counts['blocked']} | {counts['not_run']} |"
        )
    lines.extend(["", f"Statistics closed: `{summary['closed']}`", ""])
    destination.write_text("\n".join(lines), encoding="utf-8")


def build_plan(manifest: dict, suites: list[dict], profile: str | None) -> dict:
    planned = []
    python_ok, python_actual = python_compatibility(manifest.get("python_requires"))
    strict_ok, strict_reason = strict_network_available() if network_mode() == "strict" else (True, None)
    for suite in suites:
        workdir = REPO_ROOT / suite.get("workdir", ".")
        command = expand_command(suite["command"], junit=Path("<junit>"))
        available, resolved = executable_available(command, workdir)
        if suite.get("runner") == "pytest" and not python_ok:
            available = False
            resolved = f"Python {python_actual} does not satisfy {manifest.get('python_requires')}"
        if network_mode() == "strict" and not strict_ok:
            available = False
            resolved = f"strict network unavailable: {strict_reason}"
        if network_mode() == "strict" and (suite.get("services") or "local_service" in suite.get("capabilities", [])):
            available = False
            resolved = "strict network profile does not permit local services"
        planned.append(
            {
                "suite_id": suite["id"],
                "tier": suite.get("tier"),
                "capabilities": suite.get("capabilities", []),
                "required": suite.get("required", True),
                "decision": "run" if available else "blocked",
                "reason": None if available else f"executable not found: {resolved}",
                "shard_timeout_seconds": suite.get("shard_timeout_seconds", 900),
            }
        )
    return {"schema_version": SCHEMA_VERSION, "profile": profile, "created_at": utc_now(), "suites": planned}


def command_doctor(manifest: dict) -> int:
    checks = []
    python_ok, python_actual = python_compatibility(manifest.get("python_requires"))
    strict_ok, strict_reason = strict_network_available() if network_mode() == "strict" else (True, None)
    for suite in manifest["suites"]:
        cwd = REPO_ROOT / suite.get("workdir", ".")
        command = expand_command(suite["command"], junit=Path("doctor.xml"))
        available, resolved = executable_available(command, cwd)
        checks.append({"suite_id": suite["id"], "available": available, "executable": resolved})
    payload = {
        "schema_version": SCHEMA_VERSION,
        "repo": manifest.get("repo"),
        "git": git_metadata(),
        "environment": environment_fingerprint(),
        "python_requirement": {
            "required": manifest.get("python_requires"),
            "actual": python_actual,
            "compatible": python_ok,
        },
        "network_requirement": {"mode": network_mode(), "available": strict_ok, "reason": strict_reason},
        "checks": checks,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if python_ok and strict_ok and all(check["available"] for check in checks) else 2


def command_bootstrap(manifest: dict, execute: bool) -> int:
    actions = []
    for entry in manifest.get("bootstrap", []):
        cwd = REPO_ROOT / entry.get("workdir", ".")
        command = expand_command(entry["command"])
        available, resolved = executable_available(command, cwd)
        action = {
            "id": entry["id"],
            "workdir": str(cwd),
            "command": command,
            "available": available,
            "executable": resolved,
            "status": "planned" if available else "blocked",
        }
        if execute and available:
            result = run_process(command, cwd, os.environ.copy(), int(entry.get("timeout_seconds", 1200)))
            action.update(
                {
                    "status": "passed" if result["returncode"] == 0 else ("timeout" if result["timed_out"] else "error"),
                    "returncode": result["returncode"],
                    "duration_seconds": result["duration_seconds"],
                    "output_tail": result["output"][-4000:],
                }
            )
        actions.append(action)
    payload = {"schema_version": SCHEMA_VERSION, "execute": execute, "actions": actions}
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    good_states = {"planned", "passed"}
    return 0 if actions and all(action["status"] in good_states for action in actions) else 2


def discover_selected(manifest: dict, suites: list[dict], run_id: str) -> list[dict]:
    decisions = {entry["suite_id"]: entry for entry in build_plan(manifest, suites, None)["suites"]}
    results = []
    for suite in suites:
        decision = decisions[suite["id"]]
        if decision["decision"] == "blocked":
            results.append(
                {
                    "suite_id": suite["id"],
                    "status": "blocked",
                    "reason": decision["reason"],
                    "items": [],
                    "count": 0,
                }
            )
        else:
            results.append(discover_suite(suite, run_id))
    return results


def command_discover(manifest: dict, suites: list[dict], output: Path | None) -> int:
    run_id = f"discover-{dt.datetime.now().strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}"
    results = discover_selected(manifest, suites, run_id)
    payload = {"schema_version": SCHEMA_VERSION, "run_id": run_id, "created_at": utc_now(), "suites": results}
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0 if all(result["status"] == "passed" for result in results) else 1


def command_run(manifest: dict, suites: list[dict], profile: str | None, archive: Path, parent_run_id: str | None) -> int:
    run_id = f"{dt.datetime.now().strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}"
    run_dir = archive / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    profile_data = manifest["profiles"].get(profile, {}) if profile else {}
    retention_days = int(os.environ.get("TESTCTL_RETENTION_DAYS", profile_data.get("retention_days", 30)))
    snapshot = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "parent_run_id": parent_run_id,
        "created_at": utc_now(),
        "source_manifest": manifest,
        "git": git_metadata(),
        "environment": environment_fingerprint(),
    }
    (run_dir / "manifest.json").write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    plan = build_plan(manifest, suites, profile)
    (run_dir / "plan.json").write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    inventory_results = discover_selected(manifest, suites, run_id)
    inventory = {"schema_version": SCHEMA_VERSION, "run_id": run_id, "created_at": utc_now(), "suites": inventory_results}
    (run_dir / "inventory.json").write_text(json.dumps(inventory, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    decisions = {entry["suite_id"]: entry for entry in plan["suites"]}
    inventory_by_id = {entry["suite_id"]: entry for entry in inventory_results}
    results = []
    for suite in suites:
        decision = decisions[suite["id"]]
        discovery = inventory_by_id[suite["id"]]
        nonrun_state = "blocked" if decision["decision"] == "blocked" else discovery["status"]
        if nonrun_state != "passed":
            counts = empty_counts()
            state = nonrun_state if nonrun_state in {"blocked", "error", "timeout"} else "error"
            counts[state] = 1
            results.append(
                {
                    "suite_id": suite["id"],
                    "owner": suite.get("owner"),
                    "tier": suite.get("tier"),
                    "required": suite.get("required", True),
                    "status": state,
                    "reason": decision["reason"] or discovery.get("reason") or discovery.get("output_tail"),
                    "counts": counts,
                }
            )
        else:
            results.append(execute_suite(suite, run_id, run_dir))
    summary = aggregate(run_id, profile, results, parent_run_id, retention_days)
    (run_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write_summary_markdown(summary, run_dir / "summary.md")
    print(json.dumps({"run_id": run_id, "run_dir": str(run_dir), "status": summary["status"], "totals": summary["totals"]}, ensure_ascii=False, indent=2))
    return 0 if summary["status"] == "passed" else 1


def command_summarize(run_dir: Path) -> int:
    summary_path = run_dir / "summary.json"
    if not summary_path.exists():
        raise TestCtlError(f"summary not found: {summary_path}")
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    write_summary_markdown(summary, run_dir / "summary.md")
    print((run_dir / "summary.md").read_text(encoding="utf-8"))
    return 0


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    subparsers = parser.add_subparsers(dest="action", required=True)
    subparsers.add_parser("doctor")
    bootstrap = subparsers.add_parser("bootstrap")
    bootstrap.add_argument("--execute", action="store_true")
    for name in ("discover", "plan", "run"):
        child = subparsers.add_parser(name)
        child.add_argument("--profile")
        child.add_argument("--suite", action="append", default=[])
        if name == "discover":
            child.add_argument("--output", type=Path)
        if name == "run":
            child.add_argument("--archive", type=Path, default=DEFAULT_ARCHIVE)
            child.add_argument("--parent-run-id")
    summarize = subparsers.add_parser("summarize")
    summarize.add_argument("run_dir", type=Path)
    return parser


def main() -> int:
    args = make_parser().parse_args()
    try:
        manifest = load_manifest(args.manifest)
        if args.action == "bootstrap":
            return command_bootstrap(manifest, args.execute)
        if args.action == "doctor":
            return command_doctor(manifest)
        if args.action == "summarize":
            return command_summarize(args.run_dir)
        suites = selected_suites(manifest, args.profile, args.suite)
        if args.action == "discover":
            return command_discover(manifest, suites, args.output)
        if args.action == "plan":
            print(json.dumps(build_plan(manifest, suites, args.profile), ensure_ascii=False, indent=2))
            return 0
        return command_run(manifest, suites, args.profile, args.archive, args.parent_run_id)
    except TestCtlError as exc:
        print(f"testctl: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
