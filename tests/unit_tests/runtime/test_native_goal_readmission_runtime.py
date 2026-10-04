"""Real auth/store/Runtime factory/Native Goal; synthetic allocation and model/tool IO."""
import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from openjiuwen.harness.goal.schema import GoalRecord
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.governance.organization_auth import authenticated_scope, current_identity
from jiuwenswarm.governance.tool_context import tool_authority_scope
from jiuwenswarm.governance.preparation import GovernanceError
from jiuwenswarm.runtime import AgentRuntime
from jiuwenswarm.runtime.context import set_runtime_context, reset_runtime_context
from jiuwenswarm.runtime.session import SessionWorkKind
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
    f.state.runtime = runtime
    coordinator = runtime._session_coordinator
    await coordinator.register_session(f.sid, 'web')
    record = GoalRecord.create(session_id=f.sid, objective='saved', max_attempts=3)
    f.c.outer.goal_manager._store.save(record)
    return SimpleNamespace(**locals())


async def execute(x, *, action='attach', principal=None, before=None):
    f, r = x.f, x.runtime
    rid = 'goal-' + action
    request = AgentRequest(rid, session_id=f.sid, channel_id='web', is_stream=True,
        req_method=ReqMethod.COMMAND_GOAL if action == 'resume' else ReqMethod.CHAT_SEND,
        params={'project_id': f.project.project_id, 'action': action, 'attach_goal': action == 'attach'})
    saved = {}
    async def operation():
        bundle = r._resource_authorizers_for(request)
        # Keep real production native_lifecycle_factory; model/tool decisions
        # and SDK HTTP peer remain the original bounded synthetic fixtures.
        resources = replace(f.c.bundle, native_lifecycle_factory=bundle.native_lifecycle_factory)
        token = set_runtime_context(r, f.manager)
        try:
            if before:
                before(request)
            with tool_authority_scope(None, provider_authorizers=resources):
                if action == 'resume':
                    receipt, result = await f.child._submit_native_goal_request(request, {'query': 'resume'}, action='resume')
                else:
                    receipt = await f.child._attach_native_goal_request(request, {'query': 'attach'})
                    owner = x.coordinator.native_execution_owner(f.sid, rid)
                    result = owner._native_admission.owned_turn._entry.result
            payload = await asyncio.wait_for(result, 4)
            owner = x.coordinator.native_execution_owner(f.sid, rid)
            saved.update(owner=owner, admission=owner._native_admission, receipt=receipt, payload=payload)
            await asyncio.wait_for(owner._native_admission.confirmed.wait(), 4)
        finally:
            reset_runtime_context(token)
    with authenticated_scope(principal or f.bob):
        task = asyncio.create_task(x.coordinator.run_unary(f.sid, rid,
            SessionWorkKind.GOAL_STREAM if action == 'resume' else SessionWorkKind.GOAL_ATTACH, operation))
        await asyncio.wait_for(task, 6)
    await asyncio.sleep(0)
    return SimpleNamespace(**saved)


@pytest.mark.asyncio
async def test_actual_runtime_cold_attach_has_new_owned_native_producer(case):
    first = await execute(case)
    assert first.owner._execution_authority is case.f.bob
    assert first.admission.source.host_value is first.owner
    assert first.admission.owned_turn._pending._origin.host_value is first.owner
    assert first.owner.state.terminal and first.admission.producer.done()
    assert first.payload['goal']['goal_id'] == case.record.goal_id
    assert case.f.c.side_effects == ['goal'] and case.f.c.error is None


@pytest.mark.asyncio
@pytest.mark.parametrize("expire_old", [False, True])
async def test_actual_runtime_resume_reuses_exit_facts_after_registry_eviction(case, expire_old):
    x, f = case, case.f
    f.c.attempts = 2
    async def pause_first(_):
        if not f.c.side_effects:
            await f.c.outer.goal_manager.pause()
    f.c.before = pause_first
    first = await execute(x)
    assert f.c.outer.goal_manager.peek().status.value == 'paused'
    x.coordinator._registry._remove(first.owner)
    assert x.coordinator._registry.get(first.owner.execution_id) is None
    principal = f.bob
    if expire_old:
        import hashlib
        import json
        import secrets
        import time
        token = secrets.token_urlsafe(32)
        config = json.loads(f.authpath.read_text())
        config['credentials'].append({'actor_id': 'bob', 'sha256': hashlib.sha256(token.encode()).hexdigest(),
            'expires_at': time.time() + 600, 'revoked': False})
        f.authpath.write_text(json.dumps(config))
        principal = f.auth.principal({'Authorization': 'Bearer ' + token})
        f.auth.revoke(f.bob)
    second = await execute(x, action='resume', principal=principal)
    assert second.owner._execution_authority is principal
    assert first.owner._execution_authority is f.bob
    assert second.owner is not first.owner and second.admission.source is not first.admission.source
    assert second.admission.owned_turn._pending is not first.admission.owned_turn._pending
    assert f.c.side_effects == ['goal', 'goal'] and f.c.error is None


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['principal', 'generation', 'record', 'binding', 'child', 'session'])
async def test_first_identity_callback_cannot_replace_captured_target(case, change):
    x, f = case, case.f
    original = x.runtime._trusted_identity_resolver
    restored = []
    def before(req):
        first = True
        def resolver(value):
            nonlocal first
            if first:
                first = False
                owner = x.coordinator.native_execution_owner(f.sid, req.request_id)
                if change == 'principal':
                    old = owner._execution_authority
                    owner._execution_authority = f.alice
                    restored.append(lambda: setattr(owner, '_execution_authority', old))
                elif change == 'generation':
                    old = owner.generation
                    owner.generation += 1
                    restored.append(lambda: setattr(owner, 'generation', old))
                elif change == 'record':
                    old = x.coordinator._sessions[f.sid]
                    x.coordinator._sessions[f.sid] = SimpleNamespace(generation=old.generation)
                    restored.append(lambda: x.coordinator._sessions.__setitem__(f.sid, old))
                elif change == 'binding':
                    old = f.c.native.engine
                    f.c.native.engine = replace(old, binding=replace(old.binding))
                    restored.append(lambda: setattr(f.c.native, 'engine', old))
                elif change == 'child':
                    f.root._session_adapters[f.sid] = object()
                    restored.append(lambda: f.root._session_adapters.__setitem__(f.sid, f.child))
                else:
                    old = f.c.outer.goal_manager._store._session
                    f.c.outer.goal_manager._store._session = object()
                    restored.append(lambda: setattr(f.c.outer.goal_manager._store, '_session', old))
            return original(value)
        x.runtime._trusted_identity_resolver = resolver
    try:
        with pytest.raises(GovernanceError):
            await execute(x, before=before)
        assert not f.c.side_effects and not f.c.native._requests
    finally:
        x.runtime._trusted_identity_resolver = original
        for restore in reversed(restored):
            restore()
