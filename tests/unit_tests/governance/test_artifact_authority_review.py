"""Actual Runtime/Native/AbilityManager certificate and real owner sidecar.

DeepAgent output and message delivery are synthetic. No model or HTTP acceptance.
"""

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from openjiuwen.core.foundation.llm import ToolCall
from openjiuwen.core.single_agent.ability_manager import AbilityManager
from openjiuwen.core.single_agent.rail.base import (
    AgentCallbackContext,
    AgentCallbackEvent,
)
from openjiuwen.core.sys_operation.cwd import _cwd_state, init_cwd
from openjiuwen.harness.deep_agent import DeepAgent
from openjiuwen.harness.schema.interaction import SendInputRequest
from openjiuwen.harness_protocol import (
    AgentExecutionSpec,
    HarnessContext,
    TurnEventKind,
)

from jiuwenswarm.agents.harness.common.rails.permissions.resource_authority_rail import (
    NativeResourceAuthorityRail,
)
from jiuwenswarm.agents.harness.common.tools.send_file_to_user import SendFileToolkit
from jiuwenswarm.agents.harness.common.tools.web_file_download import (
    WebFileDownloadManager,
)
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.governance.resources import ResourceDefinition
from jiuwenswarm.governance.tool_context import (
    tool_authority_scope,
    native_authority_source_scope,
    begin_native_execution_slice,
    end_native_execution_slice,
    current_native_execution_slice,
    ExecutionResourceAuthorities,
    submitted_artifact_issuer_factory,
    submitted_model_authorizer,
    submitted_mcp_authorizer,
    submitted_tool_authorizer,
)
from jiuwenswarm.runtime import AgentRuntime
from jiuwenswarm.runtime.harness.binding_store import ExecutionBindingStore
from jiuwenswarm.runtime.harness.config_source import ExecutionConfigSource
from jiuwenswarm.runtime.harness.native_session import NativeExecutionSession
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
    JiuWenSwarmDeepAdapter,
)
from tests.unit_tests.governance import test_workspace_download as downloads
from tests.unit_tests.runtime.harness.test_native_session import _Stream

credentials, setup = downloads.credentials, downloads.setup


@pytest.fixture
async def chain(setup, monkeypatch):
    s = setup
    sid = "alice-session"
    ability = AbilityManager(owner_id="artifact-review-" + s.tmp_path.name)
    toolkit = SendFileToolkit("origin", sid, "web", project_dir=str(s.root))
    tool = toolkit.get_tools()[0]
    ability.add_ability(tool.card, tool)
    rail = NativeResourceAuthorityRail()

    class Callbacks:
        async def execute(self, event, ctx):
            if event is AgentCallbackEvent.BEFORE_TOOL_CALL:
                await rail.before_tool_call(ctx)

    inner = SimpleNamespace(
        card=SimpleNamespace(id="root", name="root"),
        ability_manager=ability,
        agent_callback_manager=Callbacks(),
    )
    outer = MagicMock(spec=DeepAgent)
    outer.card = SimpleNamespace(id="outer")
    outer.react_agent = inner
    sent = asyncio.Event()
    outer.ensure_initialized = AsyncMock()
    outer.start = AsyncMock()
    outer.stop = AsyncMock()
    outer.send_input = AsyncMock(side_effect=lambda *a, **k: sent.set())
    outer.cancel_round = AsyncMock()
    gate = asyncio.Event()
    outer.attach_output = AsyncMock(return_value=_Stream([], gate))
    session = SimpleNamespace(
        get_session_id=lambda: sid,
        get_agent_id=lambda: "root",
        get_state=lambda *a, **k: None,
        pre_run=AsyncMock(),
        post_run=AsyncMock(),
    )
    bound = ExecutionBindingStore().bind(
        ExecutionConfigSource(explicit=AgentExecutionSpec("native", "review")),
        subject_id=s.alice.identity().subject_id,
        host_session_id=sid,
        workspace=str(s.root),
    )

    async def guard(request, *, send):
        await send(request)

    native = NativeExecutionSession(
        bound,
        agent_factory=lambda _: outer,
        session_factory=AsyncMock(return_value=session),
        dispatch_guard=guard,
    )
    adapter = JiuWenSwarmDeepAdapter.__new__(JiuWenSwarmDeepAdapter)
    adapter._is_session_scoped_adapter = True
    adapter._parent_session_id = sid
    adapter._instance = outer
    adapter._native_execution = native
    selected = [adapter]
    manager = SimpleNamespace(
        get_agent_for_session_nowait=lambda channel, lookup_sid: (
            selected[0] if channel == "web" and lookup_sid == sid else None
        )
    )
    runtime = AgentRuntime(
        agent_manager=manager,
        initializer=AsyncMock(),
        plan_controller=SimpleNamespace(reset_session=lambda _: None),
        trusted_identity_resolver=lambda _: s.current[0](),
        project_authorizer=s.access,
        organization_session_host=s.host,
    )
    await runtime._register_session(session_id=sid, channel_id="web")
    request = AgentRequest(
        request_id="original-request",
        session_id=sid,
        channel_id="web",
        req_method=ReqMethod.CHAT_SEND,
        params={"project_id": s.project.project_id, "mode": "agent.work.normal"},
    )
    handle = runtime.begin_detached_native_turn(
        sid, "synthetic-native", request.request_id
    )
    s.access.register_resource(
        s.project.project_id,
        ResourceDefinition("send-file", "tool", "native:send_file_to_user"),
        owner_subject_id="alice",
        actions=("invoke",),
        expected_revision=2,
    )
    authorities = runtime._resource_authorizers_for(request)
    await native.start(
        HarnessContext(
            agent_name="root",
            agent_id="root",
            host_session_id=sid,
            cwd=str(s.root),
            system_prompt="",
        )
    )
    with tool_authority_scope(None, provider_authorizers=authorities):
        await native.send_request(
            SendInputRequest(
                request_id=request.request_id, inputs={"query": "synthetic"}
            )
        )
    await asyncio.wait_for(sent.wait(), 3)
    token = native._native.active_turn.content.metadata["native.host_request"]
    ctx = SimpleNamespace(
        agent=outer,
        session=session,
        inputs=SimpleNamespace(run_context={"extra": {"native.host_request": token}}),
    )
    seen = []
    mutation = [None]
    mutation_done = []

    async def consume(self, envelope, **kwargs):
        if mutation[0] is not None:
            mutation[0]()
            mutation_done.append(True)
        payload = self._build_files_payload(
            envelope, valid_files=[str(s.file)], assets_by_path={}
        )
        seen.append(payload)
        return "delivered"

    monkeypatch.setattr(SendFileToolkit, "_send_file_with_envelope", consume)
    monkeypatch.setattr(WebFileDownloadManager, "_instance", s.manager)
    cwd_token = _cwd_state.set(None)
    init_cwd(str(s.root), workspace=str(s.root), project_root=str(s.root))

    async def invoke():
        with native_authority_source_scope(
            native._current_resource_authority, slice_source=native._execution_slice_for
        ):
            scope = begin_native_execution_slice(ctx)
            try:
                return await ability.execute(
                    AgentCallbackContext(agent=inner),
                    ToolCall(
                        id="artifact",
                        type="function",
                        name="send_file_to_user",
                        arguments=json.dumps(
                            {
                                "abs_file_path_list": str(s.file),
                                "target_channels": ["web"],
                            }
                        ),
                    ),
                    session=session,
                )
            finally:
                end_native_execution_slice(scope)

    try:
        yield SimpleNamespace(**locals())
    finally:
        gate.set()
        await native.stop()
        ability.teardown_tools()
        _cwd_state.reset(cwd_token)


@pytest.mark.asyncio
async def test_original_runtime_native_toolexecution_can_issue_workspace_selector(
    chain,
):
    c = chain
    assert (await c.invoke())[0][0] == "delivered"
    token = c.seen[0][0]["download_token"]
    assert c.s.capture(token).read(0, 7) == b"fixture"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        "identity",
        "subject",
        "binding",
        "owner",
        "admission",
        "native_turn",
        "slice",
        "route",
        "tool_args",
        "tool_revoke",
        "workspace_revoke",
        "workspace_regrant",
        "request_rebind",
    ],
)
async def test_original_source_changes_before_issue_are_denied(chain, change):
    c = chain

    def mutate():
        if change == "identity":
            c.s.current[0] = c.s.bob.identity
        elif change == "subject":
            c.s.current[0] = lambda: replace(c.s.alice.identity(), subject_id="other")
        elif change == "binding":
            c.native.engine = replace(
                c.native.engine,
                binding=replace(c.native.engine.binding, subject_id="other"),
            )
        elif change == "owner":
            c.s.host.compensate_owner_registration(
                c.sid, c.s.alice.identity(), expected_revision=1, expected_epoch=1
            )
        elif change == "admission":
            c.runtime.finish_detached_native_turn(
                c.sid, c.handle.execution_id, TurnEventKind.FINISHED
            )
        elif change == "native_turn":
            c.native._requests.pop(c.token)
        elif change == "slice":
            current_native_execution_slice().active = False
        elif change == "route":
            c.selected[0] = object()
        elif change == "tool_args":
            c.toolkit.channel_id = "other"
        elif change in {"tool_revoke", "workspace_revoke", "workspace_regrant"}:
            rid = "send-file" if change == "tool_revoke" else "workspace"
            revision = c.s.access.resource_grants(
                c.s.project.project_id, c.s.alice.identity()
            )["resource_revision"]
            c.s.access.revoke_resource(
                c.s.project.project_id,
                c.s.alice.identity(),
                rid,
                subject_id="alice",
                expected_revision=revision,
            )
            if change == "workspace_regrant":
                c.s.access.register_resource(
                    c.s.project.project_id,
                    ResourceDefinition(rid, "workspace", str(c.s.root)),
                    owner_subject_id="alice",
                    actions=("read",),
                    expected_revision=revision + 1,
                    delegable=True,
                )
        elif change == "request_rebind":
            c.runtime.finish_detached_native_turn(
                c.sid, c.handle.execution_id, TurnEventKind.FINISHED
            )
            c.runtime.begin_detached_native_turn(
                c.sid, "new-turn", "replacement-request"
            )
            c.request.request_id = "replacement-request"

    c.mutation[0] = mutate
    result = await c.invoke()
    assert c.mutation_done == [True], str(result)
    assert "提交文件失败" in str(result)
    assert not c.seen


def test_artifact_only_scope_keeps_other_explicit_authorities_and_restores():
    async def tool(_):
        return True

    async def model(*a, **k):
        return {}

    async def mcp(*a, **k):
        return {}

    def artifact(**k):
        return object()

    combined = ExecutionResourceAuthorities({"native": tool}, model, mcp, artifact)
    with tool_authority_scope(None, provider_authorizers=combined):
        assert submitted_artifact_issuer_factory() is artifact
        assert submitted_model_authorizer() is model
        assert submitted_mcp_authorizer() is mcp
        assert submitted_tool_authorizer() is tool
        with tool_authority_scope(
            None,
            provider_authorizers=ExecutionResourceAuthorities(
                {}, artifact_issuer_factory=artifact
            ),
        ):
            assert submitted_artifact_issuer_factory() is artifact
            assert submitted_model_authorizer() is None
            assert submitted_mcp_authorizer() is None
            assert submitted_tool_authorizer() is not tool
        assert submitted_model_authorizer() is model
        assert submitted_mcp_authorizer() is mcp
    assert submitted_artifact_issuer_factory() is None


@pytest.mark.parametrize("regrant", [False, True])
def test_direct_issuer_cannot_outlive_original_workspace_grant(setup, regrant):
    """Issuer itself must retain grant provenance without Native wrapper help."""
    s = setup
    issuer = s.issuer()
    revision = s.access.resource_grants(s.project.project_id, s.alice.identity())[
        "resource_revision"
    ]
    s.access.revoke_resource(
        s.project.project_id,
        s.alice.identity(),
        "workspace",
        subject_id="alice",
        expected_revision=revision,
    )
    if regrant:
        s.access.register_resource(
            s.project.project_id,
            ResourceDefinition("workspace", "workspace", str(s.root)),
            owner_subject_id="alice",
            actions=("read",),
            expected_revision=revision + 1,
            delegable=True,
        )
        # A fresh operation can obtain current authority; the old one cannot.
        fresh = s.manager.generate_token(
            str(s.file), "alice-session", artifact_issuer=s.issuer()
        )
        assert s.capture(fresh).read(0, 7) == b"fixture"
    with pytest.raises(downloads.WorkspaceDownloadDenied):
        s.manager.generate_token(str(s.file), "alice-session", artifact_issuer=issuer)
