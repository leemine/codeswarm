"""Consumer ownership and confirmed transport exit, using owned asyncio tasks."""

import asyncio
from types import SimpleNamespace

import pytest

from jiuwenswarm.governance.contracts import TrustedIdentity
from jiuwenswarm.governance.tool_resources import ResourceExecutionContext
from jiuwenswarm.runtime.harness.execution_session import (
    ExecutionExitState,
    ExecutionExitUnconfirmedError,
    ExecutionSession,
)
from jiuwenswarm.runtime.harness.tool_transport import ManagedProductToolTransport
from jiuwenswarm.server.runtime.agent_adapter.engine_adapter import EngineAgentAdapter


@pytest.mark.asyncio
@pytest.mark.parametrize("during_forced_wait", [False, True])
async def test_cancelled_stop_retains_active_server_until_confirmed_exit(monkeypatch, during_forced_wait):
    from jiuwenswarm.runtime.harness import tool_transport as module

    release, cancel_received = asyncio.Event(), asyncio.Event()

    async def owned_server():
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancel_received.set()
            await release.wait()

    task = asyncio.create_task(owned_server())
    transport = ManagedProductToolTransport(None, host_session_id="synthetic")
    transport._serve_task = task
    server = SimpleNamespace(should_exit=False)
    listener = SimpleNamespace(close=lambda: None)
    transport._uvicorn, transport._socket, transport._port = server, listener, 19001
    transport._accepting_preflight = True
    if during_forced_wait:
        monkeypatch.setattr(module, "_STOP_TIMEOUT_S", .01)
    stopping = asyncio.create_task(transport.stop())
    try:
        if during_forced_wait:
            await asyncio.wait_for(cancel_received.wait(), 1)
        else:
            while not server.should_exit:
                await asyncio.sleep(0)
        stopping.cancel()
        with pytest.raises(asyncio.CancelledError):
            await stopping
        assert transport._serve_task is task
        assert transport._socket is listener
        assert not task.done()
        assert not transport.exit_confirmed
        assert not transport._accepting_preflight
        release.set()
        await asyncio.wait_for(task, 1)
        await transport.stop()
        assert transport.exit_confirmed
        assert transport._port is None
    finally:
        release.set()
        if not stopping.done():
            stopping.cancel()
        if not task.done():
            task.cancel()
        await asyncio.gather(stopping, task, return_exceptions=True)


@pytest.mark.asyncio
async def test_execution_does_not_confirm_exit_when_transport_stop_returns_without_exit():
    async def stop():
        return None

    session = SimpleNamespace(
        io=SimpleNamespace(stop=stop), _tool_transport=SimpleNamespace(stop=stop, exit_confirmed=False),
        _tool_gateway=None, _exit_state=ExecutionExitState.RUNNING,
    )
    with pytest.raises(ExecutionExitUnconfirmedError):
        await ExecutionSession._stop_owned_resources(session, router=None)
    assert session._exit_state is ExecutionExitState.EXIT_UNCONFIRMED


@pytest.mark.parametrize("subject,allowed", [("alice", True), ("bob", False)])
def test_external_consumer_ownership_requires_exact_trusted_binding_subject(subject, allowed):
    binding = SimpleNamespace(
        host_session_id="same-session", provider_id="opencode", workspace="/tmp", subject_id="alice",
    )
    owner = SimpleNamespace(
        _resource_governed=True,
        _session=SimpleNamespace(binding=binding, owns_governed_provider_session=lambda sid: sid == "native-owned"),
    )
    execution = ResourceExecutionContext(
        "project", TrustedIdentity(subject, subject, "fixture"), "same-session", "/tmp", "opencode",
    )
    assert EngineAgentAdapter.owns_external_tool_session(owner, execution, "native-owned") is allowed
