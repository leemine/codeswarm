"""Actual AgentServer registration retains the resource mutation's Runtime.

The sidecar, authenticated adapter, registry and Coordinator are real. Server
network startup and the final Provider release are deliberately not exercised.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from jiuwenswarm.governance.organization_auth import current_principal
from jiuwenswarm.runtime.session.coordinator import RuntimeSessionCoordinator
from jiuwenswarm.runtime.session.model import RuntimeSessionState
from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer
from jiuwenswarm.server.runtime.gateway_adapter.base import AdapterRegistry
from jiuwenswarm.server.runtime.gateway_adapter import project_resource_adapter as implementation
from jiuwenswarm.server.runtime.session.sharing_host import SharingHostService
from tests.unit_tests.server import test_project_resource_delegation as resource_tests

case = resource_tests.case
grants = resource_tests.grants


@pytest.fixture
def wiring(case):
    coordinator = RuntimeSessionCoordinator(cancel_timeout=0.2)
    manager = object()
    runtime = SimpleNamespace(agent_manager=manager, _session_coordinator=coordinator)
    server = AgentWebSocketServer.__new__(AgentWebSocketServer)
    server._runtime = runtime
    server._agent_manager = manager
    server._adapter_registry = AdapterRegistry()
    server._trusted_identity_resolver = lambda _: current_principal().identity()
    server._organization_session_host = SharingHostService(
        case.auth.resolve_actor, known_actor=case.auth.known_actor, storage=case.store)
    # Existing assembly must never lazily select/create a replacement Runtime
    # to claim that the original execution was drained.
    server._execution_runtime = Mock(side_effect=AssertionError('unexpected lazy Runtime lookup'))
    server._install_sharing_adapters()
    adapter = server._adapter_registry.get('project.resources.grant')
    assert type(adapter) is implementation.ProjectResourceAdapter
    assert server._adapter_registry.get('project.resources.revoke') is adapter
    replacement = RuntimeSessionCoordinator(cancel_timeout=0.2)

    def drift(kind):
        if kind == 'runtime':
            server._runtime = SimpleNamespace(agent_manager=manager, _session_coordinator=replacement)
        elif kind == 'manager':
            server._agent_manager = object()
        elif kind == 'coordinator':
            runtime._session_coordinator = replacement
        else:
            raise AssertionError(kind)

    return SimpleNamespace(case=case, server=server, runtime=runtime, coordinator=coordinator,
                           adapter=adapter, replacement=replacement, drift=drift)


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['runtime', 'manager', 'coordinator'])
async def test_registered_resource_adapter_denies_composition_drift_before_commit(wiring, kind):
    w = wiring
    before = w.case.store.path.read_bytes()
    with w.case.actor('alice'):
        request = w.case.request('grant')
        w.drift(kind)
        response = await w.adapter.handle(request)
    assert response.ok is False
    assert response.payload['code'] == 'FORBIDDEN'
    assert w.case.store.path.read_bytes() == before
    assert 'bob' not in grants(w.case)
    w.server._execution_runtime.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', [None, 'runtime', 'manager', 'coordinator'])
async def test_committed_resource_change_drains_original_coordinator_not_replacement(wiring, monkeypatch, kind):
    w = wiring
    original = await w.coordinator.register_session('original-active', 'web')
    record = w.coordinator._sessions[original.session_id]
    release_calls = []
    baseline = w.case.store._load()['projects'][w.case.pid]['resource_access']['revision']

    class OriginalAuthority:
        scope = object()

        def check_owner(self):
            assert w.coordinator._sessions[original.session_id] is record

        def check_authority(self):
            self.check_owner()
            revision = w.case.store._load()['projects'][w.case.pid]['resource_access']['revision']
            if revision != baseline:
                raise PermissionError('original authority revision changed')

        async def release(self):
            self.check_owner()
            release_calls.append(record)

    w.coordinator.watch_session_authority(original.session_id, generation=original.generation,
                                         authority=OriginalAuthority(), interval=3600)
    await asyncio.sleep(0)  # Park the real monitor before the explicit mutation.
    # A replacement contains unrelated owned state which the callback must not
    # select or alter. A successful scan of its empty registry is not a receipt.
    new = await w.replacement.register_session('replacement-session', 'web')
    new_record = w.replacement._sessions[new.session_id]
    original_io = implementation.run_history_io

    async def after_actual_commit(function, *args, **kwargs):
        result = await original_io(function, *args, **kwargs)
        if function is implementation.mutate_delegation:
            assert 'bob' in grants(w.case)  # Actual persisted write, no fake result.
            if kind is not None:
                w.drift(kind)
        return result

    monkeypatch.setattr(implementation, 'run_history_io', after_actual_commit)
    try:
        with w.case.actor('alice'):
            response = await w.adapter.handle(w.case.request('grant'))
            if kind is None:
                assert response.ok is True
                response._delivery_guard()
            else:
                assert response.ok is False
                assert response.payload['code'] == 'MUTATION_OUTCOME_UNKNOWN'
        assert 'bob' in grants(w.case)
        assert release_calls == [record]
        assert record.state is RuntimeSessionState.CLOSED
        assert new_record.state is RuntimeSessionState.READY
        assert new_record.resource_close_task is None
        w.server._execution_runtime.assert_not_called()
    finally:
        record.authority_task.cancel()
        await asyncio.gather(record.authority_task, return_exceptions=True)


def test_resource_adapter_not_registered_without_organization_host():
    server = AgentWebSocketServer.__new__(AgentWebSocketServer)
    server._organization_session_host = None
    server._adapter_registry = AdapterRegistry()
    server._install_sharing_adapters()
    assert server._adapter_registry.get('project.resources.grant') is None
