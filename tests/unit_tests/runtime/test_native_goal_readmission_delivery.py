"""Organization readmission rejects before deferred delivery/CAS consumers.

The previous admitted Goal examples belong to the full Goal follow-up scope.
These cases do not claim to retest post-admission input freezing, source-slot
replacement or exit reconciliation. Core/Native harness positive tests remain.
"""
from copy import deepcopy

import pytest
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.governance.organization_auth import authenticated_scope
from tests.unit_tests.runtime import test_native_goal_readmission_runtime as base

credentials = base.credentials
goal_case = base.goal_case
native_case = base.native_case
full_case = base.full_case
case = base.case


async def reject_before_admission(case, *, action, callback):
    x, f = case, case.f
    original = x.runtime._trusted_identity_resolver
    called = []
    def resolver(request):
        called.append(True)
        callback()
        return original(request)
    x.runtime._trusted_identity_resolver = resolver
    request = AgentRequest('unreleased-delivery', session_id=f.sid, channel_id='web', is_stream=True,
        req_method=ReqMethod.COMMAND_GOAL if action == 'resume' else ReqMethod.CHAT_SEND,
        params={'project_id': f.project.project_id, 'action': action, 'attach_goal': action == 'attach'})
    try:
        with authenticated_scope(f.bob), pytest.raises(PermissionError, match='not released'):
            _ = [event async for event in x.runtime.stream(request)]
        assert called == []
        assert not x.coordinator._registry.select(session_id=f.sid)
        assert not f.c.native._requests and not f.c.side_effects and not f.c.http
    finally:
        x.runtime._trusted_identity_resolver = original


async def test_organization_rejects_before_input_snapshot_callback(case):
    inputs = {'query': 'original', 'nested': {'value': 'original'}}
    before = deepcopy(inputs)
    def change_inputs():
        inputs['query'] = 'changed'
        inputs['nested']['value'] = 'changed'
    await reject_before_admission(case, action='attach', callback=change_inputs)
    assert inputs == before  # No claim that an admitted snapshot was exercised.


async def test_organization_resume_rejects_before_old_slot_capture(case):
    manager = case.f.c.outer.goal_manager
    slot = manager._execution_origin
    def cannot_capture():
        raise AssertionError('unreleased resume reached previous-source capture')
    await reject_before_admission(case, action='resume', callback=cannot_capture)
    assert manager._execution_origin is slot


async def test_organization_resume_rejects_before_old_coordinator_capture(case):
    coordinator = case.runtime._session_coordinator
    def cannot_capture():
        raise AssertionError('unreleased resume reached previous-exit graph')
    await reject_before_admission(case, action='resume', callback=cannot_capture)
    assert case.runtime._session_coordinator is coordinator
