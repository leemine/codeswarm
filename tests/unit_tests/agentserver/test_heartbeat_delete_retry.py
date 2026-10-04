"""Same durable deletion operation retries actual Heartbeat preparation."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from jiuwenswarm.agents.harness.code.rails.heartbeat import runtime as module
from jiuwenswarm.agents.harness.code.rails.heartbeat.runtime import HeartbeatRailRuntime
from jiuwenswarm.server.runtime.session import lifecycle as lc
from tests.unit_tests.runtime import (
    test_runtime_session_provisioner as provisioner_tests,
)

env = provisioner_tests.env


@pytest.fixture
def heartbeat(env, monkeypatch, tmp_path):
    monkeypatch.setattr(module, "get_config", lambda: {})
    monkeypatch.setattr(
        module, "get_heartbeat_jobs_path", lambda: tmp_path / "heartbeat.sqlite"
    )
    server = SimpleNamespace(
        get_agent_manager=lambda: SimpleNamespace(unpin_agent=Mock())
    )
    runtime = HeartbeatRailRuntime(server)
    runtime._available = True
    runtime.scheduler.on_session_deleted = AsyncMock()
    runtime.store.count_active_jobs_for_session = AsyncMock(return_value=0)
    runtime.scheduler.resume_session = Mock(wraps=runtime.scheduler.resume_session)
    runtime.admission.block_heartbeats = AsyncMock(
        wraps=runtime.admission.block_heartbeats
    )
    env.runtime._session_provisioner._delete_lifecycle = runtime
    return runtime


@pytest.mark.asyncio
async def test_actual_provisioner_trajectory_commit_failure_retries_same_prepared_operation(
    env, heartbeat, monkeypatch
):
    sid = "owned-delete"
    path = env.root / sid
    path.mkdir()
    operation = lc.begin("session", sid, "delete")
    commits = []

    def commit(_):
        commits.append(sid)
        if len(commits) == 1:
            raise OSError("synthetic trajectory commit outage")

    monkeypatch.setattr(
        "jiuwenswarm.observability.session_delete.commit_trajectory_session_delete",
        commit,
    )
    first = await env.runtime.delete_session(channel_id="web", session_id=sid)
    assert (
        not first.ok and first.deleted and first.error_code == "DELETE_COMMIT_PENDING"
    )
    assert not path.exists() and sid in heartbeat._deleting_sessions
    assert sid in heartbeat.scheduler._suspended_sessions
    assert heartbeat.admission._states[sid].heartbeat_blocked
    heartbeat.scheduler.resume_session.assert_not_called()
    second = await env.runtime.delete_session(channel_id="web", session_id=sid)
    assert second.ok and second.deleted
    assert (
        lc.state("session", sid)["operation"]["operation_id"]
        == operation["operation_id"]
    )
    assert heartbeat.admission.block_heartbeats.await_count == 1
    heartbeat.scheduler.resume_session.assert_called_once_with(sid)
    assert sid not in heartbeat._deleting_sessions
    assert env.events.count("runner.release") == 1


@pytest.mark.asyncio
async def test_same_operation_ready_reuses_actual_quiesced_dependencies(env, heartbeat):
    lc.begin("session", "s1", "delete")
    await heartbeat.begin_session_delete("s1")
    await heartbeat.begin_session_delete("s1")
    assert heartbeat.admission.block_heartbeats.await_count == 1
    heartbeat.scheduler.resume_session.assert_not_called()
    await heartbeat.abort_session_delete("s1")
    assert "s1" not in heartbeat._deleting_sessions


@pytest.mark.asyncio
async def test_concurrent_preparation_is_not_ready_retry(env, heartbeat, monkeypatch):
    lc.begin("session", "s1", "delete")
    entered, release = asyncio.Event(), asyncio.Event()
    original = heartbeat.admission.block_heartbeats

    async def blocking(sid):
        entered.set()
        await release.wait()
        return await original(sid)

    monkeypatch.setattr(heartbeat.admission, "block_heartbeats", blocking)
    first = asyncio.create_task(heartbeat.begin_session_delete("s1"))
    await entered.wait()
    try:
        with pytest.raises(RuntimeError, match="already in progress"):
            await heartbeat.begin_session_delete("s1")
    finally:
        release.set()
        await first
    await heartbeat.abort_session_delete("s1")


@pytest.mark.asyncio
async def test_different_operation_and_legacy_duplicate_remain_rejected(env, heartbeat):
    await heartbeat.begin_session_delete("legacy")
    with pytest.raises(RuntimeError, match="already in progress"):
        await heartbeat.begin_session_delete("legacy")
    await heartbeat.abort_session_delete("legacy")
    lc.begin("session", "s1", "delete")
    await heartbeat.begin_session_delete("s1")
    with lc.resource_lock("session", "s1"):
        state = lc.state("session", "s1")
        state["operation"]["operation_id"] = "another-real-operation"
        lc.save_locked("session", "s1", state)
    with pytest.raises(RuntimeError, match="already in progress"):
        await heartbeat.begin_session_delete("s1")
    await heartbeat.abort_session_delete("s1")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changed",
    [
        "scheduler",
        "admission",
        "execution",
        "unsuspended",
        "unblocked",
        "active",
        "pinned",
    ],
)
async def test_ready_boolean_does_not_replace_actual_quiescence(
    env, heartbeat, monkeypatch, changed
):
    lc.begin("session", "s1", "delete")
    await heartbeat.begin_session_delete("s1")
    if changed in {"scheduler", "admission", "execution"}:
        monkeypatch.setattr(heartbeat, changed, object())
    elif changed == "unsuspended":
        heartbeat.scheduler.resume_session("s1")
    elif changed == "unblocked":
        await heartbeat.admission.unblock_heartbeats("s1")
    elif changed == "active":
        monkeypatch.setattr(heartbeat.execution, "active_session_ids", lambda: {"s1"})
    else:
        heartbeat._pinned_agents["s1"] = object()
    with pytest.raises(RuntimeError, match="already in progress"):
        await heartbeat.begin_session_delete("s1")


@pytest.mark.asyncio
@pytest.mark.parametrize("cancelled", [False, True])
async def test_failed_or_cancelled_preparation_clears_only_its_fence(
    env, heartbeat, monkeypatch, cancelled
):
    lc.begin("session", "s1", "delete")
    entered = asyncio.Event()
    original = heartbeat.admission.block_heartbeats

    async def fail_after_block(sid):
        await original(sid)
        entered.set()
        if cancelled:
            await asyncio.Event().wait()
        raise OSError("synthetic preparation outage")

    monkeypatch.setattr(heartbeat.admission, "block_heartbeats", fail_after_block)
    task = asyncio.create_task(heartbeat.begin_session_delete("s1"))
    await entered.wait()
    if cancelled:
        task.cancel()
    with pytest.raises(asyncio.CancelledError if cancelled else OSError):
        await task
    assert "s1" not in heartbeat._deleting_sessions
    assert "s1" not in heartbeat.scheduler._suspended_sessions
    assert not heartbeat.admission._states.get("s1")
    monkeypatch.setattr(heartbeat.admission, "block_heartbeats", original)
    await heartbeat.begin_session_delete("s1")
    await heartbeat.abort_session_delete("s1")


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["abort", "commit"])
async def test_finishing_preparation_cannot_be_reused(
    env, heartbeat, monkeypatch, method
):
    lc.begin("session", "s1", "delete")
    await heartbeat.begin_session_delete("s1")
    entered, release = asyncio.Event(), asyncio.Event()

    async def pause(_):
        entered.set()
        await release.wait()

    if method == "abort":
        monkeypatch.setattr(heartbeat.admission, "unblock_heartbeats", pause)
    else:
        monkeypatch.setattr(heartbeat.scheduler, "on_session_deleted", pause)
    task = asyncio.create_task(getattr(heartbeat, f"{method}_session_delete")("s1"))
    await entered.wait()
    try:
        with pytest.raises(RuntimeError, match="already in progress"):
            await heartbeat.begin_session_delete("s1")
    finally:
        release.set()
        await task
    assert "s1" not in heartbeat._deleting_sessions


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["abort", "commit"])
async def test_late_cleanup_does_not_clear_a_different_handle_with_equal_fields(
    env, heartbeat, monkeypatch, method
):
    from dataclasses import replace

    lc.begin("session", "s1", "delete")
    await heartbeat.begin_session_delete("s1")
    original = heartbeat._deleting_sessions["s1"]
    entered, release = asyncio.Event(), asyncio.Event()

    async def pause(_):
        entered.set()
        await release.wait()

    if method == "abort":
        monkeypatch.setattr(heartbeat.store, "count_active_jobs_for_session", pause)
    else:
        monkeypatch.setattr(heartbeat.scheduler, "on_session_deleted", pause)
    task = asyncio.create_task(getattr(heartbeat, f"{method}_session_delete")("s1"))
    await entered.wait()
    # Fault injection of a replacement owner during the suspended old cleanup:
    # value equality must never authorize removing this distinct handle.
    replacement = replace(original)
    heartbeat._deleting_sessions["s1"] = replacement
    release.set()
    await task
    assert heartbeat._deleting_sessions["s1"] is replacement


@pytest.mark.asyncio
async def test_commit_failure_clears_original_handle_without_hiding_error(
    env, heartbeat
):
    lc.begin("session", "s1", "delete")
    await heartbeat.begin_session_delete("s1")
    heartbeat.scheduler.on_session_deleted.side_effect = OSError(
        "synthetic job policy outage"
    )
    with pytest.raises(OSError, match="job policy outage"):
        await heartbeat.commit_session_delete("s1")
    assert "s1" not in heartbeat._deleting_sessions
    assert "s1" not in heartbeat.scheduler._suspended_sessions
    assert not heartbeat.admission._states.get("s1")
