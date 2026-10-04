"""Actual Runtime readmission consumer regression from independent review."""
import asyncio
from dataclasses import replace

import pytest
from openjiuwen.core.controller.schema.execution_origin import ExecutionOrigin
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.governance.organization_auth import authenticated_scope
from jiuwenswarm.governance.tool_context import tool_authority_scope
from jiuwenswarm.runtime.native_goal_readmission import submit_readmission
from jiuwenswarm.runtime.session import SessionWorkKind
from tests.unit_tests.runtime import test_native_goal_readmission_runtime as base

credentials = base.credentials
goal_case = base.goal_case
native_case = base.native_case
full_case = base.full_case
case = base.case


@pytest.mark.asyncio
async def test_first_auth_callback_cannot_change_captured_inputs(case):
    x, f = case, case.f
    request = AgentRequest('snapshot', session_id=f.sid, channel_id='web', is_stream=True,
        req_method=ReqMethod.CHAT_SEND,
        params={'project_id': f.project.project_id, 'attach_goal': True})
    inputs = {'query': 'original', 'nested': {'value': 'original'}}
    original = x.runtime._trusted_identity_resolver
    seen = []

    async def operation():
        bundle = x.runtime._resource_authorizers_for(request)
        resources = replace(f.c.bundle, native_lifecycle_factory=bundle.native_lifecycle_factory)
        first = True
        def resolver(req):
            nonlocal first
            if first:
                first = False
                inputs['query'] = 'changed'
                inputs['nested']['value'] = 'changed'
            return original(req)
        x.runtime._trusted_identity_resolver = resolver
        with tool_authority_scope(None, provider_authorizers=resources):
            receipt, result = await submit_readmission(x.runtime, request, f.child, inputs, action='attach')
        owner = x.coordinator.native_execution_owner(f.sid, 'snapshot')
        admitted = owner._native_admission.owned_turn._entry.request.inputs
        seen.append((admitted['query'], admitted['nested']['value']))
        await asyncio.wait_for(result, 4)
        await asyncio.wait_for(owner._native_admission.confirmed.wait(), 4)
    try:
        with authenticated_scope(f.bob):
            await asyncio.wait_for(asyncio.create_task(x.coordinator.run_unary(
                f.sid, 'snapshot', SessionWorkKind.GOAL_ATTACH, operation)), 6)
        assert seen == [('original', 'original')]
        assert inputs['query'] == 'changed'
        assert f.c.side_effects == ['goal']
    finally:
        x.runtime._trusted_identity_resolver = original


@pytest.mark.asyncio
async def test_same_host_value_cannot_replace_old_slot_origin(case):
    x, f = case, case.f
    f.c.attempts = 2
    async def pause_first(_):
        if not f.c.side_effects:
            await f.c.outer.goal_manager.pause()
    f.c.before = pause_first
    first = await base.execute(x)
    manager = f.c.outer.goal_manager
    slot = manager._execution_origin
    original = x.runtime._trusted_identity_resolver
    def before(_request):
        once = True
        def resolver(req):
            nonlocal once
            if once:
                once = False
                manager._execution_origin = (*slot[:3], ExecutionOrigin(slot[3].host_value))
            return original(req)
        x.runtime._trusted_identity_resolver = resolver
    try:
        from openjiuwen.harness_protocol import HarnessStateError
        with pytest.raises(HarnessStateError):
            await base.execute(x, action='resume', before=before)
        # Original producer done callback settles deferred terminal state.
        await asyncio.sleep(0)
        from openjiuwen.harness_protocol import TurnEventKind
        owner, = x.coordinator._registry.select(session_id=f.sid, request_id='goal-resume')
        admission = owner._native_admission
        entry = admission.owned_turn._entry
        assert admission.confirmed.is_set() and admission.producer.done()
        assert owner.state.value == 'failed'
        assert entry.terminal_kind is TurnEventKind.FAILED
        assert entry.terminal_event.is_set() and isinstance(entry.result.exception(), HarnessStateError)
        assert f.c.side_effects == ['goal']
        assert first.admission.owned_turn._pending._origin is slot[3]
    finally:
        x.runtime._trusted_identity_resolver = original
        manager._execution_origin = slot


@pytest.mark.asyncio
async def test_old_admission_coordinator_cannot_be_retargeted(case):
    x, f = case, case.f
    f.c.attempts = 2
    async def pause_first(_):
        if not f.c.side_effects:
            await f.c.outer.goal_manager.pause()
    f.c.before = pause_first
    first = await base.execute(x)
    old = first.admission.coordinator
    first.admission.coordinator = object()
    try:
        from jiuwenswarm.governance.preparation import GovernanceError
        with pytest.raises((GovernanceError, PermissionError, RuntimeError)):
            await base.execute(x, action='resume')
        assert f.c.side_effects == ['goal']
    finally:
        first.admission.coordinator = old
