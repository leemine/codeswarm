"""Exact Native supplemental inputs; real core Round/TaskLoop with synthetic React."""

import asyncio
import copy
import pickle
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from openjiuwen.harness.schema.interaction import InputDispatchMode, SendInputRequest
from openjiuwen.harness_protocol import DeliveryMode
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
    JiuWenSwarmDeepAdapter,
)
from jiuwenswarm.server.runtime.agent_adapter.session_input import (
    SessionInputDeliveryUnknown,
)
from tests.unit_tests.runtime.harness.test_native_request_origin import (
    native_case as _native_case,
    _Admission,
    _done,
)


native_case = _native_case


@pytest.fixture
async def active(native_case):
    c = native_case
    entered, release = asyncio.Event(), asyncio.Event()
    original = c.react.invoke

    async def invoke(*args, **kwargs):
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)

    c.react.invoke = invoke
    owner = _Admission()
    with owner.scope():
        receipt = await c.native.send_request(
            SendInputRequest("root", {"query": "root"})
        )
    await asyncio.wait_for(entered.wait(), 2)
    try:
        yield SimpleNamespace(c=c, owner=owner, receipt=receipt, release=release)
    finally:
        release.set()
        await _done(owner)


def request(rid="extra"):
    return SendInputRequest(rid, {"query": "extra text"}, mode=InputDispatchMode.STEER)


def capture(a, check=lambda: None):
    return a.c.native.capture_steer_control(
        source=a.owner.source, request_id="extra", check_current=check
    )


def queued(a):
    return a.c.handler.interaction_queues.drain_steering(
        expected_origin=a.owner.bound[0]._pending._origin
    )


@pytest.mark.asyncio
async def test_exact_steer_keeps_original_source_token_and_callbacks(
    active, monkeypatch
):
    a = active
    c = a.c
    original = a.owner.bound[0]._entry
    root_token = a.owner.bound[0]._pending.content.metadata["native.host_request"]
    c.native._resource_governed = True
    for field in (
        "guarded_authority",
        "guarded_model_authority",
        "guarded_mcp_authority",
        "guarded_artifact_issuer",
    ):
        setattr(original, field, object())
    seen = []
    sender = c.agent._send_owned_steer

    async def actual(expected, incoming, *, check_current):
        seen.append(incoming.inputs["run"]["context"]["extra"]["native.host_request"])
        (temporary,) = [
            entry for entry in c.native._requests.values() if entry is not original
        ]
        assert temporary.lifecycle is None and temporary.owned is None
        for field in (
            "guarded_authority",
            "guarded_model_authority",
            "guarded_mcp_authority",
            "guarded_artifact_issuer",
        ):
            assert getattr(temporary, field) is getattr(original, field)
        assert expected is c.agent._active_interaction_round
        await sender(expected, incoming, check_current=check_current)

    monkeypatch.setattr(c.agent, "_send_owned_steer", actual)
    monkeypatch.setattr(
        c.native.io,
        "send",
        AsyncMock(side_effect=AssertionError("legacy AUTO fallback used")),
    )
    cap = capture(a)
    assert copy.deepcopy(cap) is cap
    with pytest.raises(TypeError):
        pickle.dumps(cap)
    unrelated = _Admission()
    with unrelated.scope():
        receipt = await c.native.send_request(request(), control=cap)
    assert not unrelated.bound and not unrelated.terminal
    assert (
        receipt.turn_id == a.receipt.turn_id
        and receipt.accepted_mode is DeliveryMode.STEER
    )
    assert seen == [root_token] and queued(a) == ["extra text"]
    assert c.native._requests == {root_token: original}
    assert c.native._turn_requests == {receipt.turn_id: root_token}
    assert a.owner.bound == [original.owned] and not a.owner.terminal
    with pytest.raises(PermissionError, match="replayed"):
        await c.native.send_request(request(), control=cap)
    assert queued(a) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["identity", "source", "binding", "request"])
async def test_control_scope_drift_cannot_append(active, change):
    a = active
    live = [True]

    def check():
        if not live[0]:
            raise PermissionError("input credential changed")

    cap = capture(a, check)
    if change == "identity":
        live[0] = False
    elif change == "source":
        a.owner.live = False
    elif change == "binding":
        object.__setattr__(cap, "_binding", object())
    try:
        with pytest.raises(PermissionError):
            await a.c.native.send_request(
                request("different" if change == "request" else "extra"), control=cap
            )
        assert queued(a) == []
    finally:
        a.owner.live = True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "lock_name", ["_interaction_send_lock", "_interaction_control_lock"]
)
async def test_input_revoked_while_core_lock_waits_never_appends(active, lock_name):
    a = active
    live = [True]

    def check():
        if not live[0]:
            raise PermissionError("input credential changed")

    cap = capture(a, check)
    lock = getattr(a.c.agent, lock_name)
    await lock.acquire()
    sending = asyncio.create_task(a.c.native.send_request(request(), control=cap))
    try:
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        live[0] = False
    finally:
        lock.release()
    with pytest.raises(PermissionError):
        await sending
    assert queued(a) == []


@pytest.mark.asyncio
async def test_caller_cancel_invalidates_original_inflight_control(active):
    a = active
    cap = capture(a)
    lock = a.c.agent._interaction_send_lock
    await lock.acquire()
    sending = asyncio.create_task(a.c.native.send_request(request(), control=cap))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    sending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await sending
    lock.release()
    pending = tuple(a.owner.bound[0]._pending._admissions)
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    assert queued(a) == []
    with pytest.raises(PermissionError, match="replayed"):
        await a.c.native.send_request(request(), control=cap)


@pytest.mark.asyncio
async def test_missing_core_port_refuses_without_fallback(active, monkeypatch):
    monkeypatch.setattr(active.c.agent, "_send_owned_steer", None, raising=False)
    with pytest.raises(Exception, match="exact Round steering is unavailable"):
        capture(active)
    assert queued(active) == []


@pytest.mark.asyncio
async def test_old_control_never_becomes_new_turn(active):
    a = active
    cap = capture(a)
    a.release.set()
    await _done(a.owner)
    with pytest.raises(PermissionError):
        await a.c.native.send_request(request(), control=cap)
    assert a.c.native._requests == {}
    assert [x["query"] for x in a.c.calls] == ["root"]


@pytest.mark.asyncio
async def test_adapter_passes_exact_control_and_checks_after_prepare(active):
    a = active
    live = [True]

    def check():
        if not live[0]:
            raise PermissionError("input ended")

    cap = capture(a, check)
    adapter = SimpleNamespace(
        _native_execution=a.c.native,
        _prepare_root_input_dispatch=AsyncMock(return_value="prepared"),
        _permission_inputs_for_dispatch=lambda *_: {"query": "extra text"},
        _permission_dispatch=SimpleNamespace(finalize=Mock()),
    )
    req = SimpleNamespace(
        params={"input_mode": "steer"}, request_id="extra", _native_steer_control=cap
    )
    assert await JiuWenSwarmDeepAdapter.deliver_active_session_input(adapter, req, {})
    assert queued(a) == ["extra text"]
    adapter._permission_dispatch.finalize.assert_called_once_with("prepared")
    with pytest.raises(PermissionError):
        await JiuWenSwarmDeepAdapter.deliver_active_session_input(
            adapter,
            SimpleNamespace(params={"input_mode": "steer"}, request_id="extra"),
            {},
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("guard_mode", ["drop", "twice", "change"])
async def test_permission_guard_cannot_silently_drop_duplicate_or_change_input(
    active, guard_mode
):
    a = active
    cap = capture(a)

    async def guard(incoming, *, send):
        if guard_mode == "drop":
            return
        if guard_mode == "change":
            incoming.inputs["query"] = "changed by guard"
            await send(incoming)
        else:
            await send(incoming)
            await send(incoming)

    a.c.native._dispatch_guard = guard
    error = SessionInputDeliveryUnknown if guard_mode == "twice" else PermissionError
    with pytest.raises(error):
        await a.c.native.send_request(request(), control=cap)
    assert queued(a) == (["extra text"] if guard_mode == "twice" else [])
    assert len(a.c.native._requests) == 1


@pytest.mark.asyncio
async def test_adapter_prepare_wait_revalidates_input_before_dispatch(active):
    a = active
    live = [True]

    def check():
        if not live[0]:
            raise PermissionError("input ended")

    cap = capture(a, check)

    async def prepare(*_):
        await asyncio.sleep(0)
        live[0] = False
        return "prepared"

    finalize = Mock()
    adapter = SimpleNamespace(
        _native_execution=a.c.native,
        _prepare_root_input_dispatch=prepare,
        _permission_inputs_for_dispatch=lambda *_: {"query": "extra text"},
        _permission_dispatch=SimpleNamespace(finalize=finalize),
    )
    req = SimpleNamespace(
        params={"input_mode": "steer"}, request_id="extra", _native_steer_control=cap
    )
    with pytest.raises(PermissionError):
        await JiuWenSwarmDeepAdapter.deliver_active_session_input(adapter, req, {})
    finalize.assert_called_once_with("prepared")
    assert queued(a) == []


@pytest.mark.asyncio
async def test_capture_same_host_value_with_different_source_is_not_authority(active):
    from openjiuwen.core.controller.schema.execution_origin import ExecutionOrigin

    with pytest.raises(PermissionError, match="original owner"):
        active.c.native.capture_steer_control(
            source=ExecutionOrigin(active.owner),
            request_id="extra",
            check_current=lambda: None,
        )
    assert queued(active) == []


@pytest.mark.asyncio
async def test_same_origin_new_round_during_lock_wait_rejects_old_control(active):
    a = active
    cap = capture(a)
    original = a.c.agent._active_interaction_round
    lock = a.c.agent._interaction_control_lock
    await lock.acquire()
    sending = asyncio.create_task(a.c.native.send_request(request(), control=cap))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    a.c.agent._active_interaction_round = copy.copy(original)
    lock.release()
    try:
        with pytest.raises(Exception):
            await sending
        assert queued(a) == []
    finally:
        a.c.agent._active_interaction_round = original
