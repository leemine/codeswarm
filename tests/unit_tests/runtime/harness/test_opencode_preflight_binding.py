"""Owned OpenCode preflight transport, without launching an external CLI."""
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from openjiuwen.harness.engine import HarnessEngine
from openjiuwen.harness_protocol import AgentExecutionSpec, HarnessContext, HostCapability
from openjiuwen.harness_providers.opencode import OpenCodeHarness, OpenCodeHarnessConfig, OpenCodeModelConfig
from jiuwenswarm.common.runtime_workspace import RuntimeWorkspacePaths
from jiuwenswarm.runtime.harness.binding_store import ExecutionBindingStore
from jiuwenswarm.runtime.harness.config_source import ExecutionConfigSource
from jiuwenswarm.runtime.harness.execution_session import ExecutionSession, ExecutionExitState
from jiuwenswarm.runtime.harness.tool_gateway import ProductToolGateway, ProductToolScope


def fixture_session(root: Path, gateway=None):
    bound = ExecutionBindingStore().bind(
        ExecutionConfigSource(explicit=AgentExecutionSpec('opencode', 'r1')),
        subject_id='owner', host_session_id='parent', workspace=str(root),
    )
    harness = OpenCodeHarness(OpenCodeHarnessConfig(model=OpenCodeModelConfig('fixture', 'http://127.0.0.1:1')))
    session = ExecutionSession(HarnessEngine(bound.binding, harness),
                               RuntimeWorkspacePaths(root, root, root, root), tool_gateway=gateway)
    async def authorize(_operation):
        return True
    context = HarnessContext(agent_name='agent', agent_id='agent', host_session_id='parent',
                             system_prompt='', cwd=str(root), tool_authorizer=authorize,
                             host_capabilities=frozenset({HostCapability.TOOL_APPROVAL}))
    return session, harness, context


@pytest.mark.asyncio
@pytest.mark.parametrize('product', [False, True])
async def test_governed_session_binds_private_endpoint_before_start_and_releases(tmp_path, product):
    tool = SimpleNamespace(card=SimpleNamespace(name='echo', description='fixture', input_params={}))
    gateway = ProductToolGateway([tool], scope=ProductToolScope('owner', 'parent', str(tmp_path))) if product else None
    session, harness, context = fixture_session(tmp_path, gateway)
    try:
        prepared = await session._prepare_tool_context(context)
        endpoint = harness._preflight.endpoint
        assert endpoint.product_tool_names == (('echo',) if product else ())
        assert prepared.tool_authorizer is context.tool_authorizer
        assert endpoint.admits_servers(prepared.mcp_servers)
        assert (HostCapability.MCP_SERVERS in prepared.host_capabilities) is product
        assert not session.owns_governed_provider_session('unstarted')
        async with httpx.AsyncClient() as client:
            response = await client.post(endpoint.url, headers={'Authorization': 'Bearer ' + endpoint.token},
                                         json={'generation': endpoint.generation, 'nonce': 'not-an-active-turn'})
            assert response.status_code == 200
            assert response.json()['allowed'] is False
            if not product:
                response = await client.post(endpoint.url.replace('/native-preflight', '/mcp'),
                                             headers={'Authorization': 'Bearer ' + endpoint.token}, json={})
                assert response.status_code == 401
                with pytest.raises(RuntimeError, match='no product'):
                    session._tool_transport.server_config()
        # Exact private Provider ID plus confirmed running transport is required.
        harness._session_id = 'native-owned'
        session._started = True
        session._exit_state = ExecutionExitState.RUNNING
        assert session.owns_governed_provider_session('native-owned')
        assert not session.owns_governed_provider_session('parent')
    finally:
        await session.stop()
    assert session._tool_transport.exit_confirmed
    assert not session.owns_governed_provider_session('native-owned')


@pytest.mark.asyncio
async def test_unbound_tool_source_rejected_before_listener(tmp_path):
    from dataclasses import replace
    session, _, context = fixture_session(tmp_path)
    with pytest.raises(ValueError, match='unbound tool sources'):
        await session._prepare_tool_context(replace(context, tools=object()))
    assert session._tool_transport is None
