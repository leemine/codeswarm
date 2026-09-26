# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Run an isolated Web fixture for the managed sql2java OpenCode plugin.

This helper is intentionally local-only: it serves a deterministic loopback
model, starts AgentServer and Gateway, and waits until interrupted.  The Vite
frontend is started separately so a real browser can exercise the page.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
from pathlib import Path

import yaml
from aiohttp import web

from jiuwenswarm.common.utils import prepare_workspace
from openjiuwen.harness_providers.opencode import opencode_plugin_content_digest


def _free_port(preferred: int) -> int:
    with socket.socket() as sock:
        try:
            sock.bind(("127.0.0.1", preferred))
        except OSError:
            sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _terminate(process: subprocess.Popen[bytes] | None) -> None:
    if process is None or process.poll() is not None:
        return
    os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)


async def _wait_for(path: Path, marker: str, *, timeout: float = 90) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if path.is_file() and marker in path.read_text(encoding="utf-8", errors="replace"):
            return
        await asyncio.sleep(0.1)
    raise TimeoutError(f"did not observe {marker!r} in {path}")


class _Model:
    async def respond(self, request: web.Request) -> web.Response:
        body = await request.json()
        messages = body.get("messages") or []
        tool_results = [message for message in messages if isinstance(message, dict) and message.get("role") == "tool"]
        if len(tool_results) >= 2:
            delta = {"content": "OC-P-SQL2JAVA-PAGE-PASS：workflow 与 saveArtifact 工具均已通过页面调用。"}
            finish = "stop"
        elif tool_results:
            delta = {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call_sql2java_save_artifact",
                        "type": "function",
                        "function": {
                            "name": "saveArtifact",
                            "arguments": json.dumps(
                                {"path": "page-regression.txt", "content": "OC-P-SQL2JAVA-PAGE"}
                            ),
                        },
                    }
                ]
            }
            finish = "tool_calls"
        else:
            delta = {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call_sql2java_page",
                        "type": "function",
                        "function": {"name": "workflow", "arguments": json.dumps({"action": "list"})},
                    }
                ]
            }
            finish = "tool_calls"
        base = {
            "id": "chatcmpl-sql2java-page",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "fixture",
        }
        chunks = [
            dict(base, choices=[{"index": 0, "delta": {"role": "assistant", **delta}, "finish_reason": None}]),
            dict(
                base,
                choices=[{"index": 0, "delta": {}, "finish_reason": finish}],
                usage={"prompt_tokens": 19, "completion_tokens": 7, "total_tokens": 26},
            ),
        ]
        return web.Response(
            text="".join("data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n" for chunk in chunks)
            + "data: [DONE]\n\n",
            content_type="text/event-stream",
        )


async def _main(args: argparse.Namespace) -> None:
    state = args.state_dir.resolve()
    plugin_root = args.plugin_root.resolve(strict=True)
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    data_dir = state / "data"
    data_dir.mkdir(mode=0o700, exist_ok=True)
    runtime_root = state / "runtime"
    runtime_root.mkdir(mode=0o700, exist_ok=True)
    home = state / "home"
    home.mkdir(mode=0o700, exist_ok=True)
    tasks = state / "tasks"
    tasks.mkdir(mode=0o700, exist_ok=True)

    model_port = _free_port(args.model_port)
    agent_port = _free_port(args.agent_port)
    gateway_port = _free_port(args.gateway_port)
    gateway_control_port = _free_port(args.gateway_control_port)
    model = _Model()
    app = web.Application()
    app.router.add_post("/v1/chat/completions", model.respond)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", model_port).start()

    prepare_workspace(overwrite=False, preferred_language="zh", workspace_dir=data_dir)
    config_path = data_dir / "config" / "config.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["permissions"]["enabled"] = not args.full_access
    config["execution"] = {
        "default_profile_id": "opencode-sql2java-page",
        "profiles": {
            "opencode-sql2java-page": {
                "provider_id": "opencode",
                "config_revision": f"oc-p3-{args.plugin_version}",
                "authorization": {"full_access": args.full_access},
                "provider_config": {
                    "cli_path": str(args.cli_path.resolve(strict=True)),
                    "runtime_root": str(runtime_root),
                    "model": {
                        "model": "fixture",
                        "provider": "openjiuwen",
                        "api_base": f"http://127.0.0.1:{model_port}/v1",
                        "api_key": "fixture-only",
                    },
                    "startup_timeout_s": 90,
                    "turn_timeout_s": 90,
                    "native_plugins": [
                        {
                            "plugin_id": "sql2java-workflow",
                            "source_type": "local",
                            "source_locator": str(plugin_root),
                            "version": args.plugin_version,
                            "content_sha256": opencode_plugin_content_digest(plugin_root),
                            "entrypoint": "plugins/workflow-engine.ts",
                            "export_name": "WorkflowEnginePlugin",
                            "required_hooks": [
                                "chat.message",
                                "chat.params",
                                "event",
                                "experimental.chat.system.transform",
                                "tool.execute.before",
                                "tool.execute.after",
                            ],
                            "required_tools": ["saveArtifact", "workflow"],
                        }
                    ],
                },
            }
        },
    }
    config_path.write_text(yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding="utf-8")
    config_path.chmod(0o600)

    environment = os.environ.copy()
    environment.update(
        {
            "HOME": str(home),
            "JIUWENSWARM_DATA_DIR": str(data_dir),
            "JIUWENSWARM_TASKS_DIR": str(tasks),
            "JIUWENSWARM_RUNTIME_WORKSPACE_READY": "1",
            "AGENT_SERVER_HOST": "127.0.0.1",
            "AGENT_SERVER_PORT": str(agent_port),
            "WEB_HOST": "127.0.0.1",
            "WEB_PORT": str(gateway_port),
            "GATEWAY_HOST": "127.0.0.1",
            "GATEWAY_PORT": str(gateway_control_port),
        }
    )
    agent_log = state / "agentserver.log"
    gateway_log = state / "gateway.log"
    agent_process = gateway_process = None
    try:
        with agent_log.open("wb") as log:
            agent_process = subprocess.Popen(
                [sys.executable, "-m", "jiuwenswarm.server.app_agentserver", "--port", str(agent_port)],
                env=environment,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        await _wait_for(agent_log, "ready:")
        with gateway_log.open("wb") as log:
            gateway_process = subprocess.Popen(
                [sys.executable, "-m", "jiuwenswarm.gateway.app_gateway", "--port", str(gateway_port)],
                env=environment,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        await _wait_for(gateway_log, "Application startup complete")
        print(
            json.dumps(
                {
                    "status": "ready",
                    "gateway": f"http://127.0.0.1:{gateway_port}",
                    "gateway_control_port": gateway_control_port,
                    "agent_port": agent_port,
                    "model_port": model_port,
                    "state_dir": str(state),
                    "plugin_root": str(plugin_root),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        await asyncio.Event().wait()
    finally:
        _terminate(gateway_process)
        _terminate(agent_process)
        await runner.cleanup()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--plugin-root", type=Path, required=True)
    parser.add_argument("--plugin-version", required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--cli-path", type=Path, default=Path.home() / ".opencode/bin/opencode")
    parser.add_argument("--model-port", type=int, default=19002)
    parser.add_argument("--agent-port", type=int, default=19001)
    parser.add_argument("--gateway-port", type=int, default=19000)
    parser.add_argument("--gateway-control-port", type=int, default=19003)
    parser.add_argument("--full-access", action="store_true")
    try:
        asyncio.run(_main(parser.parse_args()))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
