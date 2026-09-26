"""Opt-in R1-08 qualification against a real remote model and product channels.

Set RUN_HEARTBEAT_REMOTE=1 and HEARTBEAT_REMOTE_API_BASE/API_KEY/MODEL.
These tests start isolated AgentServer/Gateway processes and exercise WebSocket
RPC. They do not claim browser DOM/UI coverage. Credentials are removed from
the private generated configuration when each fixture exits.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
import websockets
import yaml

from jiuwenswarm.common.utils import prepare_workspace
from jiuwenswarm.channels.cli.gateway_client import GatewayClient

from .test_external_codex_web_channel_local import (
    _event, _pick_free_port, _receive_until, _send_request,
    _stop_process, _wait_for_log, _wait_for_websocket,
)

pytestmark = [pytest.mark.integration, pytest.mark.system, pytest.mark.skipif(
    os.environ.get("RUN_HEARTBEAT_REMOTE") != "1", reason="remote model qualification is opt-in",
)]


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    assert value, f"{name} is required for real remote qualification"
    return value


def configure_workspace(data_dir: Path, provider: str) -> tuple[Path, str | None]:
    prepare_workspace(overwrite=False, preferred_language="zh", workspace_dir=data_dir)
    root = (data_dir / "agent" / "workspace").resolve()
    config_path = data_dir / "config" / "config.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    base = _required("HEARTBEAT_REMOTE_API_BASE")
    key = _required("HEARTBEAT_REMOTE_API_KEY")
    model = os.environ.get("HEARTBEAT_REMOTE_MODEL", "glm-5.2")
    config["permissions"]["enabled"] = False
    config["models"]["defaults"] = [{
        "model_client_config": {
            "api_base": base, "api_key": key, "model_name": model,
            "client_provider": "OpenAI", "endpoint_profile": "openai_compatible",
            "timeout": 120, "stream_first_chunk_timeout": 60,
            "stream_idle_timeout": 60, "verify_ssl": True, "custom_headers": {},
        }, "model_config_obj": {"context_window": 131072}, "is_default": True,
    }]
    profile = None
    if provider == "codex":
        profile = "r1-08-codex-remote"
        provider_home = data_dir / "provider-home"
        codex_home = data_dir / "codex-home"
        (codex_home / "skills").mkdir(parents=True)
        provider_home.mkdir()
        config["execution"] = {"default_profile_id": profile, "profiles": {profile: {
            "provider_id": "codex", "config_revision": "r1-08-remote-v1",
            "authorization": {"full_access": True},
            "provider_config": {
                "inherit_process_env": False,
                "env": {"HOME": str(provider_home), "CODEX_HOME": str(codex_home),
                        "PATH": os.environ.get("PATH", "/usr/bin:/bin")},
                "mcp_required": True, "mcp_default_tools_approval_mode": "auto",
                "turn_idle_timeout_s": 120, "turn_idle_retries": 0,
                "model": {"model": model, "provider": "r1_08_remote",
                          "api_base": base, "api_key": key},
            },
        }}}
    config_path.write_text(yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding="utf-8")
    config_path.chmod(0o600)
    return root, profile


def service_environment(data_dir: Path, root: Path) -> tuple[dict[str, str], int]:
    env = os.environ.copy()
    web_port = _pick_free_port()
    env.update({
        "JIUWENSWARM_DATA_DIR": str(data_dir), "JIUWENSWARM_CONFIG_DIR": str(data_dir / "config"),
        "JIUWENSWARM_TASKS_DIR": str(root / "projectless-tasks"),
        "JIUWENSWARM_RUNTIME_WORKSPACE_READY": "1",
        "AGENT_SERVER_HOST": "127.0.0.1", "AGENT_SERVER_PORT": str(_pick_free_port()),
        "WEB_HOST": "127.0.0.1", "WEB_PORT": str(web_port),
        "GATEWAY_HOST": "127.0.0.1", "GATEWAY_PORT": str(_pick_free_port()),
    })
    # Remote secrets are already in the private config, never forwarded as
    # test-runner variables to model tool subprocesses.
    env.pop("HEARTBEAT_REMOTE_API_KEY", None)
    env.pop("HEARTBEAT_REMOTE_API_BASE", None)
    return env, web_port


def _start_service(command: list[str], *, env: dict[str, str], log_path: Path):
    # Some existing Native integrations create a relative memory.db. Keep the
    # entire subprocess working directory inside this fixture's owned scope.
    with log_path.open("w", encoding="utf-8") as log:
        return subprocess.Popen(command, cwd=log_path.parent, env=env,
                                stdout=log, stderr=subprocess.STDOUT, text=True)


def remove_secret_artifacts(scope: Path) -> list[str]:
    """Remove copied credentials after owned services exit; never print them.

    Codex can snapshot its process environment to shell_snapshots/*.sh even
    when the credential entered solely through the private Provider config.
    Keep ordinary evidence with redaction; never retain such shell snapshots.
    """
    secret = os.environ.get("HEARTBEAT_REMOTE_API_KEY", "").encode()
    if not secret:
        return []
    sanitized = []
    for path in scope.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        content = path.read_bytes()
        if secret not in content:
            continue
        sanitized.append(str(path.relative_to(scope)))
        if "shell_snapshots" in path.parts:
            path.unlink()
        else:
            try:
                content.decode("utf-8")
            except UnicodeDecodeError:
                path.unlink()
            else:
                path.write_bytes(content.replace(secret, b"[REDACTED]"))
    return sanitized


@asynccontextmanager
async def web_services(scope: Path, provider: str, channel: str = "web"):
    data = scope / "data"
    root, profile = configure_workspace(data, provider)
    env, port = service_environment(data, root)
    agent = gateway = None
    try:
        agent = _start_service([sys.executable, "-m", "jiuwenswarm.server.app_agentserver",
                                "--port", env["AGENT_SERVER_PORT"]],
                               env=env, log_path=scope / "agentserver.log")
        await _wait_for_log(scope / "agentserver.log", "ready:", timeout=90)
        gateway = _start_service([sys.executable, "-m", "jiuwenswarm.gateway.app_gateway",
                                  "--port", str(port)], env=env, log_path=scope / "gateway.log")
        await _wait_for_websocket(f"ws://127.0.0.1:{port}/ws", timeout=90)
        url = (f"ws://127.0.0.1:{env['GATEWAY_PORT']}/tui" if channel == "gateway_cli"
               else f"ws://127.0.0.1:{port}/ws")
        await _wait_for_websocket(url, timeout=90)
        yield url, data, profile
    finally:
        _stop_process(gateway)
        _stop_process(agent)
        (data / "config" / "config.yaml").unlink(missing_ok=True)
        sanitized = remove_secret_artifacts(scope)
        (scope / "cleanup.json").write_text(json.dumps({
            "agent_exited": agent is None or agent.poll() is not None,
            "gateway_exited": gateway is None or gateway.poll() is not None,
            "credential_config_removed": not (data / "config" / "config.yaml").exists(),
            "sanitized_artifacts": sanitized,
        }), encoding="utf-8")


async def rpc(ws, method: str, params: dict, request_id: str) -> dict:
    await _send_request(ws, request_id, method, params)
    response, _ = await _receive_until(ws, lambda item: item.get("type") == "res"
                                       and item.get("id") == request_id, timeout=45)
    assert response.get("ok") is True, response
    return response.get("payload") or {}


@asynccontextmanager
async def channel_connection(url: str, channel: str):
    if channel == "web":
        async with websockets.connect(url, max_size=8 * 1024 * 1024) as ws:
            yield ws
        return
    client = GatewayClient(url)

    class CliWire:
        """Reuse assertions through the original CLI client's public codec."""

        async def send(self, raw):
            await client.send_request(json.loads(raw))

        async def recv(self):
            return json.dumps(await client.recv(), ensure_ascii=False)

    try:
        await client.connect()
        yield CliWire()
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["native", "codex"])
@pytest.mark.parametrize("channel", ["web", "gateway_cli"])
async def test_remote_heartbeat_tool_due_history_and_cleanup(tmp_path: Path, provider: str, channel: str):
    async with web_services(tmp_path, provider, channel) as (url, data, profile):
        async with channel_connection(url, channel) as ws:
            params = {"persist_session": True, "mode": "agent.code", "work_mode": "code"}
            if profile:
                params["execution_profile_id"] = profile
            session = await rpc(ws, "session.create", params, "create")
            session_id = session["session_id"]
            name = f"R1-08-{provider}-followup"
            marker = f"R1-08-{provider.upper()}-AUTO-DONE"
            # Due while/after the initiating turn runs; the real scheduler must
            # retain the parent session and execute without another chat.send.
            schedule = {"type": "once", "run_at": time.time() + 30}
            query = (
                "Call heartbeat_create_job exactly once to continue this same task later. "
                f"Use name={name!r}, schedule={json.dumps(schedule)}, enabled=true, max_runs=1, "
                f"prompt='Reply with exactly {marker}. Do not call any tools.'. "
                "Then call heartbeat_list_jobs to verify the created job. "
                "Do not create cron jobs or use shell/files to create it. "
                "After both tools succeed reply exactly R1-08-JOB-CREATED."
            )
            await _send_request(ws, "init", "chat.send", {
                "session_id": session_id, "mode": "agent.code", "query": query, "content": query,
            })
            final, frames = await _receive_until(ws, lambda item: _event(item, "chat.final")
                or item.get("event") in {"chat.error", "execution.error", "runtime.error"}, timeout=180)
            assert _event(final, "chat.final"), frames
            listing = await rpc(ws, "heartbeat.job.list", {"session_id": session_id}, "list")
            jobs = [job for job in listing.get("jobs", []) if job.get("name") == name]
            assert len(jobs) == 1, listing
            job = jobs[0]
            assert job["session_id"] == session_id and job["max_runs"] == 1
            deadline = asyncio.get_running_loop().time() + 180
            while job.get("run_count", 0) < 1 and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(1)
                result = await rpc(ws, "heartbeat.job.get", {"session_id": session_id, "id": job["id"]}, "poll")
                job = result["job"]
            assert job["run_count"] == 1, job
            assert job["run_state"]["last_run_status"] == "succeeded", job
            assert job["enabled"] is False and job["next_run_at"] is None, job
            history_path = data / "agent" / "sessions" / session_id / "history.jsonl"
            records = [json.loads(line) for line in history_path.read_text().splitlines() if line.strip()]
            finals = [item for item in records if item.get("event_type") == "chat.final"
                      and marker in str(item.get("content") or "")]
            assert len(finals) == 1, finals
            await _send_request(ws, "history", "history.get", {
                "session_id": session_id, "cursor": None, "limit": 50,
            })
            _, history_frames = await _receive_until(ws, lambda item: _event(item, "history.message")
                and item.get("payload", {}).get("status") == "done", timeout=45)
            assert marker in json.dumps(history_frames, ensure_ascii=False)
            await rpc(ws, "heartbeat.job.delete", {"session_id": session_id, "id": job["id"]}, "delete")
            after = await rpc(ws, "heartbeat.job.list", {"session_id": session_id}, "list-after")
            assert not any(item.get("id") == job["id"] for item in after.get("jobs", []))
            (tmp_path / "result.json").write_text(json.dumps({
                "provider": provider, "model": os.environ.get("HEARTBEAT_REMOTE_MODEL", "glm-5.2"),
                "channel": channel, "browser_ui": False, "session_id": session_id,
                "model_created_job": True, "automatic_run_count": job["run_count"],
                "terminal_history_count": len(finals), "job_deleted": True,
                "history_channel_readback": True,
            }, indent=2), encoding="utf-8")
