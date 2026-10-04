"""Actual facade entry under real Runtime/Coordinator/identity/resource context.

No fake success; task-boundary tests explicitly bridge adapter presentation only.
The initial Goal is persisted fixture data; model/tool IO are original synthetic fixtures.
"""
import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest
from openjiuwen.harness.goal.schema import GoalStatus
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.governance.organization_auth import authenticated_scope
from jiuwenswarm.governance.tool_context import tool_authority_scope
from jiuwenswarm.runtime.context import set_runtime_context, reset_runtime_context
from jiuwenswarm.runtime.session import SessionWorkKind
from tests.unit_tests.runtime import test_native_goal_readmission_runtime as base

credentials = base.credentials
goal_case = base.goal_case
native_case = base.native_case
full_case = base.full_case
case = base.case


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["attach", "resume"])
async def test_real_facade_attach_child_producer_reaches_original_helper(case, monkeypatch, action):
    """Task-boundary component: adapter presentation body is a declared bridge.

    Actual facade creates its real run_stream_task; actual Runtime authority,
    coordinator and Native helper are unchanged. This is NOT full adapter/UI validation.
    """
    from unittest.mock import AsyncMock, Mock
    x, f = case, case.f
    if action == 'resume':
        record = f.c.outer.goal_manager.peek()
        record.status = GoalStatus.PAUSED
        f.c.outer.goal_manager._store.save(record)
    request = AgentRequest('facade-attach', session_id=f.sid, channel_id='web', is_stream=True,
        req_method=ReqMethod.COMMAND_GOAL if action == 'resume' else ReqMethod.CHAT_SEND,
        params={'project_id': f.project.project_id, 'action': action, 'attach_goal': action == 'attach', 'mode': 'agent'})
    tasks, seen = [], []
    async def adapter_presentation_bridge(req, inputs):
        owner = x.coordinator.native_execution_owner(f.sid, req.request_id)
        tasks.append((owner.task, asyncio.current_task()))
        if action == 'resume':
            _, result = await f.child._submit_native_goal_request(req, inputs, action='resume')
            await result
        else:
            await f.child._attach_native_goal_request(req, inputs)
        if False:
            yield None
    monkeypatch.setattr(f.root, 'process_message_stream_impl', adapter_presentation_bridge)
    monkeypatch.setattr(f.facade, '_ensure_adapter', Mock(return_value=f.root))
    monkeypatch.setattr(f.facade, '_select_execution_before_mcp', Mock())
    monkeypatch.setattr(f.facade, '_adapter_mode_for_request', lambda _: 'agent')
    monkeypatch.setattr(f.facade, '_build_inputs', lambda _: ({'query': '', 'conversation_id': f.sid}, 'local', SimpleNamespace(text='')))
    monkeypatch.setattr(f.facade, 'reconcile_session_mcp', AsyncMock())
    monkeypatch.setattr(f.facade, '_sdk_name', 'fixture-native', raising=False)
    monkeypatch.setattr(f.root, 'validate_auto_permission_workspace_request', Mock())
    async def operation():
        bundle = x.runtime._resource_authorizers_for(request)
        resources = replace(f.c.bundle, native_lifecycle_factory=bundle.native_lifecycle_factory)
        context = set_runtime_context(x.runtime, f.manager)
        try:
            with tool_authority_scope(None, provider_authorizers=resources):
                seen.extend([chunk.payload async for chunk in f.facade.process_message_stream(request)])
        finally:
            reset_runtime_context(context)
    with authenticated_scope(f.bob):
        await asyncio.wait_for(asyncio.create_task(x.coordinator.run_unary(
            f.sid, request.request_id, SessionWorkKind.GOAL_STREAM if action == 'resume' else SessionWorkKind.GOAL_ATTACH, operation)), 6)
    assert len(tasks) == 1 and tasks[0][0] is not tasks[0][1], 'actual facade child task not reached'
    errors = [p for p in seen if isinstance(p, dict) and p.get('event_type') == 'chat.error']
    assert not errors, f'original facade producer graph rejected by original Native helper: {errors}'
    assert f.c.native._native._first_managed_turn is not None


@pytest.mark.asyncio
async def test_late_facade_child_cannot_use_finished_original_runtime_producer(case, monkeypatch):
    from unittest.mock import AsyncMock, Mock
    x, f = case, case.f
    gate = asyncio.Event()
    jobs, seen, reached = [], [], []
    request = AgentRequest('facade-late', session_id=f.sid, channel_id='web', is_stream=True,
        req_method=ReqMethod.CHAT_SEND,
        params={'project_id': f.project.project_id, 'attach_goal': True, 'mode': 'agent'})
    async def adapter_presentation_bridge(req, inputs):
        reached.append(asyncio.current_task())
        await f.child._attach_native_goal_request(req, inputs)
        if False:
            yield None
    monkeypatch.setattr(f.root, 'process_message_stream_impl', adapter_presentation_bridge)
    monkeypatch.setattr(f.facade, '_ensure_adapter', Mock(return_value=f.root))
    monkeypatch.setattr(f.facade, '_select_execution_before_mcp', Mock())
    monkeypatch.setattr(f.facade, '_adapter_mode_for_request', lambda _: 'agent')
    monkeypatch.setattr(f.facade, '_build_inputs', lambda _: ({'query': '', 'conversation_id': f.sid}, 'local', SimpleNamespace(text='')))
    monkeypatch.setattr(f.facade, 'reconcile_session_mcp', AsyncMock())
    monkeypatch.setattr(f.facade, '_sdk_name', 'fixture-native', raising=False)
    monkeypatch.setattr(f.root, 'validate_auto_permission_workspace_request', Mock())
    async def late_consume():
        await gate.wait()
        seen.extend([chunk.payload async for chunk in f.facade.process_message_stream(request)])
    async def operation():
        bundle = x.runtime._resource_authorizers_for(request)
        resources = replace(f.c.bundle, native_lifecycle_factory=bundle.native_lifecycle_factory)
        context = set_runtime_context(x.runtime, f.manager)
        try:
            with tool_authority_scope(None, provider_authorizers=resources):
                jobs.append(asyncio.create_task(late_consume()))
        finally:
            reset_runtime_context(context)
    try:
        with authenticated_scope(f.bob):
            root = asyncio.create_task(x.coordinator.run_unary(f.sid, request.request_id, SessionWorkKind.GOAL_ATTACH, operation))
            await asyncio.wait_for(root, 6)
        owner, = x.coordinator._registry.select(session_id=f.sid, request_id=request.request_id)
        assert root.done() and owner.state.terminal
        gate.set()
        await asyncio.wait_for(jobs[0], 6)
        assert len(reached) == 1
        assert any(isinstance(p, dict) and p.get('event_type') == 'chat.error' for p in seen)
        assert f.c.native._native._first_managed_turn is None
        assert not f.c.side_effects and not f.c.http
    finally:
        gate.set()
        for job in jobs:
            if not job.done():
                job.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)
