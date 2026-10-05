"""Real Runtime/Coordinator/Native and core queue; synthetic allocation/model IO only."""

import asyncio
import hashlib
import json
import secrets
import time
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from openjiuwen.harness.schema.interaction import InputDispatchMode, SendInputRequest
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.governance.organization_auth import (
    authenticated_scope,
    current_identity,
    configured_authenticator,
)
from jiuwenswarm.governance.tool_context import tool_authority_scope
from jiuwenswarm.runtime import AgentRuntime
from jiuwenswarm.runtime.session import SessionWorkKind
from tests.unit_tests.runtime import test_native_runtime_source as fixtures

credentials = fixtures.credentials
native_case = fixtures.native_case
full_case = fixtures.full_case


@pytest.fixture
async def live(full_case):
    f = full_case
    token = secrets.token_urlsafe(32)
    config = json.loads(f.authpath.read_text())
    config["credentials"].append(
        {
            "actor_id": "bob",
            "sha256": hashlib.sha256(token.encode()).hexdigest(),
            "expires_at": time.time() + 600,
            "revoked": False,
        }
    )
    f.authpath.write_text(json.dumps(config))
    second = f.auth.principal({"Authorization": "Bearer " + token})
    runtime = AgentRuntime(
        agent_manager=f.manager,
        initializer=AsyncMock(),
        trusted_identity_resolver=lambda _: current_identity(),
        project_authorizer=f.access,
        resource_authorizer=f.access,
        organization_session_host=f.host if configured_authenticator() is not None else None,
    )
    f.state.runtime = runtime
    c = runtime._session_coordinator
    await c.register_session(f.sid, "web")
    entered, release = asyncio.Event(), asyncio.Event()

    async def model(*_, **__):
        entered.set()
        await release.wait()
        return {"output": "normal root"}

    f.c.react.invoke = model
    request = AgentRequest(
        "root",
        session_id=f.sid,
        channel_id="web",
        req_method=ReqMethod.CHAT_SEND,
        params={"project_id": f.project.project_id, "query": "ordinary"},
    )
    saved = {}

    async def root_body():
        resources = runtime._resource_authorizers_for(request)
        with tool_authority_scope(None, provider_authorizers=resources):
            receipt = await f.c.native.send_request(
                SendInputRequest("root", {"query": "ordinary"})
            )
        (owner,) = c._registry.select(session_id=f.sid, request_id="root")
        saved.update(
            owner=owner,
            admission=owner._native_admission,
            receipt=receipt,
            resources=resources,
        )
        await owner._native_admission.confirmed.wait()

    with authenticated_scope(f.bob):
        root = asyncio.create_task(
            c.run_unary(f.sid, "root", SessionWorkKind.CHAT_UNARY, root_body)
        )
    await asyncio.wait_for(entered.wait(), 3)
    try:
        yield SimpleNamespace(**locals())
    finally:
        release.set()
        await asyncio.wait_for(asyncio.gather(root, return_exceptions=True), 3)


def request_for(x):
    return AgentRequest(
        "input",
        session_id=x.f.sid,
        channel_id="web",
        req_method=ReqMethod.CHAT_SEND,
        params={
            "project_id": x.f.project.project_id,
            "query": "steer ordinary",
            "input_mode": "steer",
        },
    )


def queue(x):
    return x.f.c.handler.interaction_queues.drain_steering(
        expected_origin=x.saved["admission"].owned_turn._pending._origin
    )


async def input_run(x, operation):
    async def idle():
        raise AssertionError("STEER became an idle/new Turn")
        yield

    async def stream(_channel):
        yield await operation()

    with authenticated_scope(x.second):
        return [
            v async for v in x.c.stream_session_input(x.f.sid, "input", stream, idle)
        ]


@pytest.mark.asyncio
async def test_actual_host_control_keeps_root_credentials_and_closures(live):
    x = live
    req = request_for(x)
    original = x.saved["admission"].owned_turn._entry
    closures = tuple(
        getattr(original, name)
        for name in (
            "guarded_authority",
            "guarded_model_authority",
            "guarded_mcp_authority",
            "guarded_artifact_issuer",
        )
    )

    async def operation():
        x.runtime._capture_native_session_input_control(req)
        cap = req._native_steer_control
        (child,) = x.c._registry.select(session_id=x.f.sid, request_id="input")
        assert child._execution_authority is x.second
        assert x.saved["owner"]._execution_authority is x.f.bob
        assert child.parent_execution_id == x.saved["owner"].execution_id
        assert child._native_admission is None
        receipt = await x.f.c.native.send_request(
            SendInputRequest(
                "input", {"query": req.params["query"]}, mode=InputDispatchMode.STEER
            ),
            control=cap,
        )
        assert receipt.turn_id == x.saved["receipt"].turn_id
        assert (
            tuple(
                getattr(original, name)
                for name in (
                    "guarded_authority",
                    "guarded_model_authority",
                    "guarded_mcp_authority",
                    "guarded_artifact_issuer",
                )
            )
            == closures
        )
        return "accepted"

    assert await input_run(x, operation) == ["accepted"]
    assert queue(x) == ["steer ordinary"]
    assert len(x.f.c.native._requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        "input_credential",
        "project",
        "owner",
        "binding",
        "query",
        "mode",
        "owner_epoch",
        "acl_revision",
    ],
)
async def test_host_control_drift_before_actual_enqueue_is_rejected(live, change):
    x = live
    req = request_for(x)
    native = x.f.c.native
    engine = native.engine
    binding = engine.binding
    auth_bytes = x.f.authpath.read_bytes()
    store_bytes = x.f.access._load()
    captured = asyncio.Event()
    unlock = asyncio.Event()

    async def operation():
        x.runtime._capture_native_session_input_control(req)
        captured.set()
        await unlock.wait()
        return await native.send_request(
            SendInputRequest(
                "input", {"query": "steer ordinary"}, mode=InputDispatchMode.STEER
            ),
            control=req._native_steer_control,
        )

    running = asyncio.create_task(input_run(x, operation))
    await asyncio.wait_for(captured.wait(), 2)
    try:
        if change == "input_credential":
            x.f.auth.revoke(x.second)
        elif change == "project":
            req.params["project_id"] = "different-project"
        elif change == "owner":
            x.f.host.invalidate_source(
                x.f.sid, expected_epoch=x.f.host.source_epoch(x.f.sid)
            )
        elif change == "binding":
            native.engine = replace(engine, binding=replace(binding))
        elif change == "query":
            req.params["query"] = "changed"
        elif change == "mode":
            req.params["input_mode"] = "follow_up"
        elif change == "owner_epoch":
            epoch = x.f.host.invalidate_source(
                x.f.sid, expected_epoch=x.f.host.source_epoch(x.f.sid)
            )
            x.f.host.activate_source(
                x.f.sid, x.f.project.project_id, expected_epoch=epoch
            )
        elif change == "acl_revision":
            x.f.access.replace_acl(
                x.f.project.project_id, "bob", acl={}, expected_revision=1
            )
        unlock.set()
        error = None
        try:
            await running
        except Exception as exc:
            error = exc
        messages = queue(x)
        if error is None:
            assert messages == ["steer ordinary"]
            pytest.fail(
                "changed authority was accepted and appended to the original core steer queue"
            )
        assert messages == []
    finally:
        unlock.set()
        native.engine = engine
        x.f.authpath.write_bytes(auth_bytes)
        with x.f.access._locked():
            x.f.access._save(store_bytes)
        await asyncio.gather(running, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["completed", "cancelled"])
async def test_original_input_task_cannot_outlive_its_authority(live, kind):
    x = live
    req = request_for(x)
    captured = {}
    lock = x.f.c.agent._interaction_send_lock
    await lock.acquire()

    async def operation():
        x.runtime._capture_native_session_input_control(req)
        captured["cap"] = req._native_steer_control
        if kind == "completed":
            return "captured only"
        captured["producer"] = asyncio.current_task()
        return await x.f.c.native.send_request(
            SendInputRequest(
                "input", {"query": "steer ordinary"}, mode=InputDispatchMode.STEER
            ),
            control=req._native_steer_control,
        )

    running = asyncio.create_task(input_run(x, operation))
    while "cap" not in captured:
        await asyncio.sleep(0)
    try:
        if kind == "completed":
            await running
            with pytest.raises(Exception):
                await x.f.c.native.send_request(
                    SendInputRequest(
                        "input",
                        {"query": "steer ordinary"},
                        mode=InputDispatchMode.STEER,
                    ),
                    control=captured["cap"],
                )
        else:
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            captured["producer"].cancel()
            await asyncio.gather(running, return_exceptions=True)
        lock.release()
        pending = tuple(x.saved["admission"].owned_turn._pending._admissions)
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        assert queue(x) == []
    finally:
        if lock.locked():
            lock.release()
        await asyncio.gather(running, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["input_credential", "query", "mode"])
@pytest.mark.parametrize(
    "lock_name", ["_interaction_send_lock", "_interaction_control_lock"]
)
async def test_actual_input_authority_rechecked_after_core_lock(
    live, change, lock_name
):
    x = live
    req = request_for(x)
    captured = asyncio.Event()
    original = x.f.authpath.read_bytes()
    lock = getattr(x.f.c.agent, lock_name)
    await lock.acquire()

    async def operation():
        x.runtime._capture_native_session_input_control(req)
        captured.set()
        return await x.f.c.native.send_request(
            SendInputRequest(
                "input", {"query": "steer ordinary"}, mode=InputDispatchMode.STEER
            ),
            control=req._native_steer_control,
        )

    running = asyncio.create_task(input_run(x, operation))
    await asyncio.wait_for(captured.wait(), 2)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    try:
        if change == "input_credential":
            x.f.auth.revoke(x.second)
        elif change == "query":
            req.params["query"] = "different"
        else:
            req.params["input_mode"] = "follow_up"
        lock.release()
        with pytest.raises(Exception):
            await running
        assert queue(x) == []
    finally:
        x.f.authpath.write_bytes(original)
        if lock.locked():
            lock.release()
        await asyncio.gather(running, return_exceptions=True)


@pytest.mark.asyncio
async def test_actual_runtime_stream_facade_cached_adapter_captures_control(live):
    """Use the production Runtime entry/facade/adapter, not a manual cap call."""
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
        JiuWenSwarmDeepAdapter,
    )

    x = live
    req = request_for(x)
    # Initialize unrelated adapter fields omitted by the existing allocation
    # fixture; keep the original root/child/native object identities.
    JiuWenSwarmDeepAdapter.__init__(x.f.child)
    x.f.child._is_session_scoped_adapter = True
    x.f.child._parent_session_id = x.f.sid
    x.f.child._native_execution = x.f.c.native
    x.f.child._instance = x.f.c.agent

    async def idle():
        raise AssertionError("production input selected a new Turn")
        yield

    with authenticated_scope(x.second):
        events = [
            v
            async for v in x.c.stream_session_input(
                x.f.sid,
                "input",
                lambda channel: x.runtime._stream_session_input_started(req, channel),
                idle,
            )
        ]
    assert getattr(req, "_native_steer_control", None) is not None
    messages = queue(x)
    assert len(messages) == 1 and "steer ordinary" in messages[0]
    assert events
    assert x.saved["owner"]._execution_authority is x.f.bob
    (child,) = x.c._registry.select(session_id=x.f.sid, request_id="input")
    assert child._execution_authority is x.second and child._native_admission is None
    assert len(x.f.c.native._requests) == 1
