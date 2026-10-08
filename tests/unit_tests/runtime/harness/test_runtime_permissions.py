# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Only proven authorization deltas may retain an existing Session identity."""

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest
from openjiuwen.harness.engine.config import config_fingerprint
from openjiuwen.harness_protocol import (
    AgentExecutionSpec,
    ExecutionAuthorization,
    HarnessCapability,
    HarnessCard,
)

from jiuwenswarm.runtime.harness.binding_store import ExecutionBindingStore
from jiuwenswarm.runtime.harness.config_source import (
    ExecutionConfigSource,
    source_for_bound_fingerprint,
)
from tests.unit_tests.runtime.harness.test_external_execution_route import _route


@pytest.mark.parametrize("full", [False, True])
def test_only_explicit_authorization_delta_reconstructs_exact_binding(full):
    old = AgentExecutionSpec(
        "opencode", "r1", provider_config={"model": {"model": "fixed"}}, authorization=ExecutionAuthorization(full)
    )
    fingerprint = config_fingerprint(old)
    desired = replace(old, authorization=ExecutionAuthorization(not full))
    restored = source_for_bound_fingerprint(ExecutionConfigSource(explicit=desired), fingerprint)
    assert restored.resolve() == old
    with pytest.raises(ValueError, match="fingerprint"):
        source_for_bound_fingerprint(
            ExecutionConfigSource(explicit=desired), fingerprint, allow_runtime_authorization=False
        )
    for drift in (
        replace(desired, provider_id="codex"),
        replace(desired, config_revision="r2"),
        replace(desired, provider_config={"model": {"model": "changed"}}),
        replace(desired, requested_mode="changed"),
        replace(desired, authorization=None),
    ):
        with pytest.raises(ValueError, match="fingerprint"):
            source_for_bound_fingerprint(ExecutionConfigSource(explicit=drift), fingerprint)
    legacy = replace(old, authorization=None)
    with pytest.raises(ValueError, match="fingerprint"):
        source_for_bound_fingerprint(ExecutionConfigSource(explicit=desired), config_fingerprint(legacy))


@pytest.mark.asyncio
async def test_latest_desired_revision_wins_without_rebinding_or_false_ack(tmp_path):
    from jiuwenswarm.server.runtime.agent_adapter.engine_adapter import (
        EngineAgentAdapter,
    )

    route = _route(tmp_path, provider_id="opencode")
    source = ExecutionConfigSource(explicit=replace(route.bound.spec, authorization=ExecutionAuthorization(False)))
    bindings = ExecutionBindingStore()
    bound = bindings.bind(
        source, subject_id="alice", host_session_id="session-1", workspace=route.bound.binding.workspace
    )
    route = replace(route, source=source, bound=bound, bindings=bindings)
    adapter = EngineAgentAdapter(route)
    gate = asyncio.Event()
    calls = []

    async def update(authorization, *, runtime_policy=None):
        calls.append(authorization.full_access)
        await gate.wait()

    session = SimpleNamespace(
        started=True,
        closed=False,
        update_authorization=update,
        engine=SimpleNamespace(
            harness=SimpleNamespace(
                card=HarnessCard(
                    name="test",
                    implementation_version="1",
                    capabilities=frozenset({HarnessCapability.RUNTIME_AUTHORIZATION}),
                )
            )
        ),
    )
    adapter._session = session
    # A profile with explicit authorization still works without a host override.
    gate.set()
    adapter._request_runtime_permissions({})
    await asyncio.wait_for(adapter._permission_task, 2)
    assert adapter._permission_effective == ExecutionAuthorization(False)
    calls.clear()
    gate.clear()
    adapter._request_runtime_permissions({"permissions": {"enabled": False}})
    await asyncio.sleep(0)
    assert adapter.runtime_permission_status["state"] == "pending"
    adapter._request_runtime_permissions({"permissions": {"enabled": True}})
    assert adapter.runtime_permission_status["state"] == "pending"
    gate.set()
    await asyncio.wait_for(adapter._permission_task, 2)
    assert calls == [True, False]
    assert adapter.runtime_permission_status["effective_full_access"] is False
    assert adapter.runtime_permission_status["state"] == "applied"
    assert adapter.route.bound is bound


@pytest.mark.asyncio
async def test_failed_native_confirmation_is_exposed_and_admission_remains_blocked(tmp_path, monkeypatch):
    from jiuwenswarm.server.runtime.agent_adapter.engine_adapter import (
        EngineAgentAdapter,
    )

    route = _route(tmp_path)
    # Same exact immutable route; this test exercises the controller rather than construction.
    adapter = EngineAgentAdapter(route)

    async def fail(*args, **kwargs):
        raise RuntimeError("native mismatch")

    session = SimpleNamespace(started=True, closed=False, update_authorization=fail)
    adapter._session = session
    await adapter._apply_runtime_permissions(session)
    assert adapter.runtime_permission_status is None  # Legacy profile has no new status contract.
    assert adapter._permission_error
    assert adapter._permission_effective is None
    assert session._authorization_unconfirmed


@pytest.mark.asyncio
async def test_child_permission_update_marks_all_live_children_before_waiting():
    from jiuwenswarm.runtime.harness.external_subagent import (
        ExternalSubagentExecutionFactory,
    )

    factory = ExternalSubagentExecutionFactory.__new__(ExternalSubagentExecutionFactory)
    factory._lock = asyncio.Lock()
    first, second, closed = [SimpleNamespace(_authorization_unconfirmed=False) for _ in range(3)]
    factory._live = {
        str(index): SimpleNamespace(_session=session, closed=index == 2)
        for index, session in enumerate((first, second, closed))
    }
    calls = []

    async def update(session, authorization):
        assert first._authorization_unconfirmed and second._authorization_unconfirmed
        assert authorization.full_access is False
        calls.append(session)
        if session is second:
            raise RuntimeError("child confirmation failed")

    factory._update_child_authorization = update
    with pytest.raises(RuntimeError, match="child runtime"):
        await factory.update_authorization(ExecutionAuthorization(False))
    assert calls == [first, second]
    assert not closed._authorization_unconfirmed
    assert factory._runtime_authorization == ExecutionAuthorization(False)
