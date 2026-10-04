"""Native MCP host composition, without a real Provider.

Runtime/coordinator, authentication/owner storage, resource authorization, Native
protocol dispatch, AbilityManager, MCPTool and the MCP SDK are real. DeepAgent's
model-free queue/stream, Session allocation, permission-input dispatch wrapper and
remote HTTP server are fixtures. The stop gate binds actual DeepAgent stop methods.
"""

import asyncio
import hashlib
import json
import time
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from openjiuwen.core.foundation.llm import ToolCall
from openjiuwen.core.runner import Runner
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.deep_agent import DeepAgent
from openjiuwen.harness.schema.interaction import SendInputRequest
from openjiuwen.harness_protocol import (
    AgentExecutionSpec,
    HarnessContext,
    TurnLifecycleEvent,
    TurnEventKind,
)

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.governance.organization_auth import (
    OrganizationAuthenticator,
    CONFIG_ENV,
)
from jiuwenswarm.governance.resources import ResourceDefinition
from jiuwenswarm.governance.tool_context import (
    begin_native_execution_slice,
    end_native_execution_slice,
    tool_authority_scope,
    current_native_execution_slice,
)
from jiuwenswarm.runtime import AgentRuntime
from jiuwenswarm.runtime.harness.binding_store import ExecutionBindingStore
from jiuwenswarm.runtime.harness.config_source import ExecutionConfigSource
from jiuwenswarm.runtime.harness.execution_session import (
    ExecutionExitState,
    ExecutionExitUnconfirmedError,
)
from jiuwenswarm.runtime.service import _StoredResourceAuthority
from jiuwenswarm.runtime.session.model import SessionWorkKind
from jiuwenswarm.server.runtime.session.sharing_host import SharingHostService
from tests.unit_tests.agentserver.mcp import (
    test_native_registration as registration_fixtures,
)
from tests.unit_tests.governance.test_mcp_credentials import raw_config
from tests.unit_tests.runtime.harness.test_native_session import _Stream, _answer


native = registration_fixtures.native


@pytest.fixture
async def host(native, monkeypatch):
    from jiuwenswarm.common import config, utils
    from jiuwenswarm.governance import session_boundary
    from jiuwenswarm.server.runtime.session import lifecycle, session_metadata
    from jiuwenswarm.server.runtime.agent_adapter import interface_deep
    from jiuwenswarm.server.runtime.session.kv_cache import kv_cache_application_runtime

    n = native
    raw = raw_config(n.pid)
    monkeypatch.setattr(config, "get_config_raw", lambda: raw)
    n.store.register_resource(
        n.pid,
        ResourceDefinition("catalog-text", "tool", "mcp:declared:echo"),
        owner_subject_id="bob",
        actions=("invoke",),
        expected_revision=2,
    )
    sessions = n.tmp_path / "sessions"
    monkeypatch.setattr(utils, "get_agent_sessions_dir", lambda: sessions)
    monkeypatch.setattr(session_metadata, "get_agent_sessions_dir", lambda: sessions)
    monkeypatch.setattr(lifecycle, "get_agent_sessions_dir", lambda: sessions)
    folder = sessions / "sid"
    folder.mkdir(parents=True)
    (folder / "metadata.json").write_text(
        json.dumps(
            {"project_id": n.pid, "project_dir": str(n.work), "work_mode": "work"}
        )
    )
    authfile = n.tmp_path / "auth.json"
    token = "synthetic-host-mcp-credential-fixture-only"
    authfile.write_text(
        json.dumps(
            {
                "authority": "fixture",
                "signing_key": "ab" * 32,
                "credentials": [
                    {
                        "actor_id": "bob",
                        "sha256": hashlib.sha256(token.encode()).hexdigest(),
                        "expires_at": time.time() + 3600,
                    }
                ],
            }
        )
    )
    authfile.chmod(0o600)
    monkeypatch.setenv(CONFIG_ENV, str(authfile))
    auth = OrganizationAuthenticator(authfile)
    principal = auth.principal({"Authorization": "Bearer " + token})
    sharing = SharingHostService(
        lambda _identity, actor: principal.identity() if actor == "bob" else None,
        known_actor=lambda who: who == principal.identity(),
        storage=n.store,
    )
    sharing.register_owner_and_source("sid", principal.identity(), n.pid)
    monkeypatch.setattr(session_boundary, "organization_sharing_host", lambda: sharing)
    outer = MagicMock(spec=DeepAgent)
    outer.card = SimpleNamespace(id="outer", name="outer")
    outer.react_agent = n.agent
    outer.ensure_initialized = AsyncMock()
    queue = asyncio.Queue()

    async def worker():
        while (item := await queue.get()) is not None:
            await process_input(item)

    async def start_worker(**kwargs):
        outer._interaction_supervisor_task = asyncio.create_task(worker())

    async def stop_worker():
        await queue.put(None)
        await outer._interaction_supervisor_task

    outer.start = AsyncMock(side_effect=start_worker)
    outer.stop = AsyncMock(side_effect=stop_worker)
    outer.cancel_round = AsyncMock()
    n.session.pre_run = AsyncMock()
    n.session.post_run = AsyncMock()
    state = SimpleNamespace(
        done=None,
        terminal=None,
        result=None,
        delivered=[],
        slices=[],
        error=None,
        request_no=0,
    )
    outer.attach_output = AsyncMock(
        side_effect=lambda: _Stream([_answer()], state.done)
    )
    adapter = object.__new__(interface_deep.JiuWenSwarmDeepAdapter)
    adapter._instance = outer
    adapter._is_session_scoped_adapter = True
    adapter._parent_session_id = "sid"
    adapter.install_session_input_guard = AsyncMock()

    async def dispatch(request, *, send):
        await send(request)

    adapter._send_input_with_permission_resume_guard = AsyncMock(side_effect=dispatch)
    monkeypatch.setattr(interface_deep, "create_agent_session", lambda **_: n.session)
    monkeypatch.setattr(
        kv_cache_application_runtime, "get_kv_cache_runtime", lambda: None
    )
    manager = SimpleNamespace(
        get_agent_for_session_nowait=lambda channel, sid: (
            adapter if (channel, sid) == ("web", "sid") else None
        )
    )
    runtime = AgentRuntime(
        initializer=AsyncMock(),
        agent_manager=manager,
        trusted_identity_resolver=lambda _: principal.identity(),
        project_authorizer=n.store,
        resource_authorizer=_StoredResourceAuthority(),
    )
    runtime._started = True
    await runtime._session_coordinator.register_session("sid", "web")
    bindings = ExecutionBindingStore()
    context = HarnessContext(
        agent_name="native",
        agent_id="outer",
        host_session_id="sid",
        cwd=str(n.work),
        system_prompt="",
    )

    async def observe(event):
        if isinstance(event.event, TurnLifecycleEvent) and event.event.kind in {
            TurnEventKind.FINISHED,
            TurnEventKind.FAILED,
            TurnEventKind.ABORTED,
        }:
            if state.terminal is not None:
                state.terminal.set()

    from jiuwenswarm.server.runtime.mcp import native_registration

    installed = []
    original_install = native_registration.install_native_mcp_tools

    def observe_install(**kwargs):
        registered = original_install(**kwargs)
        installed.append(registered)
        return registered

    monkeypatch.setattr(
        native_registration, "install_native_mcp_tools", observe_install
    )
    execution = await adapter.start_native_interaction(
        source=ExecutionConfigSource(
            explicit=AgentExecutionSpec("native", "host-combination")
        ),
        bindings=bindings,
        subject_id="bob",
        workspace=str(n.work),
        context=context,
        event_observer=observe,
    )
    (record,) = installed[0].records
    assert (
        record.agent is n.agent
        and record.session is n.session
        and record.native_session is execution
    )
    assert record.execution_binding is execution.engine.binding

    async def process_input(request):
        state.delivered.append(request)
        try:
            # Fixture DeepAgent uses the production iteration slice admission;
            # original request was delivered by real SerializedTurnHarness.
            ctx = SimpleNamespace(
                agent=outer,
                session=n.session,
                inputs=SimpleNamespace(run_context=request.inputs["run"]["context"]),
            )
            handle = begin_native_execution_slice(ctx)
            try:
                state.slices.append(current_native_execution_slice())
                state.result = await n.manager.execute(
                    AgentCallbackContext(agent=n.agent),
                    ToolCall(
                        id="host-mcp-call",
                        type="function",
                        name=record.alias,
                        arguments=json.dumps({"text": "host-ordinary"}),
                    ),
                    session=n.session,
                )
            finally:
                end_native_execution_slice(handle)
        except BaseException as exc:
            state.error = exc
            raise
        finally:
            state.done.set()

    outer.send_input = AsyncMock(side_effect=queue.put)

    async def invoke(*, mcp_only=False):
        state.request_no += 1
        rid = "request-" + str(state.request_no)
        state.done = asyncio.Event()
        state.terminal = asyncio.Event()
        state.result = None
        state.error = None
        req = AgentRequest(
            rid, channel_id="web", session_id="sid", req_method=ReqMethod.CHAT_SEND
        )

        async def body():
            bundle = runtime._resource_authorizers_for(req)
            if mcp_only:
                from jiuwenswarm.governance.tool_context import (
                    ExecutionResourceAuthorities,
                )

                bundle = ExecutionResourceAuthorities(
                    {}, mcp_authorizer=bundle.mcp_authorizer
                )
            with tool_authority_scope(None, provider_authorizers=bundle):
                receipt = await execution.send_request(
                    SendInputRequest(request_id=rid, inputs={"query": "ordinary"})
                )
            await asyncio.wait_for(state.done.wait(), 3)
            await asyncio.wait_for(state.terminal.wait(), 3)
            if state.error:
                raise state.error
            return receipt, state.result, bundle

        return await runtime._session_coordinator.run_unary(
            "sid", rid, SessionWorkKind.CHAT_UNARY, body
        )

    yield SimpleNamespace(**locals())
    if adapter._native_execution is not None:
        outer.stop = AsyncMock(side_effect=stop_worker)
        await adapter.stop_interaction()
    session_metadata.remove_session_metadata_cache("sid")


@pytest.mark.asyncio
async def test_runtime_request_to_actual_native_factory_and_sdk(host):
    receipt, result, bundle = await host.invoke()
    assert "host-ordinary" in str(result)
    assert receipt.turn_id
    assert len(host.n.sent) == 4 and host.n.closed == 1
    delivered = host.state.delivered[0]
    assert delivered.inputs["run"]["context"]["extra"]["native.host_request"]
    bound = host.state.slices[0]
    assert bound.owner is host.execution and not bound.active
    assert bound.mcp_authorizer is not bundle.mcp_authorizer
    assert host.sharing.owner_current("sid", host.principal.identity())


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["catalog", "binding"])
async def test_original_catalog_or_binding_change_denies(host, change):
    if change == "catalog":
        host.raw["mcp"]["servers"][0]["organization"]["revision"] = "new"
    else:
        object.__setattr__(
            host.execution.engine, "binding", replace(host.execution.engine.binding)
        )
    receipt, result, _ = await host.invoke()
    assert not host.n.sent
    assert "host-ordinary" not in str(result)


@pytest.mark.asyncio
async def test_stop_timeout_retains_registration_then_confirmed_stop_reclaims(
    host, monkeypatch
):
    from jiuwenswarm.runtime.harness import native_session

    monkeypatch.setattr(native_session, "RESOURCE_STOP_TIMEOUT_S", 0.03)
    gate = asyncio.Event()

    async def gated_stop():
        await gate.wait()
        await host.stop_worker()

    host.outer.stop = AsyncMock(side_effect=gated_stop)
    with pytest.raises(ExecutionExitUnconfirmedError):
        await host.adapter.stop_interaction(require_owned_exit=True)
    assert host.execution.exit_state is ExecutionExitState.EXIT_UNCONFIRMED
    assert host.adapter._native_execution is host.execution
    assert host.installed[0].records
    assert (
        Runner.resource_mgr.get_tool(host.record.registry_id, session=None)
        is host.record.executor
    )
    assert host.bindings._bindings
    gate.set()
    await host.adapter.stop_interaction(require_owned_exit=True)
    assert host.execution.exit_state is ExecutionExitState.EXIT_CONFIRMED
    assert host.adapter._native_execution is None
    assert not host.record._live[0]
    assert Runner.resource_mgr.get_tool(host.record.registry_id, session=None) is None
    assert not host.bindings._bindings


@pytest.mark.asyncio
async def test_direct_execution_stop_reclaims_registered_slots(host):
    await host.execution.stop()
    assert host.execution.exit_state is ExecutionExitState.EXIT_CONFIRMED
    assert Runner.resource_mgr.get_tool(host.record.registry_id, session=None) is None
    assert not host.record._live[0]


@pytest.mark.asyncio
async def test_real_deep_stop_pending_round_must_keep_registration(host, monkeypatch):
    """Core actual stop/cancel methods, with an owned round's pending finally."""
    from jiuwenswarm.runtime.harness import execution_session
    from openjiuwen.core.controller.modules import task_scheduler
    from openjiuwen.harness.schema.interaction import ActiveInteractionRound, RoundWorkItem

    monkeypatch.setattr(task_scheduler, "_STOP_TIMEOUT_SECONDS", 0.03)

    monkeypatch.setattr(execution_session, "RESOURCE_STOP_TIMEOUT_S", 0.03)
    started = asyncio.Event()
    cancelled = asyncio.Event()
    release = asyncio.Event()

    async def round_work():
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            await release.wait()

    round_task = asyncio.create_task(round_work())
    await started.wait()
    outer = host.outer
    outer._interaction_start_lock = asyncio.Lock()
    outer._interaction_started = True
    outer._event_manager = SimpleNamespace(
        discard_all_work=lambda: None, mark_finished=lambda _: None
    )
    outer._interaction_output = SimpleNamespace(shutdown=AsyncMock())
    outer._try_transition_interaction_phase = lambda _: True
    outer._interaction_forwarder_task = None
    outer._interaction_emit_tasks = set()
    outer._interaction_session = host.n.session
    host.n.session.close_stream = AsyncMock()
    outer._active_interaction_round = ActiveInteractionRound(
        work=RoundWorkItem.user(request_id='original-stop', inputs={'query': 'fixture'}), task_id='',
    )
    outer._interaction_round_task = round_task
    outer._stopping_interaction_round_tasks = set()
    if hasattr(DeepAgent, "_drain_stopping_interaction_rounds"):
        outer._drain_stopping_interaction_rounds = (
            DeepAgent._drain_stopping_interaction_rounds.__get__(outer, DeepAgent)
        )
    outer.loop_controller = SimpleNamespace(
        stop=AsyncMock(),
        unbind_session=AsyncMock(),
        task_scheduler=SimpleNamespace(_running_tasks={}, _scheduler_task=None),
    )
    outer.abort = AsyncMock()
    outer._cancel_active_round = DeepAgent._cancel_active_round.__get__(
        outer, DeepAgent
    )
    outer._stop_interaction_locked = DeepAgent._stop_interaction_locked.__get__(
        outer, DeepAgent
    )
    outer.stop = DeepAgent.stop.__get__(outer, DeepAgent)
    try:
        with pytest.raises(Exception):
            await host.adapter.stop_interaction(require_owned_exit=True)
        assert cancelled.is_set() and not round_task.done()
        # Red regression if after_stop released before the facade's original join.
        assert (
            Runner.resource_mgr.get_tool(host.record.registry_id, session=None)
            is host.record.executor
        )
    finally:
        release.set()
        await round_task
        await host.adapter.stop_interaction(require_owned_exit=True)


@pytest.mark.asyncio
async def test_mcp_only_bundle_actual_send_still_carries_original_host_token(host):
    _, result, bundle = await host.invoke(mcp_only=True)
    delivered = host.state.delivered[0]
    assert delivered.inputs["run"]["context"]["extra"]["native.host_request"]
    assert host.state.slices[0].owner is host.execution
    assert host.state.slices[0].mcp_authorizer is not None
    assert (
        not host.n.sent
    )  # No native tool grant/proof; do not infer one from MCP alone.


@pytest.mark.asyncio
@pytest.mark.parametrize("cleanup_fails", [False, True])
async def test_startup_failure_registration_cleanup_and_retry(host, cleanup_fails):
    await host.adapter.stop_interaction()
    bound = host.bindings.bind(
        ExecutionConfigSource(explicit=AgentExecutionSpec("native", "retry-start")),
        subject_id="bob",
        host_session_id="sid",
        workspace=str(host.n.work),
    )
    execution = host.adapter.build_native_execution(bound)

    async def fail_start(**kwargs):
        await host.start_worker(**kwargs)
        raise RuntimeError("synthetic-start-failure")

    host.outer.start = AsyncMock(side_effect=fail_start)
    if cleanup_fails:
        host.outer.stop = AsyncMock(side_effect=RuntimeError("synthetic-stop-failure"))
    try:
        with pytest.raises(Exception):
            await execution.start(host.context)
        record = host.installed[-1].records[0]
        assert record.native_session is execution
        if cleanup_fails:
            assert execution.exit_state is ExecutionExitState.EXIT_UNCONFIRMED
            assert (
                Runner.resource_mgr.get_tool(record.registry_id, session=None)
                is record.executor
            )
            host.outer.stop = AsyncMock(side_effect=host.stop_worker)
            await execution.stop()
        assert Runner.resource_mgr.get_tool(record.registry_id, session=None) is None
        assert not record._live[0]
    finally:
        host.outer.stop = AsyncMock(side_effect=host.stop_worker)
        if not execution.closed:
            await execution.stop()
        host.bindings.release(bound.binding)
