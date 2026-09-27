"""Isolated original Web app for the opt-in real Goal browser qualification.

No user configuration discovery, browser state injection, or product mocks.
The shared fixture owns AgentServer/Gateway; this helper owns only app_web.
"""
from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import os
import signal
import sys
import time
import urllib.error
import urllib.request
from contextlib import asynccontextmanager
from pathlib import Path

def preflight(provider: str) -> Path:
    for name in ("HEARTBEAT_REMOTE_API_BASE", "HEARTBEAT_REMOTE_API_KEY", "HEARTBEAT_REMOTE_MODEL"):
        if not os.environ.get(name, "").strip():
            raise AssertionError(f"Explicit browser opt-in requires {name}")
    if importlib.util.find_spec("playwright") is None:
        raise AssertionError("Explicit browser opt-in requires Playwright and its Chromium installation")
    if provider == "codex" and importlib.util.find_spec("openai_codex") is None:
        raise AssertionError("Explicit Codex browser opt-in requires the locked codex extra")
    import jiuwenswarm

    dist = Path(jiuwenswarm.__file__).parent / "channels/web/frontend/dist"
    if not (dist / "index.html").is_file():
        raise AssertionError("Build the original frontend (npm ci && npm run build) before browser qualification")
    return dist


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def observe_browser_channel(page, evidence: dict) -> None:
    """Passive browser-owned WebSocket diagnostics, without message/tool bodies."""
    observations = evidence.setdefault("browser_channel", [])
    requests = {}

    def record(raw, direction):
        try:
            frame = json.loads(raw)
        except (ValueError, TypeError):
            return
        method, event = frame.get("method", ""), frame.get("event", "")
        interesting = method in {"command.goal", "session.create", "project.list", "project.pinned_sessions"} or event.startswith("goal.")
        if direction == "sent" and interesting:
            requests[frame.get("id")] = method
        if not interesting and not (direction == "received" and frame.get("id") in requests):
            return
        payload = frame.get("params") if direction == "sent" else frame.get("payload")
        payload = payload if isinstance(payload, dict) else {}
        goal = payload.get("goal")
        goal = goal if isinstance(goal, dict) else {}
        observations.append({"direction": direction, "id": frame.get("id"), "method": method or requests.get(frame.get("id"), ""),
                             "event": event, "session_id": payload.get("session_id"),
                             "action": payload.get("action"), "result_type": payload.get("result_type"),
                             "goal_id": goal.get("goal_id"), "goal_status": goal.get("status"),
                             "work_mode": payload.get("work_mode"), "ok": frame.get("ok")})

    def socket_open(socket):
        socket.on("framesent", lambda raw: record(raw, "sent"))
        socket.on("framereceived", lambda raw: record(raw, "received"))

    page.on("websocket", socket_open)


async def wait_code_mode_complete(evidence: dict, start: int) -> None:
    """Observe the original setWorkMode -> loadProjects -> loadPinnedSessions ACKs."""
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        rows = evidence["browser_channel"][start:]
        project_ids = {r["id"] for r in rows if r["direction"] == "sent"
                       and r["method"] == "project.list" and r["work_mode"] == "code"}
        project_responses = [i for i, r in enumerate(rows)
                             if r["direction"] == "received" and r["id"] in project_ids and r["ok"] is True]
        if project_responses:
            after_project = rows[project_responses[0] + 1:]
            pinned_ids = {r["id"] for r in after_project if r["direction"] == "sent"
                          and r["method"] == "project.pinned_sessions"}
            if any(r["direction"] == "received" and r["id"] in pinned_ids and r["ok"] is True
                   for r in after_project):
                return
        await asyncio.sleep(0.05)
    raise AssertionError("Original Code mode switch did not finish project.list -> pinned_sessions responses")


def owned_processes(scope: Path) -> list[tuple[int, str]]:
    """Linux CI: identify owned live children by isolated cwd/config, never names."""
    result = []
    for path in Path("/proc").glob("[0-9]*"):
        if int(path.name) == os.getpid():
            continue
        try:
            stat = (path / "stat").read_text().rsplit(")", 1)[1].split()
            if stat[0] in {"Z", "X"}:
                continue
            cwd = (path / "cwd").resolve()
            env = (path / "environ").read_bytes().split(b"\0")
            data = b"JIUWENSWARM_DATA_DIR=" + str(scope / "data").encode()
            if cwd.is_relative_to(scope) or data in env:
                result.append((int(path.name), stat[19]))
        except (OSError, IndexError):
            continue
    return result


async def cleanup_audit(scope: Path) -> None:
    from .test_heartbeat_channels_remote import remove_secret_artifacts

    # Service normal shutdown should release its children. On failed tests,
    # clean remaining exact owned identities, and retain that rescue as evidence.
    rescued = owned_processes(scope)
    for pid, start in rescued:
        try:
            stat = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
            if stat[19] == start:
                os.kill(pid, signal.SIGKILL)
        except (OSError, IndexError):
            pass
    deadline = time.monotonic() + 5
    while owned_processes(scope) and time.monotonic() < deadline:
        await asyncio.sleep(0.1)
    sanitized = remove_secret_artifacts(scope)
    secret = os.environ["HEARTBEAT_REMOTE_API_KEY"].encode()
    matches = [str(p.relative_to(scope)) for p in scope.rglob("*")
               if p.is_file() and not p.is_symlink() and secret in p.read_bytes()]
    services = json.loads((scope / "cleanup.json").read_text())
    remaining = owned_processes(scope)
    report = {
        "remaining_processes": remaining, "rescued_owned_processes": rescued,
        "remaining_credential_matches": matches, "sanitized_artifacts": sanitized,
        "credential_config_removed": not (scope / "data/config/config.yaml").exists(),
        "services": services,
    }
    (scope / "browser-cleanup.json").write_text(json.dumps(report, indent=2))
    assert not remaining and not matches, "Owned UI resources or credentials remain; see browser-cleanup.json"
    assert report["credential_config_removed"] and services["agent_exited"] and services["gateway_exited"]
    assert not rescued, "Product service shutdown left owned children; rescued and recorded in browser-cleanup.json"


async def wait_http(url: str, process) -> None:
    def ready():
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                return response.status == 200 and b"<title>WorkSwarm</title>" in response.read()
        except (OSError, urllib.error.URLError):
            return False

    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        assert process.poll() is None, "Original app_web exited before readiness"
        if await asyncio.to_thread(ready):
            return
        await asyncio.sleep(0.2)
    raise AssertionError("Original app_web did not serve the built WorkSwarm frontend")


@asynccontextmanager
async def browser_services(scope: Path, provider: str):
    # Import only after the opted-in test has isolated the test process's paths:
    # common.utils initializes its logger on import. Disabled collection must
    # never touch the user's default data/config directory.
    from .test_external_codex_web_channel_local import _pick_free_port, _stop_process
    from .test_heartbeat_channels_remote import _start_service, web_services

    frontend = None
    try:
        async with web_services(scope, provider) as (gateway_url, data, _profile):
            port = _pick_free_port()
            env = os.environ.copy()
            env.update(JIUWENSWARM_DATA_DIR=str(data), JIUWENSWARM_CONFIG_DIR=str(data / "config"))
            env.pop("HEARTBEAT_REMOTE_API_KEY", None)
            env.pop("HEARTBEAT_REMOTE_API_BASE", None)
            frontend = _start_service(
                [sys.executable, "-m", "jiuwenswarm.channels.web.app_web", "--host", "127.0.0.1",
                 "--port", str(port), "--proxy-target", gateway_url.replace("ws://", "http://").removesuffix("/ws")],
                env=env, log_path=scope / "frontend.log",
            )
            try:
                url = f"http://127.0.0.1:{port}"
                await wait_http(url, frontend)
                yield url, data
            finally:
                _stop_process(frontend)
    finally:
        (scope / "frontend-cleanup.json").write_text(json.dumps({
            "frontend_exited": frontend is None or frontend.poll() is not None,
        }))
        if (scope / "cleanup.json").exists():
            await cleanup_audit(scope)


def history_evidence(data: Path, session_id: str, marker: str, artifact: Path, provider: str) -> dict:
    session_dir = data / "agent/sessions" / session_id
    metadata = json.loads((session_dir / "metadata.json").read_text())
    assert metadata["session_id"] == session_id
    if provider == "codex":
        recovery = json.loads((session_dir / "execution-recovery.json").read_text())
        assert metadata["execution_profile_id"] == recovery["execution_profile_id"] == "r1-08-codex-remote"
        assert recovery["binding"]["provider_id"] == provider
        assert recovery["binding"]["host_session_id"] == session_id
    else:
        assert not metadata.get("execution_profile_id"), "Native case unexpectedly used an External profile"
    path = session_dir / "history.jsonl"
    records = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    objectives = [row for row in records if row.get("is_goal_objective_message")]
    assert len(objectives) == 1, "Expected exactly one durable Goal objective"
    goal_id = objectives[0]["goal_id"]
    assert marker in str(objectives[0].get("content", ""))
    cards = [row for row in records if row.get("is_goal_completed_message")]
    assert len(cards) == 1 and cards[0]["goal_id"] == goal_id
    assert cards[0]["id"] == f"goal-completed-{goal_id}"
    deliveries = [row["delivery_id"] for row in records if row.get("delivery_id")]
    assert len(deliveries) == len(set(deliveries)), "Duplicate durable deliveries"
    assert artifact.read_bytes() == marker.encode(), "Actual tool artifact differs from the synthetic objective"
    return {"session_id": session_id, "goal_id": goal_id, "objective_count": 1, "completion_card_count": 1,
            "history_sha256": digest(path), "artifact_sha256": digest(artifact), "artifact_bytes": artifact.stat().st_size}
