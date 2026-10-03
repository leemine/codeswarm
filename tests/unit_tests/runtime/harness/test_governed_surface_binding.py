# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Trusted execution subjects retain persisted Surface and recovery boundaries."""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from openjiuwen.harness_protocol import AgentExecutionSpec
from openjiuwen.harness.engine.config import config_fingerprint

from jiuwenswarm.runtime.harness.binding_store import ExecutionBindingStore
from jiuwenswarm.runtime.harness.recovery_store import ExecutionRecoveryUnavailableError
from jiuwenswarm.runtime.harness.request_binding import bind_admitted_request_execution
from jiuwenswarm.runtime.harness.surface import SurfaceAdmissionError, creation_surface


@pytest.fixture
def admission(tmp_path, monkeypatch):
    from jiuwenswarm.runtime.harness import recovery_store

    spec = AgentExecutionSpec("codex", "r1", provider_config={"model": "test"})
    monkeypatch.setattr("jiuwenswarm.common.config.get_config", lambda: {
        "execution": {"default_profile_id": "codex", "profiles": {
            "codex": {"provider_id": "codex", "config_revision": "r1",
                      "provider_config": {"model": "test"}},
        }},
    })
    monkeypatch.setattr("jiuwenswarm.common.utils.get_agent_workspace_dir", lambda: tmp_path / "internal")
    sessions = tmp_path / "sessions"
    monkeypatch.setattr(recovery_store, "resolve_session_dir", lambda sid, create=False: (sessions / sid, None))
    monkeypatch.setattr(recovery_store, "get_read_history_path", lambda sid: sessions / sid / "history.jsonl")
    manager = SimpleNamespace(execution_bindings=ExecutionBindingStore(), _session_execution_bindings={})
    manager.remember_execution_binding = lambda channel, session, binding: manager._session_execution_bindings.update(
        {(channel, session): binding}
    )
    project = tmp_path / "project"
    project.mkdir()
    request = SimpleNamespace(session_id="surface-session", channel_id="web", user_id="routing-user", params={})
    metadata = {
        "session_id": request.session_id, "channel_id": "web", "user_id": "routing-user",
        "mode": "agent.work.normal", "work_mode": "work", "project_dir": str(project),
        "execution_profile_id": "codex", "execution_config_revision": "r1",
        "execution_config_fingerprint": config_fingerprint(spec),
    }
    metadata["surface_creation"] = creation_surface(metadata)

    def bind(subject=None):
        return bind_admitted_request_execution(
            manager, request, str(project), session_metadata=metadata, trusted_subject_id=subject,
        )

    return bind, manager, request, metadata


def test_trusted_subject_projection_preserves_persisted_surface_and_routing_user(admission):
    bind, _, request, metadata = admission
    before = deepcopy(metadata)
    route = bind("host-worker")
    assert route.bound.binding.subject_id == "host-worker"
    assert route.surface.identity.binding is route.bound.binding or (
        route.surface.identity.binding == route.bound.binding
    )
    assert route.surface.identity.work_mode == "work"
    assert metadata == before
    assert request.user_id == "routing-user"
    request.user_id = "forged-routing-user"
    again = bind("host-worker")
    assert again.bound.binding is route.bound.binding
    assert metadata == before


@pytest.mark.parametrize("field,value", [("user_id", "other-user"), ("channel_id", "other-channel")])
def test_trusted_subject_does_not_hide_persisted_surface_mismatch(admission, field, value):
    bind, manager, _, metadata = admission
    metadata[field] = value
    before = deepcopy(metadata)
    with pytest.raises(SurfaceAdmissionError, match="Surface creation identity changed"):
        bind("host-worker")
    assert metadata == before
    assert not manager.execution_bindings._bindings
    assert not manager._session_execution_bindings


@pytest.mark.parametrize("authority", ["live", "recovery", "live_without_archive"])
def test_trusted_subject_cannot_replace_existing_execution_scope(admission, authority):
    bind, manager, request, metadata = admission
    route = bind("host-worker")
    archive = route.recovery.path.read_bytes()
    if authority == "recovery":
        manager._session_execution_bindings.clear()
    elif authority == "live_without_archive":
        route.recovery.path.unlink()
    before = deepcopy(metadata)
    with pytest.raises(ExecutionRecoveryUnavailableError, match="Binding changed"):
        bind("other-host-worker")
    assert metadata == before
    assert request._execution_route is route
    assert len(manager.execution_bindings._bindings) == 1
    if authority == "live_without_archive":
        assert not route.recovery.path.exists()
    else:
        assert route.recovery.path.read_bytes() == archive


def test_no_trusted_subject_preserves_legacy_routing_binding_and_refuses_implicit_conversion(admission):
    bind, manager, _, metadata = admission
    before = deepcopy(metadata)
    route = bind()
    assert route.bound.binding.subject_id == "routing-user"
    assert bind().bound.binding is route.bound.binding
    manager._session_execution_bindings.clear()
    with pytest.raises(ExecutionRecoveryUnavailableError, match="Binding changed"):
        bind("host-worker")
    assert metadata == before
    assert len(manager.execution_bindings._bindings) == 1


def test_no_trusted_subject_still_rejects_routing_subject_change(admission):
    bind, _, request, _ = admission
    request.user_id = "other-routing-user"
    with pytest.raises(SurfaceAdmissionError, match="Surface subject changed"):
        bind()


@pytest.mark.asyncio
@pytest.mark.parametrize("stored_user", ["", "routing-user"])
async def test_prepare_chat_preserves_frozen_user_through_real_metadata_sync_and_binding(
    admission, tmp_path, monkeypatch, stored_user,
):
    from unittest.mock import AsyncMock

    from jiuwenswarm.common.schema.agent import AgentRequest
    from jiuwenswarm.common.schema.message import ReqMethod
    from jiuwenswarm.runtime.request import prepare_chat_turn
    from jiuwenswarm.server.runtime.session import lifecycle, session_metadata

    _, manager, _, metadata = admission
    sessions = tmp_path / "sessions"
    monkeypatch.setattr(session_metadata, "get_agent_sessions_dir", lambda: sessions)
    monkeypatch.setattr(lifecycle, "get_agent_sessions_dir", lambda: sessions)
    metadata["user_id"] = stored_user
    metadata["surface_creation"] = creation_surface(metadata)
    frozen_creation = deepcopy(metadata["surface_creation"])
    session_metadata.init_session_metadata(**metadata)
    manager.wait_for_session_prewarm = AsyncMock()
    allocated = object()
    routes = []

    async def get_agent_for_request(request, *, admit_request, on_admitted, **_kwargs):
        routes.append(on_admitted(admit_request()))
        return allocated

    manager.get_agent_for_request = get_agent_for_request
    request = AgentRequest(
        "trusted-first", session_id=metadata["session_id"], channel_id="web",
        req_method=ReqMethod.CHAT_SEND, user_id="forged-wire-user",
        params={"mode": metadata["mode"], "work_mode": "work", "project_dir": metadata["project_dir"]},
    )
    try:
        _, _, agent = await prepare_chat_turn(manager, request, "web", trusted_subject_id="host-worker")
        assert agent is allocated
        assert routes[-1].bound.binding.subject_id == "host-worker"
        assert routes[-1].trusted_subject_id == "host-worker"
        assert session_metadata.flush_pending_writes()
        stored = session_metadata.get_session_metadata(
            metadata["session_id"], cache_bust=True, enable_writeback=False, infer_defaults=False,
        )
        assert stored["user_id"] == stored_user
        assert stored["surface_creation"] == frozen_creation
        assert request.user_id == "forged-wire-user"
        request.user_id = "another-wire-user"
        await prepare_chat_turn(manager, request, "web", trusted_subject_id="host-worker")
        assert routes[1].bound.binding is routes[0].bound.binding
        with pytest.raises(ExecutionRecoveryUnavailableError, match="Binding changed"):
            await prepare_chat_turn(manager, request, "web", trusted_subject_id="other-host-worker")
    finally:
        assert session_metadata.flush_pending_writes()
        session_metadata.remove_session_metadata_cache(metadata["session_id"])


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["persisted-user-drift", "untrusted-wire-user"])
async def test_prepare_chat_rejects_identity_drift_before_metadata_sync(admission, monkeypatch, failure):
    from unittest.mock import AsyncMock, Mock

    from jiuwenswarm.common.schema.agent import AgentRequest
    from jiuwenswarm.common.schema.message import ReqMethod
    from jiuwenswarm.runtime.request import prepare_chat_turn

    _, manager, _, metadata = admission
    if failure == "persisted-user-drift":
        metadata["user_id"] = "changed-after-creation"
    before = deepcopy(metadata)
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.session_metadata.get_session_metadata",
        lambda *_args, **_kwargs: deepcopy(metadata),
    )
    manager.wait_for_session_prewarm = AsyncMock()
    sync = Mock()
    request = AgentRequest(
        "rejected", session_id=metadata["session_id"], channel_id="web", req_method=ReqMethod.CHAT_SEND,
        user_id="forged-wire-user", params={"mode": metadata["mode"], "work_mode": "work"},
    )
    with pytest.raises(SurfaceAdmissionError, match="Surface (creation identity|subject) changed"):
        await prepare_chat_turn(
            manager, request, "web", metadata_sync=sync,
            trusted_subject_id="host-worker" if failure == "persisted-user-drift" else None,
        )
    sync.assert_not_called()
    manager.wait_for_session_prewarm.assert_not_awaited()
    assert metadata == before
    assert not manager.execution_bindings._bindings


@pytest.mark.parametrize("subject", ["", "   "])
def test_empty_host_subject_cannot_fall_back_to_wire_identity(admission, subject):
    bind, manager, _, _ = admission
    with pytest.raises(ValueError, match="must not be empty"):
        bind(subject)
    assert not manager.execution_bindings._bindings


def test_wire_fields_cannot_mark_route_as_trusted(admission):
    bind, _, request, metadata = admission
    request.params["trusted_subject_id"] = "forged-host"
    metadata["trusted_subject_id"] = "forged-host"
    route = bind()
    assert route.trusted_subject_id is None
    assert route.bound.binding.subject_id == "routing-user"
