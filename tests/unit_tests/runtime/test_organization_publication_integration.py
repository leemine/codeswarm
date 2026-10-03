"""Runtime SDK owner checks and publication authority survive await boundaries."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests.unit_tests.server import test_organization_inventory as inventory_tests
from jiuwenswarm.runtime import AgentRuntime
from jiuwenswarm.runtime.session_catalog import SessionGetInput, SessionListInput
from jiuwenswarm.runtime.session_provisioner import SessionProvisionState, SessionProvisionCommitTiming

inventory = inventory_tests.inventory


@pytest.mark.asyncio
async def test_sdk_get_and_list_use_injected_identity_not_routing_actor(inventory):
    identity = inventory.principals['bob'].identity()
    runtime = AgentRuntime(agent_manager=SimpleNamespace(), initializer=AsyncMock(),
                           trusted_identity_resolver=lambda _: identity)
    await runtime.start()
    with pytest.raises(PermissionError):
        runtime.get_session(SessionGetInput(channel_id='web', session_id='alice-one'))
    assert runtime.get_session(SessionGetInput(channel_id='web', session_id='bob-one')).session_id == 'bob-one'
    page = runtime.list_sessions(SessionListInput(channel_id='web'))
    assert page.total == 1 and [s.session_id for s in page.sessions] == ['bob-one']


@pytest.mark.asyncio
async def test_prepared_provision_invalidates_before_delivery_and_commit(inventory):
    identity = inventory.principals['alice'].identity()
    runtime = AgentRuntime(agent_manager=SimpleNamespace(), initializer=AsyncMock(),
                           trusted_identity_resolver=lambda _: identity)
    await runtime.start()
    class Prepared:
        state = SessionProvisionState.PREPARED
        result = SimpleNamespace(session_id='alice-one')
    prepared = Prepared()
    result = await runtime._prepare_owned_provision(AsyncMock(return_value=prepared), SimpleNamespace())
    runtime.validate_session_provision_for_delivery(result)
    epoch = inventory.host.invalidate_source('alice-one', expected_epoch=inventory.host.source_epoch('alice-one'))
    with pytest.raises(PermissionError):
        runtime.validate_session_provision_for_delivery(result)
    commit = AsyncMock()
    runtime._session_provisioner.commit_session_provision = commit
    with pytest.raises(PermissionError):
        await runtime.commit_session_provision(result, timing=SessionProvisionCommitTiming.AFTER_RESULT_DELIVERY)
    commit.assert_not_awaited()
    inventory.host.activate_source('alice-one', inventory.project_id, expected_epoch=epoch)
    with pytest.raises(Exception, match='authority changed'):
        runtime.validate_session_provision_for_delivery(result)
