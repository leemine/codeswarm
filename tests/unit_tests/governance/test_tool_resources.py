from types import SimpleNamespace

import pytest
from openjiuwen.harness_protocol import BeforeToolContext

from jiuwenswarm.governance.contracts import TrustedIdentity
from jiuwenswarm.governance.resources import ResourceDecision, ResourceRequest
from jiuwenswarm.governance.tool_resources import (
    BoundToolResourceAuthority, ResourceExecutionContext, ToolResourceUse,
)


@pytest.fixture
def policy():
    identity = TrustedIdentity('alice', 'worker', 'organization')
    state = SimpleNamespace(identity=identity, current=True, allowed=True, reference='/workspace', calls=[])
    def authorize(project_id, actor, request):
        state.calls.append(request)
        return ResourceDecision(state.allowed, project_id, actor.actor_id, actor.subject_id,
                                request, 1, 1, reference=state.reference, scope='/workspace')
    use = ToolResourceUse(ResourceRequest('workspace-id', 'read', '/workspace/file'), '/workspace')
    resolver = SimpleNamespace(resources_for_tool=lambda execution, tool: (use,))
    bound = BoundToolResourceAuthority(
        ResourceExecutionContext('project', identity, 'private-session', '/workspace', 'native'),
        authorizer=SimpleNamespace(authorize_resource=authorize), resolver=resolver,
        current_identity=lambda: state.identity, is_current_execution=lambda: state.current,
    )
    tool = BeforeToolContext('agent', 'provider-session', 'turn', 'call', 'read_file', {'path': '/workspace/file'})
    return bound, state, resolver, tool


@pytest.mark.asyncio
async def test_current_resource_and_reference_rechecked_each_operation(policy):
    bound, state, _, tool = policy
    assert await bound(tool) is True
    state.allowed = False
    assert await bound(tool) is False
    state.allowed = True
    state.reference = '/another-resource'
    assert await bound(tool) is False
    assert len(state.calls) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['identity', 'authority', 'subject', 'expired', 'generation'])
async def test_live_identity_and_generation_bound_before_authorizer(policy, change):
    bound, state, _, tool = policy
    state.identity = {
        'identity': TrustedIdentity('bob', 'worker', 'organization'),
        'authority': TrustedIdentity('alice', 'worker', 'different'),
        'subject': TrustedIdentity('alice', 'other-worker', 'organization'),
        'expired': None, 'generation': state.identity,
    }[change]
    state.current = change != 'generation'
    assert await bound(tool) is False
    assert not state.calls


@pytest.mark.asyncio
@pytest.mark.parametrize('uses', [(), [], None, (True,), (ResourceRequest('x', 'invoke'),)])
async def test_unknown_or_client_shaped_mapping_cannot_allow(policy, uses):
    bound, state, resolver, tool = policy
    resolver.resources_for_tool = lambda *_: uses
    assert await bound(tool) is False
    assert not state.calls


@pytest.mark.asyncio
async def test_mapping_error_and_mid_resolution_revoke_fail_closed(policy):
    bound, state, resolver, tool = policy
    def unavailable(*_):
        raise OSError('authority unavailable')
    resolver.resources_for_tool = unavailable
    assert await bound(tool) is False
    use = ToolResourceUse(ResourceRequest('workspace-id', 'read', '/workspace/file'), '/workspace')
    def revoke(*_):
        state.identity = None
        return (use,)
    resolver.resources_for_tool = revoke
    assert await bound(tool) is False


@pytest.mark.asyncio
async def test_resource_mapping_is_rechecked_after_policy(policy):
    bound, state, resolver, tool = policy
    original = resolver.resources_for_tool
    calls = []
    def changing(execution, operation):
        calls.append(True)
        if len(calls) == 1:
            return original(execution, operation)
        return (ToolResourceUse(ResourceRequest('another-id', 'read', '/workspace/file'), '/workspace'),)
    resolver.resources_for_tool = changing
    assert await bound(tool) is False
    assert len(state.calls) == 1 and len(calls) == 2


@pytest.mark.asyncio
async def test_identity_changed_by_final_mapping_cannot_pass(policy):
    bound, state, resolver, tool = policy
    original = resolver.resources_for_tool
    calls = []
    def changing(execution, operation):
        calls.append(True)
        if len(calls) == 2:
            state.identity = None
        return original(execution, operation)
    resolver.resources_for_tool = changing
    assert await bound(tool) is False
    assert len(state.calls) == 1 and len(calls) == 2
