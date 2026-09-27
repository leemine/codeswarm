# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Opt-in real Goal qualification using the shared isolated product fixtures.

RUN_GOAL_REMOTE=1 requires HEARTBEAT_REMOTE_API_BASE, HEARTBEAT_REMOTE_API_KEY
and HEARTBEAT_REMOTE_MODEL. No opt-in skips each collected case; incomplete
explicit configuration fails before any service or model is started.
"""

import asyncio
import json
import os
import sys
from contextlib import asynccontextmanager

import pytest
import yaml

from tests.system_tests.test_goal_channels_remote import (
    _Channel,
    _clear_goal,
    _create,
    _objective,
    _save,
    _verify_completed,
)
from tests.system_tests.test_heartbeat_channels_remote import (
    _start_service,
    _stop_process,
    _wait_for_log,
    _wait_for_websocket,
    channel_connection,
    configure_workspace,
    remove_secret_artifacts,
    service_environment,
    web_services,
)

pytestmark = [pytest.mark.integration, pytest.mark.system]


def _require_remote_goal():
    if os.environ.get("RUN_GOAL_REMOTE") != "1":
        pytest.skip("real Goal qualification requires RUN_GOAL_REMOTE=1")
    missing = [
        name
        for name in (
            "HEARTBEAT_REMOTE_API_BASE",
            "HEARTBEAT_REMOTE_API_KEY",
            "HEARTBEAT_REMOTE_MODEL",
        )
        if not os.environ.get(name, "").strip()
    ]
    if missing:
        pytest.fail(
            "Explicit remote Goal qualification is missing: " + ", ".join(missing)
        )


@asynccontextmanager
async def permission_services(scope):
    # Original fixture composition with only isolated permission config enabled.
    # No product/model patch or alternate execution loop.
    data = scope / "data"
    root, profile = configure_workspace(data, "native")
    config_path = data / "config" / "config.yaml"
    config = yaml.safe_load(config_path.read_text())
    config["permissions"]["enabled"] = True
    config["permissions"]["mode"] = "manual"
    config["permissions"]["tools"].update(
        {
            "bash": "ask",
            "powershell": "ask",
            "mcp_exec_command": "ask",
            "create_terminal": "ask",
        }
    )
    config_path.write_text(yaml.safe_dump(config, allow_unicode=True, sort_keys=False))
    env, port = service_environment(data, root)
    agent = gateway = None
    try:
        agent = _start_service(
            [
                sys.executable,
                "-m",
                "jiuwenswarm.server.app_agentserver",
                "--port",
                env["AGENT_SERVER_PORT"],
            ],
            env=env,
            log_path=scope / "agentserver.log",
        )
        await _wait_for_log(scope / "agentserver.log", "ready:", timeout=90)
        gateway = _start_service(
            [
                sys.executable,
                "-m",
                "jiuwenswarm.gateway.app_gateway",
                "--port",
                str(port),
            ],
            env=env,
            log_path=scope / "gateway.log",
        )
        url = f"ws://127.0.0.1:{port}/ws"
        await _wait_for_websocket(url, timeout=90)
        yield url, data, profile
    finally:
        _stop_process(gateway)
        _stop_process(agent)
        config_path.unlink(missing_ok=True)
        sanitized = remove_secret_artifacts(scope)
        (scope / "cleanup.json").write_text(
            json.dumps(
                {
                    "agent_exited": agent is None or agent.poll() is not None,
                    "gateway_exited": gateway is None or gateway.poll() is not None,
                    "credential_config_removed": not config_path.exists(),
                    "sanitized_artifacts": sanitized,
                }
            )
        )


@pytest.mark.asyncio
async def test_native_two_attempts_continue_complete(tmp_path):
    _require_remote_goal()
    async with web_services(tmp_path, "native", "web") as (url, data, profile):
        async with channel_connection(url, "web") as ws:
            c = _Channel(ws, tmp_path)
            sid = await _create(c, profile)
            artifact = data / "agent" / "workspace" / "native-two-attempts.txt"
            marker = "R1-09B-NATIVE-TWO-ATTEMPTS"
            objective = (
                "This Goal requires TWO separate Goal attempts. First call get_current_goal and inspect attempt_count. "
                "When attempt_count is 1, do NOT create the artifact, do NOT claim completion, and do NOT ask the user. "
                'Instead submit_goal_report status=continue, evidence="first attempt intentionally checkpoints before artifact creation", '
                'remaining_work="create and verify the artifact in the next automatic Goal attempt", '
                'next_instruction="This is now the second attempt; create and verify the requested artifact and report complete". '
                "Then end your response immediately. Never perform second-attempt work in that first attempt. "
                "When attempt_count is at least 2, complete the following work: "
                + _objective(artifact, marker)
            )
            await c.goal(sid, "set", objective=objective, max_attempts=3)
            goal = await c.wait_goal(sid, {"completed", "blocked", "paused"})
            assert goal["attempt_count"] == 2, goal
            accepted_continue = [
                f
                for f in c.frames
                if f.get("event") == "chat.tool_result"
                and (f.get("payload", {}).get("raw_output") or {}).get("result")
                == "report_accepted"
                and f["payload"]["raw_output"].get("status") == "continue"
            ]
            assert accepted_continue, (
                "first attempt did not submit accepted continue report"
            )
            result = await _verify_completed(
                c, tmp_path, data, sid, goal, objective, artifact, marker
            )
            _save(
                tmp_path,
                c,
                {
                    "provider": "native",
                    "channel": "web",
                    "session_id": sid,
                    "two_attempts": True,
                    "accepted_continue_tool_ids": [
                        f["payload"]["tool_call_id"] for f in accepted_continue
                    ],
                    **result,
                },
            )


@pytest.mark.asyncio
async def test_native_attempt_budget_stops(tmp_path):
    _require_remote_goal()
    async with web_services(tmp_path, "native", "web") as (url, data, profile):
        async with channel_connection(url, "web") as ws:
            c = _Channel(ws, tmp_path)
            sid = await _create(c, profile)
            objective = (
                "The required approval code has not been supplied, so this goal cannot complete. Call get_current_goal, "
                'then submit_goal_report status=continue evidence="required approval code absent" remaining_work="obtain approval code" '
                'next_instruction="wait for approval code". Do not invent an approval code, ask a question, or call any other tool. '
                "End the attempt saying the required code is absent."
            )
            await c.goal(sid, "set", objective=objective, max_attempts=1)
            goal = await c.wait_goal(sid, {"completed", "blocked", "paused"})
            assert goal["status"] == "blocked" and goal["attempt_count"] == 1, goal
            assert "max_attempts_exhausted" in goal["last_assessment"]["evidence"], goal
            await _clear_goal(c, sid, goal["goal_id"])
            _save(
                tmp_path,
                c,
                {
                    "provider": "native",
                    "channel": "web",
                    "session_id": sid,
                    "attempt_budget": True,
                    "goal": goal,
                    "cleared": True,
                },
            )


@pytest.mark.asyncio
@pytest.mark.parametrize("permission", [False, True], ids=["ask_user", "permission"])
async def test_native_wait_answer_resumes_same_attempt(tmp_path, permission):
    _require_remote_goal()
    services = (
        permission_services(tmp_path)
        if permission
        else web_services(tmp_path, "native", "web")
    )
    async with services as (url, data, profile):
        async with channel_connection(url, "web") as ws:
            c = _Channel(ws, tmp_path)
            sid = await _create(c, profile)
            artifact = (
                data
                / "agent"
                / "workspace"
                / ("native-permission.txt" if permission else "native-answer.txt")
            )
            marker = (
                "R1-09B-NATIVE-PERMISSION" if permission else "R1-09B-NATIVE-ANSWER"
            )
            work = _objective(artifact, marker).replace(
                "Do not delegate or ask the user questions.", "Do not delegate."
            )
            if permission:
                objective = (
                    work
                    + " Immediately after the shell returns, call get_current_goal again before submit_goal_report."
                )
            else:
                objective = (
                    'First call get_current_goal, then call ask_user with query="R1-09B provide approval word" '
                    'and questions=[{"question":"R1-09B provide approval word"}]. Do not write the artifact before the user answers. '
                    "After the user answers APPROVED, call get_current_goal again and complete this work: "
                    + work
                )
            await c.goal(sid, "set", objective=objective, max_attempts=3)
            event = await c.until(
                lambda f: (
                    f.get("event") == "chat.ask_user_question"
                    and f.get("payload", {}).get("session_id") == sid
                ),
                timeout=180,
            )
            question = event["payload"]
            expected = "permission_interrupt" if permission else "ask_user_interrupt"
            assert question.get("source") == expected, question
            before = (await c.goal(sid, "get"))["goal"]
            assert before["status"] == "active" and before["attempt_count"] >= 1, before
            assert not artifact.exists(), "tool ran before user approval/answer"
            await asyncio.sleep(2)
            waiting = (await c.goal(sid, "get"))["goal"]
            assert (
                waiting["attempt_count"] == before["attempt_count"]
                and not artifact.exists()
            )
            qs = question.get("questions") or []
            assert qs, question
            answer = {
                "question": qs[0].get("question"),
                "selected_options": ["allow_once"] if permission else [],
                "custom_input": "" if permission else "APPROVED",
            }
            if qs[0].get("card_id"):
                answer["card_id"] = qs[0]["card_id"]
            params = {
                "session_id": sid,
                "mode": "agent.code",
                "query": "",
                "request_id": question["request_id"],
                "source": expected,
                "answers": [answer],
            }
            for key in ("session_generation", "approval_schema"):
                if key in question:
                    params[key] = question[key]
            await c.send("chat.send", params, stream=True)
            goal = await c.wait_goal(sid, {"completed", "blocked", "paused"})
            assert (
                goal["goal_id"] == before["goal_id"]
                and goal["attempt_count"] == before["attempt_count"]
            ), goal
            result = await _verify_completed(
                c, tmp_path, data, sid, goal, objective, artifact, marker
            )
            _save(
                tmp_path,
                c,
                {
                    "provider": "native",
                    "channel": "web",
                    "session_id": sid,
                    "interaction_source": expected,
                    "interaction_request_id": question["request_id"],
                    "same_attempt": True,
                    **result,
                },
            )
