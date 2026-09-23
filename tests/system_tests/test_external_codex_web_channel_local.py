# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Real WebSocket channel coverage for the External Codex product route."""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
import websockets
import yaml

from jiuwenswarm.common.utils import prepare_workspace

pytestmark = [pytest.mark.integration, pytest.mark.system]

REPO_ROOT = Path(__file__).resolve().parents[2]


def _pick_free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _start_process(
    cmd: list[str], *, env: dict[str, str], log_path: Path
) -> subprocess.Popen:
    log_file = log_path.open("w", encoding="utf-8")
    process = subprocess.Popen(
        cmd,
        cwd=str(REPO_ROOT),
        env=env,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        text=True,
    )
    log_file.close()
    return process


def _stop_process(process: subprocess.Popen | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)


async def _wait_for_log(path: Path, needle: str, timeout: float = 60.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if path.exists() and needle in path.read_text(encoding="utf-8", errors="ignore"):
            return
        await asyncio.sleep(0.2)
    text = path.read_text(encoding="utf-8", errors="ignore") if path.exists() else ""
    raise AssertionError(f"Timed out waiting for {needle!r}\n{text}")


async def _wait_for_websocket(url: str, timeout: float = 60.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    last_error: Exception | None = None
    while asyncio.get_running_loop().time() < deadline:
        try:
            async with websockets.connect(url):
                return
        except Exception as error:  # noqa: BLE001 - readiness probe
            last_error = error
            await asyncio.sleep(0.2)
    raise AssertionError(f"WebSocket did not become ready: {last_error}")


async def _send_request(ws: Any, request_id: str, method: str, params: dict) -> None:
    await ws.send(
        json.dumps(
            {"type": "req", "id": request_id, "method": method, "params": params},
            ensure_ascii=False,
        )
    )


async def _receive_until(ws: Any, predicate, timeout: float = 30.0) -> tuple[dict, list[dict]]:
    frames: list[dict] = []
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        remaining = max(0.1, deadline - asyncio.get_running_loop().time())
        frame = json.loads(await asyncio.wait_for(ws.recv(), timeout=remaining))
        frames.append(frame)
        if predicate(frame):
            return frame, frames
    raise AssertionError(f"Timed out; received frames={frames}")


def _event(frame: dict, event_type: str) -> bool:
    return frame.get("type") == "event" and frame.get("event") == event_type


def _sse(name: str, payload: dict) -> bytes:
    return (
        f"event: {name}\ndata: "
        + json.dumps({"type": name, **payload})
        + "\n\n"
    ).encode()


class _WebResponsesFixture:
    """Prompt-routed Responses fixture used by the real bundled Codex CLI."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.requests: list[dict] = []
        self.tool_issued: set[str] = set()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, _format, *_args):
                return None

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                owner.requests.append(body)
                serialized = json.dumps(body, ensure_ascii=False)
                index = len(owner.requests)

                if "R1-A2-WEB-ALLOW" in serialized and "allow" not in owner.tool_issued:
                    owner.tool_issued.add("allow")
                    item = owner._tool_item(
                        index,
                        f"printf R1-A2-WEB-ALLOW > {shlex.quote(str(owner.root / 'allow-marker.txt'))}",
                    )
                elif "R1-A2-WEB-DENY" in serialized and "deny" not in owner.tool_issued:
                    owner.tool_issued.add("deny")
                    item = owner._tool_item(
                        index,
                        f"printf R1-A2-WEB-DENY > {shlex.quote(str(owner.root / 'deny-marker.txt'))}",
                    )
                elif "R1-A2-WEB-STOP" in serialized and "stop" not in owner.tool_issued:
                    owner.tool_issued.add("stop")
                    item = owner._tool_item(index, "sleep 30")
                elif "R1-A2-WEB-DISCONNECT" in serialized:
                    self._send_delayed_message(index)
                    return
                elif "R1-04B-WEB-FAIL" in serialized:
                    self._send_failure()
                    return
                else:
                    item = owner._message_item(index, f"R1-A2-WEB-FINAL-{index}")
                self._send_item(index, item)

            def _send_failure(self) -> None:
                data = json.dumps(
                    {
                        "error": {
                            "code": "r1_04b_fixture_failure",
                            "message": "R1-04B-WEB-PROVIDER-FAILED",
                        }
                    }
                ).encode()
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _send_item(self, index: int, item: dict) -> None:
                response = owner._response(index)
                data = b"".join(
                    (
                        _sse("response.created", {"response": response}),
                        _sse(
                            "response.output_item.added",
                            {"output_index": 0, "item": item},
                        ),
                        _sse(
                            "response.output_item.done",
                            {"output_index": 0, "item": item},
                        ),
                        _sse("response.completed", {"response": owner._completed(response, item)}),
                    )
                )
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _send_delayed_message(self, index: int) -> None:
                response = owner._response(index)
                pending = {
                    "type": "message",
                    "role": "assistant",
                    "id": f"msg_{index}",
                    "status": "in_progress",
                    "content": [],
                }
                done = owner._message_item(index, "R1-A2-WEB-DISCONNECT-FINAL")
                before = b"".join(
                    (
                        _sse("response.created", {"response": response}),
                        _sse(
                            "response.output_item.added",
                            {"output_index": 0, "item": pending},
                        ),
                        _sse(
                            "response.output_text.delta",
                            {
                                "item_id": pending["id"],
                                "output_index": 0,
                                "content_index": 0,
                                "delta": "R1-A2-WEB-DISCONNECT-",
                            },
                        ),
                    )
                )
                after = b"".join(
                    (
                        _sse(
                            "response.output_text.delta",
                            {
                                "item_id": pending["id"],
                                "output_index": 0,
                                "content_index": 0,
                                "delta": "FINAL",
                            },
                        ),
                        _sse(
                            "response.output_text.done",
                            {
                                "item_id": pending["id"],
                                "output_index": 0,
                                "content_index": 0,
                                "text": "R1-A2-WEB-DISCONNECT-FINAL",
                            },
                        ),
                        _sse(
                            "response.output_item.done",
                            {"output_index": 0, "item": done},
                        ),
                        _sse("response.completed", {"response": owner._completed(response, done)}),
                    )
                )
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(before) + len(after)))
                self.end_headers()
                self.wfile.write(before)
                self.wfile.flush()
                time.sleep(2.0)
                try:
                    self.wfile.write(after)
                    self.wfile.flush()
                except BrokenPipeError:
                    # The model connection is owned by AgentServer, not the browser.
                    # A browser disconnect must therefore normally leave it open.
                    return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @staticmethod
    def _response(index: int) -> dict:
        return {
            "id": f"resp_{index}",
            "object": "response",
            "status": "in_progress",
            "output": [],
        }

    @staticmethod
    def _completed(response: dict, item: dict) -> dict:
        return {
            **response,
            "status": "completed",
            "output": [item],
            "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        }

    @staticmethod
    def _message_item(index: int, text: str) -> dict:
        return {
            "type": "message",
            "role": "assistant",
            "id": f"msg_{index}",
            "status": "completed",
            "content": [{"type": "output_text", "text": text, "annotations": []}],
        }

    @staticmethod
    def _tool_item(index: int, command: str) -> dict:
        return {
            "type": "function_call",
            "name": "exec_command",
            "id": f"fc_{index}",
            "call_id": f"call_{index}",
            "arguments": json.dumps({"cmd": command}),
        }

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}/v1"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def _configure_workspace(data_dir: Path, codex_home: Path, base_url: str) -> Path:
    prepare_workspace(overwrite=False, preferred_language="zh", workspace_dir=data_dir)
    root = (data_dir / "agent" / "workspace").resolve()
    config_path = data_dir / "config" / "config.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["execution"] = {
        "default_profile_id": "codex-web-local",
        "profiles": {
            "codex-web-local": {
                "provider_id": "codex",
                "config_revision": "r1-a2-web-local",
                "provider_config": {
                    "inherit_process_env": False,
                    "env": {
                        "HOME": str(data_dir / "provider-home"),
                        "CODEX_HOME": str(codex_home),
                        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                    },
                    "startup_source_roots": [str(root), str(codex_home / "skills")],
                    # ProductToolGateway injects a managed loopback MCP into
                    # this real channel path; restricted Codex sessions only
                    # admit that server when the profile marks it required.
                    "mcp_required": True,
                    "mcp_default_tools_approval_mode": "prompt",
                    "model": {
                        "model": "gpt-5.6-sol",
                        "provider": "r1_a2_web_fixture",
                        "api_base": base_url,
                        "api_key": "local-only",
                    },
                },
            }
        },
    }
    config_path.write_text(
        yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    return root


def _configure_codex(codex_home: Path, root: Path, binary: str) -> None:
    (codex_home / "skills").mkdir(parents=True)
    readable = {
        ":minimal": "read",
        str(root): "read",
        str(codex_home / "tmp"): "read",
        str(Path(binary).parent): "read",
    }
    lines = ['default_permissions = "r1-a2-web-read"', "[permissions.r1-a2-web-read.filesystem]"]
    lines.extend(f"{json.dumps(path)} = {json.dumps(access)}" for path, access in readable.items())
    lines.extend(("[permissions.r1-a2-web-read.network]", "enabled=false"))
    (codex_home / "config.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")


async def _create_session(ws: Any) -> str:
    await _send_request(
        ws,
        "create",
        "session.create",
        {
            "persist_session": True,
            "mode": "agent.code",
            "work_mode": "code",
            "execution_profile_id": "codex-web-local",
        },
    )
    response, _ = await _receive_until(
        ws, lambda frame: frame.get("type") == "res" and frame.get("id") == "create"
    )
    assert response["ok"] is True, response
    session_id = str(response.get("payload", {}).get("session_id") or "").strip()
    assert session_id.startswith("web_"), response
    return session_id


async def _chat_until_question(ws: Any, request_id: str, session_id: str, query: str) -> dict:
    await _send_request(
        ws,
        request_id,
        "chat.send",
        {"session_id": session_id, "mode": "agent.code", "query": query},
    )
    question, frames = await _receive_until(
        ws,
        lambda frame: _event(frame, "chat.ask_user_question")
        or frame.get("event") in {"chat.error", "execution.error", "runtime.error"}
        or (frame.get("type") == "res" and frame.get("ok") is False),
        timeout=40,
    )
    assert _event(question, "chat.ask_user_question"), frames
    statuses = {
        frame.get("payload", {}).get("submission_status")
        for frame in frames
        if _event(frame, "runtime.accepted")
    }
    assert {"harness_accepted", "provider_accepted"} <= statuses, frames
    payload = question["payload"]
    assert isinstance(payload.get("session_generation"), int), frames
    assert payload["session_generation"] > 0
    return payload


async def _answer_and_wait_final(
    ws: Any, request_id: str, session_id: str, question: dict, option: str
) -> None:
    await _send_request(
        ws,
        request_id,
        "chat.send",
        {
            "session_id": session_id,
            "mode": "agent.code",
            "query": "",
            "request_id": question["request_id"],
            "source": question["source"],
            "session_generation": question["session_generation"],
            "answers": [{"selected_options": [option]}],
        },
    )
    final, _ = await _receive_until(ws, lambda frame: _event(frame, "chat.final"), timeout=40)
    assert final.get("event") == "chat.final"


@pytest.mark.asyncio
async def test_external_codex_web_approval_stop_disconnect_and_history(tmp_path: Path) -> None:
    sdk = pytest.importorskip(
        "openai_codex", reason="optional Codex SDK and bundled CLI required"
    )
    data_dir = (tmp_path / "data").resolve()
    codex_home = (tmp_path / "codex-home").resolve()
    provider_home = data_dir / "provider-home"
    data_dir.mkdir()
    codex_home.mkdir()
    provider_home.mkdir(parents=True)

    binary = sdk.client._resolve_codex_bin(sdk.CodexConfig())
    agent_port = _pick_free_port()
    web_port = _pick_free_port()
    gateway_port = _pick_free_port()
    agent_process = None
    gateway_process = None

    with _WebResponsesFixture(data_dir / "agent" / "workspace") as responses:
        root = _configure_workspace(data_dir, codex_home, responses.base_url)
        _configure_codex(codex_home, root, binary)
        env = os.environ.copy()
        env.update(
            {
                "HOME": str(tmp_path / "service-home"),
                "JIUWENSWARM_DATA_DIR": str(data_dir),
                "JIUWENSWARM_TASKS_DIR": str(root / "projectless-tasks"),
                "JIUWENSWARM_RUNTIME_WORKSPACE_READY": "1",
                "AGENT_SERVER_HOST": "127.0.0.1",
                "AGENT_SERVER_PORT": str(agent_port),
                "WEB_HOST": "127.0.0.1",
                "WEB_PORT": str(web_port),
                "GATEWAY_HOST": "127.0.0.1",
                "GATEWAY_PORT": str(gateway_port),
            }
        )
        agent_log = tmp_path / "agentserver.log"
        gateway_log = tmp_path / "gateway.log"
        try:
            agent_process = _start_process(
                [sys.executable, "-m", "jiuwenswarm.server.app_agentserver", "--port", str(agent_port)],
                env=env,
                log_path=agent_log,
            )
            await _wait_for_log(agent_log, "ready:")
            gateway_process = _start_process(
                [sys.executable, "-m", "jiuwenswarm.gateway.app_gateway", "--port", str(web_port)],
                env=env,
                log_path=gateway_log,
            )
            url = f"ws://127.0.0.1:{web_port}/ws"
            await _wait_for_websocket(url)
            async with websockets.connect(url) as ws:
                session_id = await _create_session(ws)

                allow = await _chat_until_question(
                    ws, "allow", session_id, "R1-A2-WEB-ALLOW"
                )
                await _answer_and_wait_final(ws, "allow-answer", session_id, allow, "allow_once")
                assert (root / "allow-marker.txt").read_text(encoding="utf-8") == "R1-A2-WEB-ALLOW"
                provider_calls = len(responses.requests)
                await _send_request(
                    ws,
                    "allow-answer-duplicate",
                    "chat.send",
                    {
                        "session_id": session_id,
                        "mode": "agent.code",
                        "query": "",
                        "request_id": allow["request_id"],
                        "source": allow["source"],
                        "session_generation": allow["session_generation"],
                        "answers": [{"selected_options": ["allow_once"]}],
                    },
                )
                duplicate, duplicate_frames = await _receive_until(
                    ws,
                    lambda frame: _event(frame, "runtime.accepted")
                    and frame.get("payload", {}).get("duplicate") is True,
                    timeout=20,
                )
                assert duplicate["payload"]["submission_status"] == "provider_accepted"
                assert len(responses.requests) == provider_calls, duplicate_frames

                await _send_request(
                    ws,
                    "allow-answer-stale",
                    "chat.send",
                    {
                        "session_id": session_id,
                        "mode": "agent.code",
                        "query": "",
                        "request_id": allow["request_id"],
                        "source": allow["source"],
                        "session_generation": allow["session_generation"] + 1,
                        "answers": [{"selected_options": ["allow_once"]}],
                    },
                )
                stale, stale_frames = await _receive_until(
                    ws,
                    lambda frame: (
                        frame.get("type") == "res"
                        and frame.get("id") == "allow-answer-stale"
                        and frame.get("ok") is False
                    )
                    or frame.get("event")
                    in {"chat.error", "execution.error", "runtime.error"},
                    timeout=20,
                )
                assert "stale Session generation" in json.dumps(
                    [stale, *stale_frames], ensure_ascii=False
                )
                assert len(responses.requests) == provider_calls

                deny = await _chat_until_question(ws, "deny", session_id, "R1-A2-WEB-DENY")
                await _answer_and_wait_final(ws, "deny-answer", session_id, deny, "deny")
                assert not (root / "deny-marker.txt").exists()

                await _send_request(
                    ws,
                    "failed",
                    "chat.send",
                    {
                        "session_id": session_id,
                        "mode": "agent.code",
                        "query": "R1-04B-WEB-FAIL",
                    },
                )
                failed, failed_frames = await _receive_until(
                    ws,
                    lambda frame: _event(frame, "chat.error")
                    and frame.get("payload", {}).get("terminal_status") == "failed",
                    timeout=40,
                )
                assert failed["payload"]["code"]
                assert "R1-04B-WEB-PROVIDER-FAILED" in json.dumps(
                    failed_frames, ensure_ascii=False
                )

                await _send_request(
                    ws,
                    "disconnect",
                    "chat.send",
                    {
                        "session_id": session_id,
                        "mode": "agent.code",
                        "query": "R1-A2-WEB-DISCONNECT",
                    },
                )
                delta, _ = await _receive_until(
                    ws,
                    lambda frame: _event(frame, "chat.delta")
                    and "R1-A2-WEB-DISCONNECT" in str(frame.get("payload", {}).get("content")),
                    timeout=40,
                )
                assert delta

            await asyncio.sleep(3.0)
            async with websockets.connect(url) as ws:
                await _send_request(
                    ws,
                    "history",
                    "history.get",
                    {"session_id": session_id, "cursor": None, "limit": 50},
                )
                _, history_frames = await _receive_until(
                    ws,
                    lambda frame: _event(frame, "history.message")
                    and frame.get("payload", {}).get("status") == "done",
                    timeout=30,
                )
                serialized_history = json.dumps(history_frames, ensure_ascii=False)
                assert "R1-A2-WEB-DISCONNECT-FINAL" in serialized_history
                assert any(_event(frame, "history.message") for frame in history_frames)

                stop = await _chat_until_question(ws, "stop", session_id, "R1-A2-WEB-STOP")
                assert stop["request_id"]
                await _send_request(
                    ws,
                    "stop-cancel",
                    "chat.interrupt",
                    {"session_id": session_id, "mode": "agent.code", "intent": "cancel"},
                )
                cancelled, cancel_frames = await _receive_until(
                    ws,
                    lambda frame: _event(frame, "chat.error")
                    and frame.get("payload", {}).get("terminal_status") == "cancelled",
                    timeout=40,
                )
                assert cancelled["payload"]["code"] == "EXECUTION_CANCELLED"
                interrupt_results = [
                    frame
                    for frame in cancel_frames
                    if (
                    _event(frame, "chat.interrupt_result")
                    and frame.get("payload", {}).get("intent") == "cancel"
                    )
                ]
                if interrupt_results:
                    assert interrupt_results[-1]["payload"]["success"] is True
                else:
                    stopped, trailing_frames = await _receive_until(
                        ws,
                        lambda frame: _event(frame, "chat.interrupt_result")
                        and frame.get("payload", {}).get("intent") == "cancel",
                        timeout=30,
                    )
                    cancel_frames.extend(trailing_frames)
                    assert stopped["payload"]["success"] is True

                await _send_request(
                    ws,
                    "terminal-history",
                    "history.get",
                    {"session_id": session_id, "cursor": None, "limit": 50},
                )
                _, terminal_history = await _receive_until(
                    ws,
                    lambda frame: _event(frame, "history.message")
                    and frame.get("payload", {}).get("status") == "done",
                    timeout=30,
                )
                serialized_terminals = json.dumps(terminal_history, ensure_ascii=False)
                assert '"terminal_status": "failed"' in serialized_terminals
                assert '"terminal_status": "cancelled"' in serialized_terminals
                assert "EXECUTION_CANCELLED" in serialized_terminals
        finally:
            _stop_process(gateway_process)
            _stop_process(agent_process)

    assert responses.requests
    assert agent_process is not None and agent_process.poll() is not None
    assert gateway_process is not None and gateway_process.poll() is not None
