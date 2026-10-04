"""Cold UI read followed by actual Runtime/AgentManager factory admission."""
import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.governance.tool_context import tool_authority_scope
from jiuwenswarm.runtime.session.model import SessionWorkKind
from jiuwenswarm.server.runtime.agent_manager import AgentManager
from tests.unit_tests.runtime import test_continuation_transaction as transactions
from tests.unit_tests.runtime.test_opencode_continuation import add_profile
from tests.unit_tests.server.test_surface_capabilities_get import _call, _request

setup = transactions.setup
transaction = transactions.transaction


@pytest.mark.asyncio
async def test_cold_read_then_real_runtime_agent_manager_gateway_factory(transaction, monkeypatch):
    tx = transaction
    add_profile(tx.catalog)
    tx.request = replace(tx.request, execution_profile_id='opencode')
    result = await transactions.create(tx)
    # Restore the real cache, facade and factory. Only Provider I/O is absent;
    # neither the returned owner nor its model gateway is manually assembled.
    AgentManager.__init__(tx.manager)
    tx.manager.cancel_all_inflight_work = AsyncMock()
    tx.manager.cleanup = AsyncMock()
    response = await _call(_request(result.session_id), tx.manager)
    assert response.ok and response.payload['surface_capabilities']['provider_id'] == 'opencode'
    assert tx.manager.agents == {}
    assert tx.manager._session_execution_bindings == {}

    request = AgentRequest('first-chat', channel_id='web', session_id=result.session_id,
        req_method=ReqMethod.CHAT_SEND, params={'project_id': tx.setup.target.project_id,
            'model_name': 'synthetic#0', 'mode': 'agent.work.normal'})
    async def operation():
        bundle = tx.runtime._resource_authorizers_for(request)
        with tool_authority_scope(None, provider_authorizers=bundle):
            _, _, agent = await tx.runtime._prepare_chat_turn(request, 'web')
            adapter = agent._adapter
            assert adapter._model_gateway_binding is not None
            authority = adapter._capture_model_authority()
            assert authority.credential_authority.execution.identity == transactions.BOB
            assert authority.is_current() is True
            return agent
    agent = await tx.runtime._session_coordinator.run_unary(result.session_id, request.request_id,
        SessionWorkKind.CHAT_STREAM, operation)
    again = await _call(_request(result.session_id), tx.manager)
    assert again.payload['surface_capabilities'] == agent._adapter.ui_capability_manifest.record()
    assert response.payload == again.payload
    assert len(tx.manager.agents['web']) == 1
    await agent.cleanup()
    await asyncio.wait_for(tx.runtime.close(), timeout=5)
