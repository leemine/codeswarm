"""Organization public Goal readmission remains outside ordinary Single release.

Real auth, sidecar, Runtime and Native Goal fixtures are retained. These tests
assert rejection before admission, not post-admission CAS coverage. The original
core/Native host readmission positive tests remain in their independent suites.
"""
import asyncio
import hashlib
import json
import secrets
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from openjiuwen.harness.goal.schema import GoalRecord, GoalStatus
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.governance.organization_auth import authenticated_scope, current_identity
from jiuwenswarm.runtime import AgentRuntime
from tests.unit_tests.runtime import test_native_runtime_source as runtime_fixtures
from tests.unit_tests.runtime.harness import test_native_goal_source as goal_fixtures

credentials = runtime_fixtures.credentials
goal_case = goal_fixtures.goal_case
full_case = runtime_fixtures.full_case


@pytest.fixture
async def native_case(goal_case):
    goal_case.agent = goal_case.outer
    return goal_case


@pytest.fixture
async def case(full_case):
    f = full_case
    runtime = AgentRuntime(agent_manager=f.manager, initializer=AsyncMock(),
        trusted_identity_resolver=lambda _: current_identity(), project_authorizer=f.access,
        resource_authorizer=f.access, organization_session_host=f.host)
    runtime._started = True
    f.state.runtime = runtime
    coordinator = runtime._session_coordinator
    await coordinator.register_session(f.sid, 'web')
    record = GoalRecord.create(session_id=f.sid, objective='saved', max_attempts=3)
    f.c.outer.goal_manager._store.save(record)
    return SimpleNamespace(**locals())


async def reject(x, *, action, principal=None):
    f = x.f
    request = AgentRequest('goal-' + action, session_id=f.sid, channel_id='web', is_stream=True,
        req_method=ReqMethod.COMMAND_GOAL if action == 'resume' else ReqMethod.CHAT_SEND,
        params={'project_id': f.project.project_id, 'action': action, 'attach_goal': action == 'attach'})
    before = f.c.outer.goal_manager.peek().to_dict()
    with authenticated_scope(principal or f.bob), pytest.raises(PermissionError, match='not released'):
        async with asyncio.timeout(3):
            _ = [event async for event in x.runtime.stream(request)]
    assert f.c.outer.goal_manager.peek().to_dict() == before
    assert not x.coordinator._registry.select(session_id=f.sid)
    assert not f.c.native._requests and f.c.native._native.active_turn is None
    assert not f.c.side_effects and not f.c.http


async def test_organization_cold_attach_rejected_before_new_owned_producer(case):
    await reject(case, action='attach')


@pytest.mark.parametrize('expire_old', [False, True])
async def test_organization_idle_resume_is_not_enabled_by_new_credential(case, expire_old):
    x, f = case, case.f
    case.record.status = GoalStatus.PAUSED
    f.c.outer.goal_manager._store.save(case.record)
    principal = f.bob
    if expire_old:
        token = secrets.token_urlsafe(32)
        config = json.loads(f.authpath.read_text())
        config['credentials'].append({'actor_id': 'bob', 'sha256': hashlib.sha256(token.encode()).hexdigest(),
            'expires_at': time.time() + 600, 'revoked': False})
        f.authpath.write_text(json.dumps(config))
        principal = f.auth.principal({'Authorization': 'Bearer ' + token})
        f.auth.revoke(f.bob)
    await reject(x, action='resume', principal=principal)


@pytest.mark.parametrize('change', ['principal', 'generation', 'record', 'binding', 'child', 'session'])
async def test_organization_scope_rejects_before_readmission_retarget_callback(case, change):
    # These used to probe post-admission capture/CAS. Scope now rejects before
    # the callback; do not count their passes as revalidation of that CAS layer.
    # The six target cases remain enumerated to document the deferred coverage.
    called = []
    original = case.runtime._trusted_identity_resolver
    def resolver(value):
        called.append(change)
        raise AssertionError('unreleased readmission reached identity callback')
    case.runtime._trusted_identity_resolver = resolver
    try:
        await reject(case, action='attach')
        assert called == []
    finally:
        case.runtime._trusted_identity_resolver = original
