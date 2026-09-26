# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Opt-in real-model Goal qualification through existing product channels.

RUN_GOAL_REMOTE=1 opts in. Reuses the isolated R1-08 service fixture and its
HEARTBEAT_REMOTE_API_BASE/API_KEY/MODEL configuration inputs. WebSocket and
Gateway CLI are protocol coverage, not browser UI or the one-shot Process CLI.
No core/product monkeypatch or synthetic model response is used.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import time
from pathlib import Path

import pytest

from .test_heartbeat_channels_remote import channel_connection, web_services

pytestmark = [
    pytest.mark.integration,
    pytest.mark.system,
    pytest.mark.skipif(
        os.environ.get("RUN_GOAL_REMOTE") != "1",
        reason="real Goal qualification is opt-in",
    ),
]


class _Channel:
    """Single socket consumer retaining the real response/event transcript."""

    def __init__(self, ws, scope):
        self.ws = ws
        self.trace_path = scope / "frames.jsonl"
        self.frames = []
        self.sequence = 0

    async def send(self, method, params, *, stream=False):
        self.sequence += 1
        request_id = f"goal-remote-{self.sequence}"
        request = {"type": "req", "id": request_id, "method": method, "params": params}
        if stream:
            request["is_stream"] = True
        await self.ws.send(json.dumps(request))
        return request_id

    async def until(self, predicate, *, timeout=45):
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            assert remaining > 0, "timed out waiting for product channel response"
            frame = json.loads(await asyncio.wait_for(self.ws.recv(), remaining))
            self.frames.append(frame)
            with self.trace_path.open("a", encoding="utf-8") as trace:
                trace.write(json.dumps(frame, ensure_ascii=False) + "\n")
            if predicate(frame):
                return frame

    async def rpc(self, method, params):
        request_id = await self.send(method, params)
        response = await self.until(
            lambda frame: frame.get("type") == "res" and frame.get("id") == request_id
        )
        assert response.get("ok") is True, response
        return response.get("payload") or {}

    async def goal(self, sid, action, **params):
        body = {"session_id": sid, "mode": "agent.code", "action": action, **params}
        if action in {"set", "resume"}:
            return await self.send("command.goal", body, stream=True)
        return await self.rpc("command.goal", body)

    async def wait_goal(self, sid, statuses, *, timeout=240):
        deadline = asyncio.get_running_loop().time() + timeout
        last = None
        while asyncio.get_running_loop().time() < deadline:
            response = await self.goal(sid, "get")
            last = response.get("goal")
            if isinstance(last, dict) and last.get("status") in statuses:
                return last
            assert response.get("result_type") != "goal_error", response
            errors = [
                f
                for f in self.frames
                if f.get("event") in {"goal.error", "chat.error", "execution.error"}
            ]
            assert not errors, errors[-3:]
            await asyncio.sleep(1)
        raise AssertionError(f"Goal did not reach {statuses}: {last}")


async def _create(channel, profile):
    params = {"persist_session": True, "mode": "agent.code", "work_mode": "code"}
    if profile:
        params["execution_profile_id"] = profile
    return (await channel.rpc("session.create", params))["session_id"]


def _objective(artifact, marker, *, slow=False):
    started = artifact.with_suffix(".started")
    setup = (
        f"if [ ! -f {shlex.quote(str(started))} ]; then printf started > {shlex.quote(str(started))}; sleep 25; fi; "
        if slow
        else ""
    )
    command = (
        setup
        + f"printf '%s' {shlex.quote(marker)} > {shlex.quote(str(artifact))}; cat {shlex.quote(str(artifact))}"
    )
    return (
        f"Create the artifact {artifact} containing exactly {marker}, then verify its contents. "
        "First call get_current_goal. Use the shell tool to execute this exact command: "
        f"{command}\n"
        "After successful verification call submit_goal_report with status complete and evidence "
        "citing the verified path and content. Use any current goal identity/token supplied in the "
        "system instructions; never invent an identity. Do not delegate or ask the user questions. "
        f"Your final response must contain {marker}."
    )


def _history(data, sid):
    path = data / "agent" / "sessions" / sid / "history.jsonl"
    return (
        [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
        if path.exists()
        else []
    )


async def _verify_completed(
    channel, scope, data, sid, goal, objective, artifact, marker
):
    assert goal["status"] == "completed", goal
    assert goal["last_assessment"]["status"] == "complete", goal
    assert goal["attempt_count"] >= 1
    assert artifact.read_text() == marker
    goal_id = goal["goal_id"]
    deadline = asyncio.get_running_loop().time() + 30
    while True:
        records = _history(data, sid)
        objectives = [
            row
            for row in records
            if row.get("is_goal_objective_message") and row.get("goal_id") == goal_id
        ]
        cards = [
            row
            for row in records
            if row.get("is_goal_completed_message") and row.get("goal_id") == goal_id
        ]
        if objectives and cards:
            break
        assert asyncio.get_running_loop().time() < deadline, (
            "durable Goal history was not written"
        )
        await asyncio.sleep(0.2)
    assert len(objectives) == len(cards) == 1, {
        "objectives": objectives,
        "cards": cards,
    }
    assert objectives[0]["content"] == objective
    assert cards[0]["id"] == f"goal-completed-{goal_id}"
    # A real tool report must have reached the core sink; agent prose alone is
    # insufficient, even if the evaluator could infer completion from output.
    log = (scope / "agentserver.log").read_text(errors="replace")
    assert re.search(
        r"\[GoalReportSink\] Report submitted:.*goal_id=" + re.escape(goal_id), log
    )
    tool_frames = [
        frame
        for frame in channel.frames
        if str(frame.get("event", "")).startswith("chat.tool_")
    ]
    assert "get_current_goal" in json.dumps(tool_frames), (
        "Goal read tool was not observed on the product stream"
    )
    start = len(channel.frames)
    await channel.send("history.get", {"session_id": sid, "cursor": None, "limit": 100})
    await channel.until(
        lambda frame: (
            frame.get("event") == "history.message"
            and frame.get("payload", {}).get("status") == "done"
        )
    )
    readback = json.dumps(channel.frames[start:], ensure_ascii=False)
    assert (
        goal_id in readback
        and "is_goal_completed_message" in readback
        and "is_goal_objective_message" in readback
    )
    # Read-only refresh must not append another objective or completion card.
    refreshed = await channel.goal(sid, "get")
    assert refreshed["goal"]["goal_id"] == goal_id
    after = _history(data, sid)
    assert (
        sum(
            bool(row.get("is_goal_completed_message")) and row.get("goal_id") == goal_id
            for row in after
        )
        == 1
    )
    cleared = await channel.goal(sid, "clear")
    assert cleared.get("goal") is None and cleared["cleared_goal"]["goal_id"] == goal_id
    assert (await channel.goal(sid, "get")).get("goal") is None
    return {
        "goal_id": goal_id,
        "attempt_count": goal["attempt_count"],
        "status": goal["status"],
        "last_assessment": goal["last_assessment"],
        "token_usage": goal.get("token_usage"),
        "objective_history_count": len(objectives),
        "completion_card_count": len(cards),
        "history_channel_readback": True,
        "cleared": True,
        "real_goal_report": True,
    }


def _save(scope, channel, result):
    (scope / "frames.json").write_text(
        json.dumps(channel.frames, ensure_ascii=False, indent=2)
    )
    (scope / "result.json").write_text(
        json.dumps(
            {"browser_ui": False, "process_cli": False, **result},
            ensure_ascii=False,
            indent=2,
        )
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["native", "codex"])
@pytest.mark.parametrize("channel_name", ["web", "gateway_cli"])
async def test_remote_goal_completed_history_clear(
    tmp_path: Path, provider, channel_name
):
    async with web_services(tmp_path, provider, channel_name) as (url, data, profile):
        async with channel_connection(url, channel_name) as ws:
            channel = _Channel(ws, tmp_path)
            try:
                sid = await _create(channel, profile)
                artifact = data / "agent" / "workspace" / "goal-artifact.txt"
                marker = f"R1-09B-{provider.upper()}-{channel_name.upper()}-DONE"
                objective = _objective(artifact, marker)
                await channel.goal(
                    sid,
                    "set",
                    objective=objective,
                    max_attempts=3,
                    model_name=os.environ.get("HEARTBEAT_REMOTE_MODEL", "glm-5.2"),
                )
                goal = await channel.wait_goal(sid, {"completed", "blocked", "paused"})
                result = await _verify_completed(
                    channel, tmp_path, data, sid, goal, objective, artifact, marker
                )
                _save(
                    tmp_path,
                    channel,
                    {
                        "provider": provider,
                        "channel": channel_name,
                        "session_id": sid,
                        **result,
                    },
                )
            finally:
                (tmp_path / "frames.json").write_text(
                    json.dumps(channel.frames, ensure_ascii=False, indent=2)
                )


async def _wait_started(channel, sid, started, *, timeout=150):
    deadline = asyncio.get_running_loop().time() + timeout
    while not started.exists():
        assert asyncio.get_running_loop().time() < deadline, (
            "real shell did not create start marker"
        )
        goal = (await channel.goal(sid, "get")).get("goal")
        assert goal is None or goal.get("status") == "active", goal
        await asyncio.sleep(0.5)


@pytest.mark.asyncio
async def test_remote_codex_goal_pause_resume(tmp_path: Path):
    async with web_services(tmp_path, "codex", "web") as (url, data, profile):
        async with channel_connection(url, "web") as ws:
            channel = _Channel(ws, tmp_path)
            sid = await _create(channel, profile)
            artifact = data / "agent" / "workspace" / "pause-artifact.txt"
            marker = "R1-09B-PAUSE-RESUME-DONE"
            objective = _objective(artifact, marker, slow=True)
            await channel.goal(sid, "set", objective=objective, max_attempts=3)
            await _wait_started(channel, sid, artifact.with_suffix(".started"))
            paused = await channel.goal(sid, "pause")
            assert paused["goal"]["status"] == "paused", paused
            assert not artifact.exists(), (
                "pause arrived after the real shell had already finished"
            )
            await channel.goal(sid, "resume")
            goal = await channel.wait_goal(sid, {"completed", "blocked", "paused"})
            assert goal["goal_id"] == paused["goal"]["goal_id"]
            result = await _verify_completed(
                channel, tmp_path, data, sid, goal, objective, artifact, marker
            )
            _save(
                tmp_path,
                channel,
                {
                    "provider": "codex",
                    "channel": "web",
                    "session_id": sid,
                    "pause_resume": True,
                    **result,
                },
            )


@pytest.mark.asyncio
async def test_remote_codex_goal_attempt_budget(tmp_path: Path):
    async with web_services(tmp_path, "codex", "web") as (url, _data, profile):
        async with channel_connection(url, "web") as ws:
            channel = _Channel(ws, tmp_path)
            sid = await _create(channel, profile)
            objective = (
                "The objective is to verify a user approval code that has not been supplied. "
                "Do not invent the code or claim completion. For this attempt call get_current_goal, "
                "then submit_goal_report status=continue, evidence='required approval code is absent', "
                "remaining_work='obtain approval code', next_instruction='wait for approval code'. "
                "Use the report identity supplied by the system. Do not ask questions or call other tools. "
                "End this attempt stating that the approval code is absent."
            )
            await channel.goal(sid, "set", objective=objective, max_attempts=1)
            goal = await channel.wait_goal(sid, {"completed", "blocked", "paused"})
            assert goal["status"] == "blocked", goal
            assert goal["attempt_count"] == 1
            assert "max_attempts_exhausted" in goal["last_assessment"]["evidence"], goal
            await channel.goal(sid, "clear")
            _save(
                tmp_path,
                channel,
                {
                    "provider": "codex",
                    "channel": "web",
                    "session_id": sid,
                    "attempt_budget": True,
                    "goal": goal,
                    "cleared": True,
                },
            )


@pytest.mark.asyncio
async def test_remote_codex_goal_defers_due_heartbeat(tmp_path: Path):
    async with web_services(tmp_path, "codex", "web") as (url, data, profile):
        async with channel_connection(url, "web") as ws:
            channel = _Channel(ws, tmp_path)
            sid = await _create(channel, profile)
            artifact = data / "agent" / "workspace" / "heartbeat-artifact.txt"
            marker = "R1-09B-GOAL-BEFORE-HEARTBEAT"
            objective = _objective(artifact, marker, slow=True)
            await channel.goal(sid, "set", objective=objective, max_attempts=3)
            await _wait_started(channel, sid, artifact.with_suffix(".started"))
            job = (
                await channel.rpc(
                    "heartbeat.job.create",
                    {
                        "session_id": sid,
                        "name": "R1-09B-goal-followup",
                        "enabled": True,
                        "schedule": {"type": "once", "run_at": time.time() + 2},
                        "max_runs": 1,
                        "prompt": "Reply exactly R1-09B-HEARTBEAT-AFTER-GOAL. Do not call tools.",
                    },
                )
            )["job"]
            await asyncio.sleep(4)
            during = (
                await channel.rpc(
                    "heartbeat.job.get", {"session_id": sid, "id": job["id"]}
                )
            )["job"]
            assert during["run_count"] == 0, during
            assert (await channel.goal(sid, "get"))["goal"]["status"] == "active"
            goal = await channel.wait_goal(sid, {"completed", "blocked", "paused"})
            result = await _verify_completed(
                channel, tmp_path, data, sid, goal, objective, artifact, marker
            )
            deadline = asyncio.get_running_loop().time() + 180
            while True:
                final_job = (
                    await channel.rpc(
                        "heartbeat.job.get", {"session_id": sid, "id": job["id"]}
                    )
                )["job"]
                if final_job["run_count"] == 1:
                    break
                assert asyncio.get_running_loop().time() < deadline, final_job
                await asyncio.sleep(1)
            assert final_job["run_state"]["last_run_status"] == "succeeded", final_job
            await channel.rpc(
                "heartbeat.job.delete", {"session_id": sid, "id": job["id"]}
            )
            _save(
                tmp_path,
                channel,
                {
                    "provider": "codex",
                    "channel": "web",
                    "session_id": sid,
                    "heartbeat_waited_for_goal": True,
                    "heartbeat_run_count": 1,
                    **result,
                },
            )
