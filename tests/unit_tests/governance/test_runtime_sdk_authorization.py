# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Public SDK mutations consult the same persisted Project ACL as ingress."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponse
from jiuwenswarm.governance.contracts import TrustedIdentity
from jiuwenswarm.governance.preparation import GovernanceError
from jiuwenswarm.runtime.service import AgentRuntime
from jiuwenswarm.runtime.session_provisioner import SessionCreateInput, RuntimeSessionProvisioner
from jiuwenswarm.server.runtime.session import project_store
from jiuwenswarm.server.runtime.session.project_access import ProjectAccessStore


@pytest.fixture
def protected_runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(project_store, "_projects_file", lambda: tmp_path / "projects.json")
    root = tmp_path / "protected"
    root.mkdir()
    project = project_store.create_project("Protected SDK", str(root), "code")
    access = ProjectAccessStore()
    access.initialize(project.project_id, "owner")
    actor = [None]
    runtime = AgentRuntime(
        agent_manager=SimpleNamespace(), initializer=AsyncMock(), plan_controller=SimpleNamespace(),
        trusted_identity_resolver=lambda _: actor[0],
    )
    runtime._started = True
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.session_metadata.get_session_metadata",
        lambda sid, **kwargs: {"project_id": project.project_id, "project_dir": str(root), "work_mode": "code"}
        if sid == "governed-session" else {},
    )
    return runtime, actor, project, root, access


@pytest.mark.asyncio
@pytest.mark.parametrize("path_object", [False, True])
async def test_create_cwd_resolves_protected_project_before_provisioning(protected_runtime, path_object):
    runtime, actor, project, root, _ = protected_runtime
    prepare = AsyncMock(return_value=object())
    runtime._session_provisioner.prepare_session_create = prepare
    value = SessionCreateInput(channel_id="tui", cwd=root if path_object else str(root), create_token="sdk-cwd")
    with pytest.raises(GovernanceError, match="denied"):
        await runtime.prepare_session_create(value)
    prepare.assert_not_awaited()
    assert runtime._session_provision_prepares == 0
    actor[0] = TrustedIdentity("owner", "worker", "authenticated-sdk-host")
    prepared = await runtime.prepare_session_create(value)
    assert runtime._governed_provisions[prepared].project_id == project.project_id
    prepare.assert_awaited_once_with(value)


@pytest.mark.asyncio
async def test_sdk_cancel_checks_acl_without_waiting_for_start(protected_runtime, monkeypatch):
    runtime, actor, _, _, _ = protected_runtime
    runtime._started = False
    runtime.start = AsyncMock(side_effect=AssertionError("cancel must not await startup"))
    cancel = AsyncMock(return_value=AgentResponse("cancel", "tui", payload={"success": True}))
    monkeypatch.setattr("jiuwenswarm.runtime.request.cancel_request", cancel)
    runtime._clear_pending_interaction = AsyncMock()
    request = AgentRequest("cancel", channel_id="tui", session_id="governed-session", user_id="owner")
    with pytest.raises(GovernanceError, match="denied"):
        await runtime.cancel_request(request)
    cancel.assert_not_awaited()
    actor[0] = TrustedIdentity("owner", "worker", "authenticated-sdk-host")
    assert (await runtime.cancel_request(request)).ok
    cancel.assert_awaited_once()
    runtime.start.assert_not_awaited()


@pytest.mark.asyncio
async def test_sdk_session_delete_requires_write_not_execute(protected_runtime):
    runtime, actor, project, _, access = protected_runtime
    delete = AsyncMock(return_value=SimpleNamespace(ok=True))
    runtime._session_provisioner.delete_session = delete
    with pytest.raises(GovernanceError, match="denied"):
        await runtime.delete_session(channel_id="tui", session_id="governed-session")
    access.replace_acl(project.project_id, "owner", acl={"executor": ["execute"]}, expected_revision=1)
    actor[0] = TrustedIdentity("executor", "worker", "authenticated-sdk-host")
    with pytest.raises(GovernanceError, match="write denied"):
        await runtime.delete_session(channel_id="tui", session_id="governed-session")
    delete.assert_not_awaited()
    actor[0] = TrustedIdentity("owner", "worker", "authenticated-sdk-host")
    assert (await runtime.delete_session(channel_id="tui", session_id="governed-session")).ok
    delete.assert_awaited_once()


@pytest.mark.asyncio
async def test_sdk_team_delete_checks_every_bound_session(protected_runtime, monkeypatch):
    runtime, actor, _, _, _ = protected_runtime
    delete = AsyncMock(return_value=SimpleNamespace(ok=True))
    runtime._session_provisioner.delete_team = delete
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.team_binding_store.get_team_binding_store",
        lambda: SimpleNamespace(get=lambda _: SimpleNamespace(session_ids=("public-session", "governed-session"))),
    )
    monkeypatch.setattr(RuntimeSessionProvisioner, "_inventory_team_session_ids", staticmethod(
        lambda name, *, binding_session_ids: list(binding_session_ids)
    ))
    with pytest.raises(GovernanceError, match="denied"):
        await runtime.delete_team(team_name="sdk-team", channel_id="tui")
    delete.assert_not_awaited()
    actor[0] = TrustedIdentity("owner", "worker", "authenticated-sdk-host")
    assert (await runtime.delete_team(team_name="sdk-team", channel_id="tui")).ok
    delete.assert_awaited_once()
