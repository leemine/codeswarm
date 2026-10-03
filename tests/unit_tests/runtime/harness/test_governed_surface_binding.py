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
