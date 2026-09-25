# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Opt-in OC6 Web and Process CLI qualification with a remote model.

RUN_OPENCODE_OC6=1 OPENCODE_OC6_API_KEY=... pytest \
    tests/system_tests/test_external_opencode_channels_remote.py

The execution profile is server-owned and writes the supplied credential only
to the test's private temporary configuration tree.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import websockets
import yaml

from jiuwenswarm.common.utils import prepare_workspace

from .test_external_codex_web_channel_local import (
    _event,
    _pick_free_port,
    _receive_until,
    _send_request,
    _start_process,
    _stop_process,
    _wait_for_log,
    _wait_for_websocket,
)


pytestmark = [
    pytest.mark.integration,
    pytest.mark.system,
    pytest.mark.skipif(
        os.environ.get("RUN_OPENCODE_OC6") != "1",
        reason="real remote OpenCode channel qualification is opt-in",
    ),
]

REPO_ROOT = Path(__file__).resolve().parents[2]
PROFILE_ID = "opencode-oc6-remote"


def _required_environment(name: str) -> str:
    value = os.environ.get(name, "").strip()
    assert value, f"{name} is required when RUN_OPENCODE_OC6=1"
    return value


def _configure_workspace(
    data_dir: Path,
    runtime_root: Path,
    *,
    full_access: bool,
) -> Path:
    prepare_workspace(overwrite=False, preferred_language="zh", workspace_dir=data_dir)
    root = (data_dir / "agent" / "workspace").resolve()
    config_path = data_dir / "config" / "config.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["permissions"]["enabled"] = not full_access
    config["execution"] = {
        "default_profile_id": PROFILE_ID,
        "profiles": {
            PROFILE_ID: {
                "provider_id": "opencode",
                "config_revision": "oc6-remote-r1",
                "authorization": {"full_access": full_access},
                "provider_config": {
                    "cli_path": os.environ.get(
                        "OPENCODE_OC6_CLI",
                        os.path.expanduser("~/.opencode/bin/opencode"),
                    ),
                    "runtime_root": str(runtime_root),
                    "model": {
                        "model": os.environ.get("OPENCODE_OC6_MODEL", "glm-5.2"),
                        "provider": "oc6_volcano",
                        "api_base": _required_environment("OPENCODE_OC6_API_BASE"),
                        "api_key": _required_environment("OPENCODE_OC6_API_KEY"),
                    },
                    "turn_timeout_s": 180,
                },
            }
        },
    }
    config_path.write_text(
        yaml.safe_dump(config, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    config_path.chmod(0o600)
    return root


async def _create_web_session(ws: Any) -> str:
    await _send_request(
        ws,
        "create",
        "session.create",
        {
            "persist_session": True,
            "mode": "agent.code",
            "work_mode": "code",
            "execution_profile_id": PROFILE_ID,
        },
    )
    response, _ = await _receive_until(
        ws,
        lambda frame: frame.get("type") == "res" and frame.get("id") == "create",
    )
    assert response["ok"] is True, response
    session_id = str(response.get("payload", {}).get("session_id") or "")
    assert session_id.startswith("web_"), response
    return session_id


def _service_environment(data_dir: Path, root: Path, tmp_path: Path) -> tuple[dict[str, str], int]:
    agent_port = _pick_free_port()
    web_port = _pick_free_port()
    gateway_port = _pick_free_port()
    environment = os.environ.copy()
    environment.update(
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
    return environment, web_port


@pytest.mark.asyncio
async def test_remote_web_question_survives_disconnect_and_refresh(tmp_path: Path) -> None:
    data_dir = (tmp_path / "web-data").resolve()
    runtime_root = (tmp_path / "web-runtime").resolve()
    data_dir.mkdir()
    runtime_root.mkdir(mode=0o700)
    root = _configure_workspace(data_dir, runtime_root, full_access=False)
    environment, web_port = _service_environment(data_dir, root, tmp_path)
    agent_process = None
    gateway_process = None
    agent_log = tmp_path / "agentserver.log"
    gateway_log = tmp_path / "gateway.log"
    try:
        agent_process = _start_process(
            [
                sys.executable,
                "-m",
                "jiuwenswarm.server.app_agentserver",
                "--port",
                environment["AGENT_SERVER_PORT"],
            ],
            env=environment,
            log_path=agent_log,
        )
        await _wait_for_log(agent_log, "ready:", timeout=60)
        gateway_process = _start_process(
            [
                sys.executable,
                "-m",
                "jiuwenswarm.gateway.app_gateway",
                "--port",
                str(web_port),
            ],
            env=environment,
            log_path=gateway_log,
        )
        url = f"ws://127.0.0.1:{web_port}/ws"
        await _wait_for_websocket(url, timeout=60)
        async with websockets.connect(url) as ws:
            session_id = await _create_web_session(ws)
            await _send_request(
                ws,
                "question",
                "chat.send",
                {
                    "session_id": session_id,
                    "mode": "agent.code",
                    "query": (
                        "Call the question tool exactly once. Ask `Continue OC6?` with "
                        "header `OC6` and one option labelled `Yes`. Wait for the answer. "
                        "After the user answers, reply with exactly OC6-WEB-ANSWERED."
                    ),
                },
            )
            frames: list[dict[str, Any]] = []
            approval_count = 0
            while True:
                question, received = await _receive_until(
                    ws,
                    lambda frame: _event(frame, "chat.ask_user_question")
                    or frame.get("event")
                    in {"chat.error", "execution.error", "runtime.error"},
                    timeout=180,
                )
                frames.extend(received)
                assert _event(question, "chat.ask_user_question"), frames
                payload = question["payload"]
                if payload.get("source") == "ask_user_interrupt":
                    break

                assert payload.get("source") == "permission_interrupt", payload
                approval_count += 1
                assert approval_count <= 3, frames
                await _send_request(
                    ws,
                    f"approval-{approval_count}",
                    "chat.send",
                    {
                        "session_id": session_id,
                        "mode": "agent.code",
                        "query": "",
                        "request_id": payload["request_id"],
                        "source": payload["source"],
                        "session_generation": payload["session_generation"],
                        "answers": [{"selected_options": ["allow_once"]}],
                    },
                )

            assert isinstance(payload.get("session_generation"), int)
            assert payload["session_generation"] > 0
            questions = payload.get("questions") or []
            assert questions, payload
            rendered_question = json.dumps(questions, ensure_ascii=False)
            assert "Continue OC6?" in rendered_question
            options = questions[0].get("options") or []
            assert options and (options[0].get("value") or options[0].get("label")), payload
            answer_value = options[0].get("value") or options[0]["label"]

        await asyncio.sleep(1)
        async with websockets.connect(url) as ws:
            await _send_request(
                ws,
                "answer",
                "chat.send",
                {
                    "session_id": session_id,
                    "mode": "agent.code",
                    "query": "",
                    "request_id": payload["request_id"],
                    "source": payload["source"],
                    "session_generation": payload["session_generation"],
                    "answers": [
                        {
                            "question": questions[0]["question"],
                            "selected_options": [answer_value],
                        }
                    ],
                },
            )
            final, answer_frames = await _receive_until(
                ws,
                lambda frame: _event(frame, "chat.final")
                or frame.get("event") in {"chat.error", "execution.error", "runtime.error"},
                timeout=180,
            )
            assert _event(final, "chat.final"), answer_frames
            answer_text = "".join(
                str(frame.get("payload", {}).get("content") or "")
                for frame in answer_frames
                if _event(frame, "chat.delta") or _event(frame, "chat.final")
            )
            assert "OC6-WEB-ANSWERED" in answer_text, answer_frames

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
            assert "OC6-WEB-ANSWERED" in json.dumps(history_frames, ensure_ascii=False)
    finally:
        _stop_process(gateway_process)
        _stop_process(agent_process)

    assert not list(runtime_root.rglob("owner.json"))


def _run_process_cli(
    *,
    data_dir: Path,
    root: Path,
    prompt: str,
    session_id: str | None = None,
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
        "agent.code",
        "--work-mode",
        "code",
        "--output",
        "json",
        "--timeout",
        "240",
    ]
    if session_id:
        command.extend(("--session", session_id))
    else:
        command.extend(("--execution-profile", PROFILE_ID))
    environment = os.environ.copy()
    environment.update(
        {
            "HOME": str(data_dir / "home"),
            "JIUWENSWARM_DATA_DIR": str(data_dir),
        }
    )
    completed = subprocess.run(
        command,
        cwd=REPO_ROOT,
        env=environment,
        text=True,
        capture_output=True,
        timeout=300,
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


def test_remote_process_cli_parent_child_and_history(tmp_path: Path) -> None:
    data_dir = (tmp_path / "cli-data").resolve()
    runtime_root = (tmp_path / "cli-runtime").resolve()
    data_dir.mkdir()
    runtime_root.mkdir(mode=0o700)
    root = _configure_workspace(data_dir, runtime_root, full_access=True)

    first = _run_process_cli(
        data_dir=data_dir,
        root=root,
        prompt=(
            "Use the subagent spawn tool to create one verification_agent whose task is to "
            "reply exactly OC6-CLI-CHILD. Wait for that child with the subagent wait tool. "
            "After receiving its result, reply with exactly OC6-CLI-PARENT."
        ),
    )
    session_id = str(first.get("session_id") or "")
    assert first.get("ok") is True, first
    assert session_id.startswith("process_cli_"), first
    assert "OC6-CLI-PARENT" in _response_text(first)

    second = _run_process_cli(
        data_dir=data_dir,
        root=root,
        session_id=session_id,
        prompt="Reply with exactly OC6-CLI-HISTORY.",
    )
    assert second.get("ok") is True, second
    assert second.get("session_id") == session_id
    assert "OC6-CLI-HISTORY" in _response_text(second)

    history_path = data_dir / "agent" / "sessions" / session_id / "history.jsonl"
    history = history_path.read_text(encoding="utf-8")
    assert "OC6-CLI-CHILD" in history
    assert "OC6-CLI-PARENT" in history
    assert "OC6-CLI-HISTORY" in history
    assert not list(runtime_root.rglob("owner.json"))
