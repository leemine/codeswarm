"""Bounded real guard and cancellation-fact review; Provider tail is synthetic."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from openjiuwen.harness_protocol import TurnEventKind
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.common.work_mode import DEFAULT_PROJECT_ID_WORK
from jiuwenswarm.governance.organization_auth import authenticated_scope, current_identity, CONFIG_ENV
from jiuwenswarm.governance.preparation import GovernanceError
from jiuwenswarm.governance.session_boundary import admit_session_request
from jiuwenswarm.governance.session_sharing import SessionSharingDenied
from jiuwenswarm.runtime import AgentRuntime
from jiuwenswarm.runtime.session import RuntimeSessionCoordinator, SessionWorkKind
from jiuwenswarm.server.runtime.session import project_store
from tests.unit_tests.runtime import test_native_runtime_source as fixtures

full_case = fixtures.full_case
native_case = fixtures.native_case
credentials = fixtures.credentials


@pytest.mark.asyncio
@pytest.mark.parametrize('project_kind', ['none', 'default', 'unmanaged'])
async def test_organization_public_chat_rejects_unowned_default_before_resource_none(full_case, project_kind, tmp_path_factory):
    f = full_case
    pid = ''
    if project_kind == 'default':
        pid = DEFAULT_PROJECT_ID_WORK
    elif project_kind == 'unmanaged':
        otherdir = tmp_path_factory.mktemp('unmanaged-work')
        pid = project_store.create_project('Legacy', str(otherdir)).project_id
    runtime = AgentRuntime(agent_manager=f.manager, initializer=AsyncMock(),
        trusted_identity_resolver=lambda _: current_identity(),
        project_authorizer=f.access, resource_authorizer=f.access, organization_session_host=f.host)
    f.state.runtime = runtime
    runtime._started = True  # allocation/bootstrap fixture; all actual guards remain unchanged
    req = AgentRequest('without-managed-project', session_id='unowned-session', channel_id='web',
                       req_method=ReqMethod.CHAT_SEND, params={'project_id': pid, 'query': 'ordinary'})
    with authenticated_scope(f.bob):
        assert runtime._resource_authorizers_for(req) is None
        with pytest.raises(SessionSharingDenied):
            admit_session_request('chat.send', {'session_id': req.session_id},
                                  identity_resolver=current_identity, host=f.host)
        with pytest.raises(GovernanceError, match='current trusted Session owner'):
            await runtime.invoke(req)
        stream = runtime.stream(req)
        try:
            with pytest.raises(GovernanceError, match='current trusted Session owner'):
                await anext(stream)
        finally:
            await stream.aclose()
    assert not f.c.calls
    assert not runtime._session_coordinator.snapshot_session(req.session_id)
    with pytest.raises(SessionSharingDenied):
        f.host.register_owner_and_source(req.session_id, f.bob.identity(), pid)


@pytest.mark.asyncio
async def test_nonorganization_default_sdk_keeps_existing_legacy_guard(full_case, monkeypatch):
    f = full_case
    monkeypatch.delenv(CONFIG_ENV)
    runtime = AgentRuntime(agent_manager=f.manager, initializer=AsyncMock(),
                           trusted_identity_resolver=lambda _: f.bob.identity(),
                           project_authorizer=f.access, resource_authorizer=f.access)
    f.state.runtime = runtime
    assert runtime._organization_session_host is None
    req = AgentRequest('legacy', session_id='legacy', req_method=ReqMethod.CHAT_SEND)
    assert runtime._governance_owned_request(req).request_id == 'legacy'
    assert runtime._resource_authorizers_for(req) is None


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', [SessionWorkKind.SESSION_MESSAGE, SessionWorkKind.CHAT_UNARY])
async def test_native_producer_cleanup_cancel_is_requested_once_across_scheduler_and_direct(kind):
    c = RuntimeSessionCoordinator(cancel_timeout=.01)
    await c.register_session('cancel-proof', 'web')
    entered, cancel_seen, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    state = {}
    cancels = []
    async def provider_exit(owned):
        state['admission'].terminal(owned, TurnEventKind.ABORTED)
    native = SimpleNamespace(abort_owned_request_turn=provider_exit)
    async def body():
        lifecycle = c.native_request_lifecycle('cancel-proof', 'original', native,
                                                lambda: None, require_principal=False)
        owner, = c._registry.select(session_id='cancel-proof', request_id='original')
        state['admission'] = owner._native_admission
        lifecycle.on_bound(SimpleNamespace(source=lifecycle.source))
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancels.append('first')
            asyncio.current_task().uncancel()
            cancel_seen.set()
            try:
                await release.wait()  # original producer resource cleanup
            except asyncio.CancelledError:
                cancels.append('second')
                raise
    running = asyncio.create_task(c.run_unary('cancel-proof', 'original', kind, body))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        await c.cancel_execution('cancel-proof', request_id='original')
        await cancel_seen.wait()
        if kind is SessionWorkKind.CHAT_UNARY:
            await c.cancel_execution('cancel-proof', request_id='original')
        assert cancels == ['first'], 'scheduler cancellation was not recorded before direct/retry cancellation'
        assert state['admission'].producer_cancel_requested is True
        released = []
        async def release_resources():
            released.append('released')
        close_result = await c.close_session('cancel-proof', wait_timeout=.01,
                                             release_resources=release_resources)
        assert close_result.timed_out, 'close must retain the still-running original cleanup'
        assert cancels == ['first'], 'processor close must not send a second producer cancellation'
        assert not released and not running.done()
        assert not state['admission'].owner.state.terminal
        release.set()
        await asyncio.wait_for(asyncio.gather(running, return_exceptions=True), 1)
        final_close = await c.close_session('cancel-proof', wait_timeout=.1,
                                           release_resources=release_resources)
        assert not final_close.timed_out
        assert released == ['released']
    finally:
        release.set()
        await asyncio.gather(running, return_exceptions=True)
        await c.close()
