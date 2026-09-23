# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Real Process CLI channel coverage for the Native DeepAgent route."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
import yaml

from jiuwenswarm.common.utils import prepare_workspace

pytestmark = [pytest.mark.integration, pytest.mark.system]

REPO_ROOT = Path(__file__).resolve().parents[2]


class _ChatCompletionsFixture:
    """Small OpenAI-compatible endpoint exercised by the real Native model client."""

    def __init__(self, *, suffix: str = "") -> None:
        self.suffix = suffix
        self.requests: list[dict[str, Any]] = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, _format: str, *_args: Any) -> None:
                return None

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length) or b"{}")
                owner.requests.append(body)
                if self.path != "/v1/chat/completions":
                    self._write_json(404, {"error": {"message": "not found"}})
                    return

                serialized = json.dumps(body, ensure_ascii=False)
                marker = (
                    "R1-04A-NATIVE-SECOND"
                    if "R1-04A-NATIVE-SECOND" in serialized
                    else "R1-04A-NATIVE-FIRST"
                )
                if body.get("stream"):
                    marker += owner.suffix
                    self._write_stream(marker, str(body.get("model") or "local-native"))
                    return
                self._write_json(
                    200,
                    {
                        "id": f"chatcmpl-{len(owner.requests)}",
                        "object": "chat.completion",
                        "created": int(time.time()),
                        "model": body.get("model") or "local-native",
                        "choices": [
                            {
                                "index": 0,
                                "message": {"role": "assistant", "content": marker},
                                "finish_reason": "stop",
                            }
                        ],
                        "usage": {
                            "prompt_tokens": 4,
                            "completion_tokens": 4,
                            "total_tokens": 8,
                        },
                    },
                )

            def _write_json(self, status: int, payload: dict[str, Any]) -> None:
                encoded = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def _write_stream(self, marker: str, model: str) -> None:
                common = {
                    "id": f"chatcmpl-{len(owner.requests)}",
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": model,
                }
                events = [
                    {
                        **common,
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"role": "assistant", "content": marker},
                                "finish_reason": None,
                            }
                        ],
                    },
                    {
                        **common,
                        "choices": [
                            {
                                "index": 0,
                                "delta": {},
                                "finish_reason": "stop",
                            }
                        ],
                    },
                    {
                        **common,
                        "choices": [],
                        "usage": {
                            "prompt_tokens": 4,
                            "completion_tokens": 4,
                            "total_tokens": 8,
                        },
                    },
                ]
                encoded = (
                    "".join(
                        f"data: {json.dumps(event, separators=(',', ':'))}\n\n"
                        for event in events
                    )
                    + "data: [DONE]\n\n"
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}/v1"

    def __enter__(self) -> _ChatCompletionsFixture:
        self.thread.start()
        return self

    def __exit__(self, *_args: Any) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def _configure_workspace(data_dir: Path, base_url: str) -> Path:
    prepare_workspace(overwrite=False, preferred_language="zh", workspace_dir=data_dir)
    root = (data_dir / "agent" / "workspace").resolve()
    config_path = data_dir / "config" / "config.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["models"]["defaults"] = [
        {
            "model_client_config": {
                "api_base": base_url,
                "api_key": "local-only",
                "model_name": "local-native",
                "client_provider": "OpenAI",
                "endpoint_profile": "openai_compatible",
                "timeout": 30,
                "stream_first_chunk_timeout": 10,
                "stream_idle_timeout": 10,
                "verify_ssl": False,
                "custom_headers": {},
            },
            "model_config_obj": {"context_window": 32768},
            "is_default": True,
        }
    ]
    config_path.write_text(
        yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    return root


def _run_turn(
    *, data_dir: Path, root: Path, prompt: str, session_id: str | None = None
) -> dict[str, Any]:
    command = [
        sys.executable,
        "-m",
        "jiuwenswarm.channels.process_cli.main",
        prompt,
        "--cwd",
        str(root),
        "--project-dir",
        str(root),
        "--trusted-dir",
        str(root),
        "--mode",
        "agent.work.normal",
        "--work-mode",
        "work",
        "--output",
        "json",
        "--timeout",
        "60",
    ]
    if session_id:
        command.extend(("--session", session_id))
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(data_dir / "home"),
            "JIUWENSWARM_DATA_DIR": str(data_dir),
            "NO_PROXY": "127.0.0.1,localhost",
            "no_proxy": "127.0.0.1,localhost",
        }
    )
    completed = subprocess.run(
        command,
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=90,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def _response_text(document: dict[str, Any]) -> str:
    return "".join(
        str((event.get("payload") or {}).get(key) or "")
        for event in document.get("events") or []
        for key in ("delta", "content", "text", "answer")
    )


def test_native_process_cli_two_turn_history_is_durable(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    with _ChatCompletionsFixture() as fixture:
        root = _configure_workspace(data_dir, fixture.base_url)
        first = _run_turn(
            data_dir=data_dir,
            root=root,
            prompt="只回复：R1-04A-NATIVE-FIRST",
        )
        session_id = str(first.get("session_id") or "")
        assert first.get("ok") is True
        assert session_id.startswith("process_cli_")
        assert "R1-04A-NATIVE-FIRST" in _response_text(first)

        second = _run_turn(
            data_dir=data_dir,
            root=root,
            session_id=session_id,
            prompt="只回复：R1-04A-NATIVE-SECOND",
        )
        assert second.get("ok") is True
        assert second.get("session_id") == session_id
        assert "R1-04A-NATIVE-SECOND" in _response_text(second)

    history_path = data_dir / "agent" / "sessions" / session_id / "history.jsonl"
    records = [
        json.loads(line)
        for line in history_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    finals = [record for record in records if record.get("event_type") == "chat.final"]
    context_records = [
        record
        for record in records
        if record.get("event_type") == "context.usage"
    ]
    assert any("R1-04A-NATIVE-FIRST" in str(record.get("content")) for record in finals)
    assert any("R1-04A-NATIVE-SECOND" in str(record.get("content")) for record in finals)
    assert len(context_records) == 2
    delivery_ids = [
        str(record.get("delivery_id") or "")
        for record in finals + context_records
    ]
    assert all(delivery_ids)
    assert len(delivery_ids) == len(set(delivery_ids))
    assert any(request.get("stream") is True for request in fixture.requests)


def test_native_large_result_is_complete_in_cli_and_durable_history(tmp_path: Path) -> None:
    suffix = "".join(f"R1-04C-{index:05d}-COMPLETE " for index in range(14000))
    data_dir = tmp_path / "data"
    with _ChatCompletionsFixture(suffix=suffix) as fixture:
        root = _configure_workspace(data_dir, fixture.base_url)
        result = _run_turn(data_dir=data_dir, root=root, prompt="只回复：R1-04A-NATIVE-FIRST")
        assert result["ok"] is True
        assert "R1-04A-NATIVE-FIRST" + suffix in _response_text(result)
    history_path = data_dir / "agent" / "sessions" / result["session_id"] / "history.jsonl"
    records = [json.loads(line) for line in history_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert any(record.get("event_type") == "chat.final" and record.get("content") == "R1-04A-NATIVE-FIRST" + suffix
               for record in records)
