# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Opt-in real Goal qualification using the shared isolated product fixtures.

RUN_GOAL_REMOTE=1 requires HEARTBEAT_REMOTE_API_BASE, HEARTBEAT_REMOTE_API_KEY
and HEARTBEAT_REMOTE_MODEL. No opt-in skips each collected case; incomplete
explicit configuration fails before any service or model is started.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import os
import shlex
import signal
import time
from pathlib import Path

import pytest

from tests.system_tests.test_goal_channels_remote import (
    _Channel,
    _clear_goal,
    _create,
    _history,
)
from tests.system_tests.test_heartbeat_channels_remote import (
    channel_connection,
    web_services,
)

pytestmark = [pytest.mark.integration, pytest.mark.system]


_SLOW_TOOL_SCRIPT = """\
import json,os,subprocess,time
from pathlib import Path
root=Path(__file__).parent
child=subprocess.Popen(['sleep','120'])
def identity(pid):
 fields=(Path('/proc')/str(pid)/'stat').read_text().rsplit(')',1)[1].split()
 return {'pid':pid,'start':fields[19],'state':fields[0]}
(root/'goal-started.json').write_text(json.dumps({'at':time.time(),'parent':identity(os.getpid()),'child':identity(child.pid)}))
child.wait()
(root/'goal-finished.txt').write_text('R1-09B-GOAL-SHOULD-NOT-FINISH')
print('R1-09B-GOAL-SHOULD-NOT-FINISH')
"""

_USER_TOOL_SCRIPT = """\
import hashlib,json,time
from pathlib import Path
root=Path(__file__).parent
owned=json.loads((root/'owned-processes.json').read_text())
observations=[]
for old in owned:
 path=Path('/proc')/str(old['pid'])/'stat'
 fields=path.read_text().rsplit(')',1)[1].split() if path.exists() else None
 same=fields is not None and fields[19]==old['start']
 observations.append({'original':old,'state':fields[0] if fields else None,'ppid':int(fields[1]) if fields else None,'pgid':int(fields[2]) if fields else None,'sid':int(fields[3]) if fields else None,'start':fields[19] if fields else None,'command_sha256':hashlib.sha256((Path('/proc')/str(old['pid'])/'cmdline').read_bytes()).hexdigest() if fields else None,'same_identity':same,'live':same and fields[0] not in {'Z','X'}})
(root/'user-entry.json').write_text(json.dumps({'at':time.time(),'processes':observations}))
assert not any(row['live'] for row in observations),observations
assert not (root/'goal-finished.txt').exists()
(root/'user-done.json').write_text(json.dumps({'at':time.time(),'old_processes':observations}))
print('R1-09B-ORDINARY-USER-DONE')
"""


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


def process_identity(pid):
    path = Path("/proc", str(pid), "stat")
    try:
        fields = path.read_text().rsplit(")", 1)[1].split()
        return {
            "pid": int(pid),
            "start": fields[19],
            "state": fields[0],
            "ppid": int(fields[1]),
            "pgid": int(fields[2]),
            "sid": int(fields[3]),
            "command_sha256": hashlib.sha256(
                Path("/proc", str(pid), "cmdline").read_bytes()
            ).hexdigest(),
        }
    except (FileNotFoundError, ProcessLookupError):
        return None


def observe_old(owned):
    observations = []
    for original in owned:
        current = process_identity(original["pid"])
        same = current is not None and current["start"] == original["start"]
        observations.append(
            {
                "original": original,
                "current": current,
                "same_identity": same,
                "live": same and current["state"] not in {"Z", "X"},
            }
        )
    return observations


async def _cleanup_owned_processes(scope, owned):
    """After fixture shutdown, reap only this test's exact recorded identities."""
    signals = []
    for signal_value in (signal.SIGTERM, signal.SIGKILL):
        for row in observe_old(owned):
            if row["live"]:
                try:
                    os.kill(row["original"]["pid"], signal_value)
                    signals.append(
                        {"pid": row["original"]["pid"], "signal": signal_value}
                    )
                except ProcessLookupError:
                    pass
        deadline = time.monotonic() + 3
        while (
            any(row["live"] for row in observe_old(owned))
            and time.monotonic() < deadline
        ):
            await asyncio.sleep(0.05)
        if not any(row["live"] for row in observe_old(owned)):
            break
    remaining = observe_old(owned)
    (scope / "owned-process-cleanup.json").write_text(
        json.dumps({"signals": signals, "remaining": remaining}, indent=2)
    )
    assert not any(row["live"] for row in remaining), remaining


@pytest.mark.asyncio
async def test_real_codex_ordinary_chat_cancels_goal_and_confirms_exit(tmp_path):
    _require_remote_goal()
    if not Path("/proc/self/stat").exists():
        pytest.fail("Explicit Codex process-exit qualification requires Linux /proc")
    if importlib.util.find_spec("openai_codex") is None:
        pytest.fail(
            "Explicit Codex qualification requires the optional openai-codex SDK"
        )
    owned = []
    try:
        async with web_services(tmp_path, "codex", "web") as (url, data, profile):
            workspace = data / "agent" / "workspace"
            slow = workspace / "goal_slow.py"
            user_script = workspace / "ordinary_user.py"
            slow.write_text(_SLOW_TOOL_SCRIPT)
            user_script.write_text(_USER_TOOL_SCRIPT)
            objective = (
                f"Call get_current_goal then execute exactly python3 {shlex.quote(str(slow))} using the shell. "
                "The script deliberately takes 120 seconds. Wait for the actual result; do not fake success or edit the script. "
                "Only if goal-finished.txt contains R1-09B-GOAL-SHOULD-NOT-FINISH, submit_goal_report complete using supplied identity. "
                "Do not delegate or ask questions."
            )
            async with channel_connection(url, "web") as ws:
                channel = _Channel(ws, tmp_path)
                sid = await _create(channel, profile)
                goal_request = await channel.goal(
                    sid, "set", objective=objective, max_attempts=3
                )
                deadline = time.monotonic() + 180
                while not (workspace / "goal-started.json").exists():
                    assert time.monotonic() < deadline, "Goal slow tool did not start"
                    state = (await channel.goal(sid, "get")).get("goal")
                    assert state is None or state["status"] == "active", state
                    await asyncio.sleep(0.3)
                started = json.loads((workspace / "goal-started.json").read_text())
                owned.extend([started["parent"], started["child"]])
                for proc in Path("/proc").iterdir():
                    if not proc.name.isdigit():
                        continue
                    try:
                        command = (proc / "cmdline").read_bytes()
                        cwd = (proc / "cwd").resolve()
                    except (FileNotFoundError, PermissionError, ProcessLookupError):
                        continue
                    argv = command.split(b"\0")
                    if b"app-server" in argv and cwd.is_relative_to(tmp_path.resolve()):
                        executable = Path(argv[0].decode()).name
                        kind = (
                            "codex-app-server"
                            if executable == "codex"
                            else "codex-process-scope"
                            if any(x.endswith(b"/process_scope.py") for x in argv)
                            else None
                        )
                        identity = process_identity(proc.name)
                        if identity and kind:
                            owned.append({**identity, "kind": kind})
                assert (
                    sum(row.get("kind") == "codex-app-server" for row in owned) == 1
                ), owned
                assert (
                    sum(row.get("kind") == "codex-process-scope" for row in owned) == 1
                ), owned
                assert all(row["live"] for row in observe_old(owned)), owned
                (workspace / "owned-processes.json").write_text(json.dumps(owned))
                submitted = time.time()
                user_request = await channel.send(
                    "chat.send",
                    {
                        "session_id": sid,
                        "mode": "agent.code",
                        "query": f"New ordinary user request replaces the Goal task. Run exactly python3 {shlex.quote(str(user_script))} using the shell, then reply R1-09B-ORDINARY-USER-DONE. Do not continue or resume the Goal, call Goal tools, edit scripts, or fake success. If the script fails report the exact failure and stop.",
                    },
                    stream=True,
                )
                accepted = await channel.until(
                    lambda f: (
                        f.get("event")
                        in {"runtime.accepted", "chat.error", "execution.error"}
                        and f.get("payload", {}).get("request_id") == user_request
                    ),
                    timeout=90,
                )
                at_accepted = observe_old(owned)
                observations = [
                    {
                        "at": time.time(),
                        "stage": "new_provider_accepted",
                        "processes": at_accepted,
                        "accepted": accepted,
                    }
                ]
                if any(row["live"] for row in at_accepted):
                    for _ in range(6):
                        await asyncio.sleep(0.5)
                        observations.append(
                            {
                                "at": time.time(),
                                "stage": "post_acceptance",
                                "processes": observe_old(owned),
                            }
                        )
                (tmp_path / "exit-observations.json").write_text(
                    json.dumps(
                        {
                            "submitted_at": submitted,
                            "goal_started": started,
                            "observations": observations,
                        },
                        indent=2,
                    )
                )
                assert accepted["event"] == "runtime.accepted", accepted
                assert not any(row["live"] for row in at_accepted), at_accepted
                final = await channel.until(
                    lambda f: (
                        f.get("event")
                        in {"chat.final", "chat.error", "execution.error"}
                        and f.get("payload", {}).get("request_id") == user_request
                    ),
                    timeout=240,
                )
                assert final["event"] == "chat.final", final
                user_entry = json.loads((workspace / "user-entry.json").read_text())
                (tmp_path / "user-entry-observations.json").write_text(
                    json.dumps(user_entry, indent=2)
                )
                assert not any(row["live"] for row in user_entry["processes"]), (
                    user_entry
                )
                goal = (await channel.goal(sid, "get"))["goal"]
                (tmp_path / "goal-after-interrupt.json").write_text(
                    json.dumps(goal, indent=2)
                )
                assert (
                    goal["status"] == "paused"
                    and goal["attempt_count"] == 1
                    and goal["last_assessed_attempt"] == 0
                ), goal
                resume_id = await channel.goal(sid, "resume")
                rejected = await channel.until(
                    lambda f: (
                        f.get("event") in {"chat.error", "goal.snapshot"}
                        and f.get("payload", {}).get("request_id") == resume_id
                    ),
                    timeout=45,
                )
                (tmp_path / "resume-result.json").write_text(
                    json.dumps(rejected, indent=2)
                )
                if rejected["event"] == "chat.error":
                    assert (
                        rejected["payload"].get("code") == "goal_usage_unavailable"
                    ), rejected
                    after = (await channel.goal(sid, "get"))["goal"]
                    assert after == goal, (goal, after)
                    recovery = "rejected_unknown_usage"
                else:
                    assert (
                        goal["token_usage"]["input_tokens"] > 0
                        and goal["token_usage"]["output_tokens"] > 0
                    ), goal
                    resumed = rejected["payload"]["goal"]
                    assert (
                        resumed["goal_id"] == goal["goal_id"]
                        and resumed["status"] == "active"
                    ), rejected
                    recovery = "explicit_resume_known_usage"
                await _clear_goal(channel, sid, goal["goal_id"])
                await asyncio.sleep(3)
                assert not (workspace / "goal-finished.txt").exists()
                records = _history(data, sid)
                objectives = [
                    r
                    for r in records
                    if r.get("is_goal_objective_message")
                    and r.get("goal_id") == goal["goal_id"]
                ]
                cards = [
                    r
                    for r in records
                    if r.get("is_goal_completed_message")
                    and r.get("goal_id") == goal["goal_id"]
                ]
                segments = [
                    r
                    for r in records
                    if r.get("event_type") == "chat.final"
                    and r.get("request_id") == user_request
                ]
                finals = [
                    r
                    for r in segments
                    if "R1-09B-ORDINARY-USER-DONE" in r.get("content", "")
                ]
                delivery_ids = [
                    r["delivery_id"] for r in records if r.get("delivery_id")
                ]
                assert len(delivery_ids) == len(set(delivery_ids)), (
                    "duplicate durable delivery"
                )
                assert len(objectives) == 1 and not cards, (objectives, cards)
                assert len(finals) == 1, finals
                user = {"observed_entry": user_entry}
                (tmp_path / "end-observations.json").write_text(
                    json.dumps(
                        {
                            "owned": observe_old(owned),
                            "goal": goal,
                            "resume": rejected,
                            "objective_count": len(objectives),
                            "completion_cards": len(cards),
                        },
                        indent=2,
                    )
                )
                assert not any(row["live"] for row in at_accepted), at_accepted
                assert not any(row["live"] for row in user_entry["processes"]), (
                    user_entry
                )
                result = {
                    "behavior": "Gateway cancel-then-start for ordinary chat; confirmed old resources no longer live; Goal paused without assessment and explicit recovery follows observed accounting",
                    "recovery": recovery,
                    "session_id": sid,
                    "goal_request": goal_request,
                    "user_request": user_request,
                    "goal_started": started,
                    "user_submitted_at": submitted,
                    "owned_old_processes": owned,
                    "at_user_provider_accepted": at_accepted,
                    "user_tool_entry": user,
                    "goal": goal,
                    "resume_rejection": rejected,
                    "objective_count": 1,
                    "completion_card_count": 0,
                    "user_result_final_count": 1,
                    "user_history_segment_count": len(segments),
                    "duplicate_delivery_count": 0,
                    "old_processes_final": observe_old(owned),
                }
                (tmp_path / "result.json").write_text(json.dumps(result, indent=2))
    finally:
        await _cleanup_owned_processes(tmp_path, owned)
