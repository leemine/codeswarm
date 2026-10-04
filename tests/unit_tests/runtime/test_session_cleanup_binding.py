"""Actual cached resource selection is pinned to persisted owner metadata."""

import asyncio
from unittest.mock import AsyncMock, Mock

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.runtime.agent_manager import AgentManager
from jiuwenswarm.governance.session_sharing import SessionSharingDenied
from jiuwenswarm.server.runtime.session import lifecycle as lc
from tests.unit_tests.runtime import test_continuation_transaction as transactions

setup = transactions.setup
transaction = transactions.transaction


class CachedOwner:
    def __init__(self, sid):
        self.sessions = {sid}
        self.stopped = []
        self.released = []
        self.cleaned = []

    def has_session_runtime(self, sid):
        return sid in self.sessions

    async def stop_existing_session_runtime(self, sid):
        self.stopped.append(sid)
        self.sessions.remove(sid)

    async def release_subagent_runtime_for_session(self, sid, *, reason):
        self.released.append((sid, reason))

    async def cancel_inflight_work(self, *args, **kwargs):
        pass

    async def cleanup(self):
        pass

    async def cleanup_session_runtime(self, sid):
        self.cleaned.append(sid)
        return True


async def request_with_caches(tx):
    result = await transactions.create(tx)
    sid = result.session_id
    tx.runtime.begin_detached_native_turn(sid, "original-native", "original-request")
    manager = AgentManager()
    bound, wire, unrelated = (
        CachedOwner(sid),
        CachedOwner(sid),
        CachedOwner("other-session"),
    )
    manager.agents = {
        "web": {"unrelated-first": unrelated, "bound": bound},
        "remote": {"wire": wire},
    }
    tx.runtime._agent_manager = manager
    tx.runtime._forget_agent_execution_owner = AsyncMock()
    tx.runtime._clear_pending_interaction = AsyncMock()
    request = AgentRequest(
        request_id="cancel",
        session_id=sid,
        channel_id="remote",
        req_method=ReqMethod.CHAT_CANCEL,
        params={"session_id": sid, "intent": "cancel"},
    )
    return request, manager, bound, wire, unrelated


@pytest.mark.asyncio
async def test_cancel_stops_persisted_channel_owner_and_returns_original_wire_channel(
    transaction,
):
    tx = transaction
    request, manager, bound, wire, unrelated = await request_with_caches(tx)
    tx.setup.host.store.revoke(
        tx.request.share_id, transactions.ALICE, expected_revision=1
    )
    response = await tx.runtime.cancel_request(request)
    assert response.ok and response.channel_id == "remote"
    assert bound.stopped == [request.session_id]
    assert bound.released == [(request.session_id, "owner_cancel")]
    assert wire.stopped == wire.released == [] and request.session_id in wire.sessions
    assert unrelated.stopped == unrelated.released == []
    tx.runtime._forget_agent_execution_owner.assert_awaited_once_with(
        channel_id="web", session_id=request.session_id
    )


def change_metadata(tx, sid, field, value):
    metadata = lc.raw_metadata(sid)
    metadata[field] = value
    lc.atomic_json(tx.root / sid / "metadata.json", metadata)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field",
    [
        "project_id",
        "project_dir",
        "mode",
        "work_mode",
        "team_name",
        "channel_id",
        "execution_profile_id",
        "execution_config_fingerprint",
    ],
)
async def test_queued_cancel_rejects_any_original_binding_change(transaction, field):
    tx = transaction
    request, _, bound, wire, _ = await request_with_caches(tx)
    tx.runtime.prepare_session_cleanup(request)
    change_metadata(tx, request.session_id, field, "changed")
    with pytest.raises(SessionSharingDenied):
        await tx.runtime.cancel_request(request)
    assert bound.stopped == bound.released == wire.stopped == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [
        ("channel_id", None),
        ("channel_id", ""),
        ("channel_id", " web "),
        ("channel_id", {}),
        ("mode", None),
        ("mode", "team.work.normal"),
        ("team_name", "another-team"),
    ],
)
async def test_missing_or_invalid_explicit_binding_does_not_infer_default(
    transaction, field, value
):
    tx = transaction
    request, _, bound, wire, _ = await request_with_caches(tx)
    change_metadata(tx, request.session_id, field, value)
    with pytest.raises(SessionSharingDenied):
        await tx.runtime.cancel_request(request)
    assert bound.stopped == wire.stopped == []


@pytest.mark.asyncio
async def test_missing_metadata_denies_capture(transaction):
    request, _, bound, _, _ = await request_with_caches(transaction)
    (transaction.root / request.session_id / "metadata.json").unlink()
    with pytest.raises(SessionSharingDenied):
        transaction.runtime.prepare_session_cleanup(request)
    assert bound.stopped == []


@pytest.mark.asyncio
async def test_mutated_wire_channel_cannot_reuse_original_authority(transaction):
    request, _, bound, _, _ = await request_with_caches(transaction)
    transaction.runtime.prepare_session_cleanup(request)
    request.channel_id = "web"
    with pytest.raises(SessionSharingDenied):
        await transaction.runtime.cancel_request(request)
    assert bound.stopped == []


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["release", "stop", "cleanup", "forget"])
async def test_binding_change_during_each_resource_await_denies_further_cleanup(
    transaction, stage, monkeypatch
):
    tx = transaction
    request, manager, bound, _, _ = await request_with_caches(tx)
    method = {
        "release": "release_subagent_runtime_for_session",
        "stop": "stop_existing_session_runtime",
        "cleanup": "cleanup_session_runtime",
        "forget": "_forget_agent_execution_owner",
    }[stage]
    owner = tx.runtime if stage == "forget" else manager
    original = getattr(owner, method)
    entered, resume = asyncio.Event(), asyncio.Event()

    async def pause(**kwargs):
        value = await original(**kwargs)
        entered.set()
        await resume.wait()
        return value

    monkeypatch.setattr(owner, method, pause)
    reset = Mock()
    monkeypatch.setattr(tx.runtime._plan_controller, "reset_session", reset)
    task = asyncio.create_task(tx.runtime.cancel_request(request))
    await entered.wait()
    change_metadata(tx, request.session_id, "channel_id", "changed")
    resume.set()
    with pytest.raises(SessionSharingDenied):
        await task
    assert (
        (bound.stopped == [])
        if stage == "release"
        else bound.stopped == [request.session_id]
    )
    reset.assert_not_called()
    tx.runtime._clear_pending_interaction.assert_not_awaited()
    # Restore the synthetic metadata fault, then use the original fenced close
    # retry; fixture teardown must not discard a failed resource-close owner.
    change_metadata(tx, request.session_id, "channel_id", "web")
    assert (await tx.runtime.cancel_request(request)).ok


@pytest.mark.asyncio
async def test_host_projection_rejects_metadata_change_between_its_reads(
    transaction, monkeypatch
):
    tx = transaction
    request, _, _, _, _ = await request_with_caches(tx)
    stamp = tx.setup.host.cleanup_owner_stamp(request.session_id, transactions.BOB)
    assert len(stamp) == 9
    original = lc.raw_metadata
    calls = []

    def racing(sid):
        result = original(sid)
        calls.append(sid)
        if len(calls) > 2:
            result["channel_id"] = "changed"
        return result

    monkeypatch.setattr(lc, "raw_metadata", racing)
    with pytest.raises(SessionSharingDenied):
        tx.setup.host.cleanup_owner_binding(
            request.session_id, transactions.BOB, expected_stamp=stamp
        )


@pytest.mark.asyncio
async def test_subagent_release_selects_exact_existing_session_across_both_cache_dimensions():
    manager = AgentManager()
    unrelated, actual, remote = CachedOwner("other"), CachedOwner("s"), CachedOwner("s")
    manager.agents = {
        "web": {"first": unrelated, "bound": actual},
        "remote": {"same-sid": remote},
    }
    manager.get_agent_nowait = Mock(
        side_effect=AssertionError("default lookup forbidden")
    )
    manager.get_agent = AsyncMock(side_effect=AssertionError("allocation forbidden"))
    assert await manager.release_subagent_runtime_for_session(
        channel_id="web", session_id="s"
    )
    assert actual.released == [("s", "session_deleted")]
    assert unrelated.released == remote.released == []
    assert not await manager.release_subagent_runtime_for_session(
        channel_id="web", session_id="missing"
    )
    assert not await manager.release_subagent_runtime_for_session(
        channel_id="missing", session_id="s"
    )


@pytest.mark.asyncio
async def test_subagent_release_does_not_accept_replaced_cached_owner():
    manager = AgentManager()
    original, replacement = CachedOwner("s"), CachedOwner("s")
    manager.agents = {"web": {"bound": original}}
    entered, resume = asyncio.Event(), asyncio.Event()

    async def pause(sid, *, reason):
        entered.set()
        await resume.wait()

    original.release_subagent_runtime_for_session = pause
    task = asyncio.create_task(
        manager.release_subagent_runtime_for_session(channel_id="web", session_id="s")
    )
    await entered.wait()
    manager.agents["web"]["bound"] = replacement
    resume.set()
    with pytest.raises(RuntimeError, match="owner changed"):
        await task
    assert replacement.released == []


@pytest.mark.asyncio
async def test_subagent_release_checks_later_cached_owner_after_first_await():
    manager = AgentManager()
    first, second, replacement = CachedOwner("s"), CachedOwner("s"), CachedOwner("s")
    manager.agents = {"web": {"first": first, "second": second}}
    entered, resume = asyncio.Event(), asyncio.Event()

    async def pause(sid, *, reason):
        entered.set()
        await resume.wait()

    first.release_subagent_runtime_for_session = pause
    task = asyncio.create_task(
        manager.release_subagent_runtime_for_session(channel_id="web", session_id="s")
    )
    await entered.wait()
    manager.agents["web"]["second"] = replacement
    resume.set()
    with pytest.raises(RuntimeError, match="owner changed before"):
        await task
    assert second.released == replacement.released == []
