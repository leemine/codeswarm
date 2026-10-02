# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponse, AgentResponseChunk
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.governance.contracts import AuthorizationDecision, TrustedIdentity
from jiuwenswarm.governance.preparation import AlreadySubmitted, GovernanceError
from jiuwenswarm.runtime import AgentRuntime
from jiuwenswarm.runtime.session_provisioner import (
    PreparedSessionProvision, SessionCreateInput, SessionCreateResult,
    SessionProvisionCommitTiming, SessionProvisionState,
)


class Authority:
    allowed = True
    def authorize(self, project_id, actor_id, action):
        return AuthorizationDecision(self.allowed and actor_id == "alice", project_id, actor_id,
                                     action, 1, "test ACL")


@pytest.fixture
def env(monkeypatch):
    authority = Authority()
    identity = TrustedIdentity("alice", "worker", "authenticated-test-host")
    manager = SimpleNamespace(begin_foreground_chat=AsyncMock(), end_foreground_chat=AsyncMock())
    plan = SimpleNamespace(ensure_state=AsyncMock(return_value=SimpleNamespace(events=[])),
                           check_post_process_exit=AsyncMock(return_value=[]))
    runtime = AgentRuntime(agent_manager=manager, initializer=AsyncMock(), plan_controller=plan,
                           trusted_identity_resolver=lambda _: identity, project_authorizer=authority)
    runtime._started = True
    monkeypatch.setattr("jiuwenswarm.server.runtime.session.session_metadata.get_session_metadata",
                        lambda *args, **kwargs: {"project_id": "project", "work_mode": "work"})
    request = AgentRequest("request", channel_id="web", session_id="s", req_method=ReqMethod.CHAT_SEND,
                           params={"query": "original", "work_mode": "work"})
    calls = []
    async def process(req):
        calls.append(req.params["query"])
        return AgentResponse(req.request_id, "web", payload={"event_type": "chat.final"})
    agent = SimpleNamespace(process_message=process, execute_message=process)
    runtime._prepare_chat_turn = AsyncMock(return_value=("agent", None, agent))
    return runtime, authority, request, agent, calls


async def invoke(runtime, request):
    return await runtime._invoke_started(request, trigger_hook=False, on_control_event=None, agent_execution=None)


@pytest.mark.asyncio
async def test_revocation_during_agent_preparation_never_dispatches(env):
    runtime, authority, request, agent, calls = env
    async def prepare(*args, **kwargs):
        authority.allowed = False
        return "agent", None, agent
    runtime._prepare_chat_turn = prepare
    events = await invoke(runtime, request)
    assert not calls
    assert any(not event.ok for event in events)


@pytest.mark.asyncio
async def test_invocation_is_not_resubmitted_after_success(env):
    runtime, _, request, _, calls = env
    await invoke(runtime, request)
    with pytest.raises(AlreadySubmitted, match="accepted"):
        await invoke(runtime, request)
    assert calls == ["original"]


@pytest.mark.asyncio
async def test_unknown_dispatch_failure_cannot_retry(env):
    runtime, _, request, agent, calls = env
    async def unknown(req):
        calls.append("committed-but-no-ack")
        raise ConnectionError("response lost")
    agent.process_message = agent.execute_message = unknown
    events = await invoke(runtime, request)
    assert any(not event.ok for event in events)
    with pytest.raises(AlreadySubmitted, match="unknown"):
        await invoke(runtime, request)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_stream_rechecks_before_first_provider_call(env):
    runtime, authority, request, agent, calls = env
    async def stream(req):
        calls.append("stream")
        yield AgentResponseChunk(req.request_id, "web", payload={"event_type": "chat.final"})
    agent.process_message_stream = stream
    async def ready(_): authority.allowed = False
    events = [event async for event in runtime._stream_started(
        request, trigger_hook=False, on_control_event=None, background=False,
        on_agent_ready=ready, agent_execution=None)]
    assert not calls
    assert any(not event.ok for event in events)


def test_wire_identity_does_not_authorize_protected_project(env):
    runtime, _, request, _, _ = env
    runtime._trusted_identity_resolver = None
    request.user_id = "alice"
    request.metadata = {"actor_id": "alice", "authority": "host"}
    with pytest.raises(GovernanceError, match="denied"):
        runtime._governance_owned_request(request)


def test_snapshot_is_owned_and_project_cannot_be_substituted(env):
    runtime, _, request, _, _ = env
    owned = runtime._governance_owned_request(request)
    request.params["query"] = "tampered"
    assert owned.params["query"] == "original"
    request.params["project_id"] = "other"
    with pytest.raises(GovernanceError, match="does not match"):
        runtime._governance_owned_request(request)


@pytest.mark.asyncio
async def test_create_revoke_compensates_only_owned_provision(env):
    runtime, authority, _, _, _ = env
    released = []
    async def abort(): released.append("own-lease")
    token = object()
    result = SessionCreateResult(channel_id="web", session_id="new", project_id="project", project_dir="",
                                 work_mode="work", persist_session=False, prewarm_hit=False,
                                 prewarm_status="none", created=True, canonical_mode="agent.work.normal")
    timing = SessionProvisionCommitTiming.BEFORE_RESULT_DELIVERY
    lease = PreparedSessionProvision(owner_token=token, result=result, commit_timing=timing, abort_hook=abort)
    async def abort_lease(prepared): await prepared.abort_for_owner(token)
    runtime._session_provisioner = SimpleNamespace(prepare_session_create=AsyncMock(return_value=lease),
                                                  abort_session_provision=abort_lease,
                                                  commit_session_provision=AsyncMock())
    prepared = await runtime.prepare_session_create(SessionCreateInput(channel_id="web", project_id="project", create_token="create"))
    authority.allowed = False
    with pytest.raises(GovernanceError, match="denied"):
        await runtime.commit_session_provision(prepared, timing=timing)
    assert released == ["own-lease"]
    assert prepared.state is SessionProvisionState.ABORTED
    runtime._session_provisioner.commit_session_provision.assert_not_awaited()
    assert not runtime._pending_session_provisions


@pytest.mark.asyncio
async def test_create_unknown_is_not_resent_or_compensated(env):
    runtime, _, _, _, _ = env
    timing = SessionProvisionCommitTiming.BEFORE_RESULT_DELIVERY
    result = SessionCreateResult(channel_id="web", session_id="new", project_id="project", project_dir="",
                                 work_mode="work", persist_session=False, prewarm_hit=False,
                                 prewarm_status="none", created=True, canonical_mode="agent.work.normal")
    lease = PreparedSessionProvision(owner_token=object(), result=result, commit_timing=timing)
    # An adapter may lose its response without exposing a committed flag. The
    # governance owner must not infer that rollback or repeat is safe.
    runtime._session_provisioner = SimpleNamespace(
        prepare_session_create=AsyncMock(return_value=lease),
        commit_session_provision=AsyncMock(side_effect=ConnectionError("ack lost")),
        abort_session_provision=AsyncMock(),
    )
    prepared = await runtime.prepare_session_create(SessionCreateInput(channel_id="web", project_id="project", create_token="create"))
    with pytest.raises(ConnectionError):
        await runtime.commit_session_provision(prepared, timing=timing)
    with pytest.raises(AlreadySubmitted, match="unknown"):
        await runtime.commit_session_provision(prepared, timing=timing)
    assert runtime._session_provisioner.commit_session_provision.await_count == 1
    runtime._session_provisioner.abort_session_provision.assert_not_awaited()


@pytest.mark.asyncio
async def test_generation_change_during_preparation_prevents_dispatch(env):
    runtime, _, request, agent, calls = env
    generation = [1]
    runtime._governance_generation = lambda _: generation[0]
    async def prepare(*args, **kwargs):
        generation[0] = 2
        return "agent", None, agent
    runtime._prepare_chat_turn = prepare
    events = await invoke(runtime, request)
    assert not calls
    assert any(not event.ok for event in events)


@pytest.mark.asyncio
@pytest.mark.parametrize("existing", [False, True])
async def test_rejected_preparation_releases_only_new_session_scope(env, existing):
    runtime, authority, request, agent, calls = env
    runtime.uses_session_runtime = lambda _: True
    runtime._agent_manager._session_execution_bindings = {}
    runtime._agent_manager.get_agent_for_session_nowait = lambda *args: agent if existing else None
    runtime._agent_manager.cleanup_session_runtime = AsyncMock()
    async def prepare(*args, **kwargs):
        authority.allowed = False
        return "agent", None, agent
    runtime._prepare_chat_turn = prepare
    await invoke(runtime, request)
    assert not calls
    assert runtime._agent_manager.cleanup_session_runtime.await_count == (0 if existing else 1)


@pytest.mark.parametrize("child", [False, True])
def test_alias_work_mode_or_subdirectory_cannot_bypass_protected_project(env, monkeypatch, tmp_path, child):
    runtime, _, request, _, _ = env
    root = tmp_path / "protected"
    target = root / "child" if child else root
    request.session_id = None
    request.params.update(project_id="legacy-alias", project_dir=str(target), work_mode="code")
    legacy = SimpleNamespace(project_id="legacy-alias", project_dir=str(target), work_mode="code")
    protected = SimpleNamespace(project_id="protected", project_dir=str(root), work_mode="work")
    monkeypatch.setattr("jiuwenswarm.server.runtime.session.project_store.get_project_by_id", lambda *args, **kwargs: legacy)
    monkeypatch.setattr("jiuwenswarm.server.runtime.session.project_store.get_project_by_dir_and_mode", lambda *args, **kwargs: legacy)
    monkeypatch.setattr("jiuwenswarm.server.runtime.session.project_store.list_projects", lambda **kwargs: [legacy, protected])
    with pytest.raises(GovernanceError, match="overlaps"):
        runtime._governance_owned_request(request)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["fork", "switch"])
async def test_fork_and_switch_recheck_and_abort_on_revocation(env, operation):
    from jiuwenswarm.runtime.session_provisioner import SessionForkInput, SessionForkResult, SessionSwitchInput, SessionSwitchResult
    runtime, authority, _, _, _ = env
    timing = SessionProvisionCommitTiming.BEFORE_RESULT_DELIVERY
    token = object()
    released = []
    async def abort(): released.append(operation)
    if operation == "fork":
        value = SessionForkInput(channel_id="web", source_session_id="source")
        result = SessionForkResult(channel_id="web", source_session_id="source", session_id="new", title="fork")
    else:
        value = SessionSwitchInput(channel_id="web", target_session_id="target")
        result = SessionSwitchResult(channel_id="web", session_id="target", mode="agent")
    lease = PreparedSessionProvision(owner_token=token, result=result, commit_timing=timing, abort_hook=abort)
    async def abort_lease(prepared): await prepared.abort_for_owner(token)
    runtime._session_provisioner = SimpleNamespace(**{
        f"prepare_session_{operation}": AsyncMock(return_value=lease),
        "abort_session_provision": abort_lease,
        "commit_session_provision": AsyncMock(),
    })
    prepared = await getattr(runtime, f"prepare_session_{operation}")(value)
    authority.allowed = False
    with pytest.raises(GovernanceError, match="denied"):
        await runtime.commit_session_provision(prepared, timing=timing)
    assert released == [operation]
    runtime._session_provisioner.commit_session_provision.assert_not_awaited()


@pytest.mark.parametrize("source", ["cwd", "trusted_dirs"])
def test_legacy_workspace_fallback_cannot_bypass_protected_project(env, monkeypatch, tmp_path, source):
    runtime, authority, request, _, _ = env
    runtime._trusted_identity_resolver = None
    authority.allowed = False
    root = tmp_path / "protected"
    protected = SimpleNamespace(project_id="protected", project_dir=str(root), work_mode="work")
    request.session_id = None
    request.params = {source: str(root) if source == "cwd" else [str(root)], "mode": "team", "work_mode": "work"}
    monkeypatch.setattr("jiuwenswarm.server.runtime.session.project_store.get_project_by_dir_and_mode", lambda *args, **kwargs: protected)
    monkeypatch.setattr("jiuwenswarm.server.runtime.session.project_store.list_projects", lambda **kwargs: [protected])
    with pytest.raises(GovernanceError, match="denied"):
        runtime._governance_owned_request(request)


def test_secondary_trusted_directory_cannot_bypass_protected_project(env, monkeypatch, tmp_path):
    runtime, _, request, _, _ = env
    root = tmp_path / "protected"
    protected = SimpleNamespace(project_id="protected", project_dir=str(root), work_mode="work")
    request.session_id = None
    request.params = {"trusted_dirs": [str(tmp_path / "public"), str(root)], "mode": "team"}
    monkeypatch.setattr("jiuwenswarm.server.runtime.session.project_store.get_project_by_dir_and_mode", lambda *args, **kwargs: None)
    monkeypatch.setattr("jiuwenswarm.server.runtime.session.project_store.list_projects", lambda **kwargs: [protected])
    with pytest.raises(GovernanceError, match="overlaps"):
        runtime._governance_owned_request(request)
