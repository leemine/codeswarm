"""Original credential provenance at admission, not active-exit confirmation."""
import asyncio
import hashlib
import json
import secrets
import time
from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.governance.organization_auth import authenticated_scope, current_principal
from jiuwenswarm.runtime import AgentRuntime
from jiuwenswarm.runtime.session import SessionWorkKind
from jiuwenswarm.runtime.session.model import SessionExecutionState
from tests.unit_tests.governance.test_organization_auth import credentials, principal


@pytest.fixture
async def admitted(credentials, monkeypatch):
    auth, tokens, path = credentials
    monkeypatch.setattr('jiuwenswarm.governance.session_boundary.organization_sharing_host', lambda: None)
    runtime = AgentRuntime(agent_manager=SimpleNamespace(), initializer=AsyncMock())
    coordinator = runtime._session_coordinator
    await coordinator.register_session('session-a', 'process')
    try:
        yield runtime, coordinator, auth, tokens, path
    finally:
        await coordinator.close()


@pytest.mark.asyncio
async def test_same_actor_credentials_remain_distinct_and_private(admitted):
    _, coordinator, auth, tokens, path = admitted
    first = principal(auth, tokens, 'alice')
    second_token = secrets.token_urlsafe(32)
    config = json.loads(path.read_text())
    config['credentials'].append({'actor_id': 'alice', 'sha256': hashlib.sha256(second_token.encode()).hexdigest(),
                                  'expires_at': time.time() + 600, 'revoked': False})
    path.write_text(json.dumps(config))
    second = auth.principal({'Authorization': 'Bearer ' + second_token})
    assert first.identity() == second.identity()
    handles = []
    for index, owner in enumerate((first, second)):
        with authenticated_scope(owner):
            await coordinator.run_unary('session-a', str(index), SessionWorkKind.SESSION_MESSAGE,
                                        lambda: asyncio.sleep(0, result='ok'))
        handle, = coordinator._registry.select(session_id='session-a', request_id=str(index))
        assert handle._execution_authority is owner
        handles.append(handle)
        assert owner.credential_digest not in repr(handle)
        assert owner.credential_digest not in json.dumps(asdict(handle.snapshot()), default=str)
        assert '_execution_authority' not in asdict(handle.snapshot())
    auth.revoke(first)
    with pytest.raises(PermissionError):
        handles[0]._execution_authority.identity()
    assert handles[1]._execution_authority.identity() == second.bound_identity


@pytest.mark.asyncio
async def test_queued_admission_keeps_original_credential_after_context_changes(admitted):
    _, coordinator, auth, tokens, _ = admitted
    first = principal(auth, tokens, 'alice')
    second = principal(auth, tokens, 'bob')
    release = asyncio.Event()
    entered = asyncio.Event()
    observed = []
    async def operation():
        entered.set()
        await release.wait()
        observed.append(current_principal())
    try:
        with authenticated_scope(first):
            snapshot = coordinator.submit_unary('session-a', 'queued', SessionWorkKind.SESSION_MESSAGE, operation)
        handle = coordinator._registry.get(snapshot.execution_id)
        assert handle._execution_authority is first
        await entered.wait()
        with authenticated_scope(second):
            release.set()
            await handle.terminal_event.wait()
        assert observed == [first]
        assert handle._execution_authority is first
    finally:
        release.set()


@pytest.mark.asyncio
async def test_revoked_admission_does_not_supersede_waiting_original(admitted):
    _, coordinator, auth, tokens, _ = admitted
    owner = principal(auth, tokens, 'alice')
    with authenticated_scope(owner):
        await coordinator.run_unary('session-a', 'first', SessionWorkKind.CHAT_UNARY,
                                    lambda: asyncio.sleep(0, result='question'), suspension_key=lambda _: 'q')
        first, = coordinator._registry.select(session_id='session-a', request_id='first')
        auth.revoke(owner)
        operation = AsyncMock()
        with pytest.raises(PermissionError):
            await coordinator.run_unary('session-a', 'rejected', SessionWorkKind.CHAT_UNARY, operation)
    operation.assert_not_awaited()
    assert first.state is SessionExecutionState.WAITING_FOR_CONTROL
    assert not first.cancellation_requested
    assert not coordinator._registry.select(session_id='session-a', request_id='rejected')


@pytest.mark.asyncio
@pytest.mark.parametrize('streaming', [False, True])
async def test_rejected_control_leaves_parent_and_claim_retryable(admitted, streaming):
    _, coordinator, auth, tokens, _ = admitted
    first = principal(auth, tokens, 'alice')
    second = principal(auth, tokens, 'bob')
    with authenticated_scope(first):
        await coordinator.run_unary('session-a', 'first', SessionWorkKind.CHAT_UNARY,
                                    lambda: asyncio.sleep(0, result='question'), suspension_key=lambda _: 'q')
        auth.revoke(first)
        async def accepted():
            yield 'accepted'
        with pytest.raises(PermissionError):
            if streaming:
                _ = [value async for value in coordinator.deliver_control_stream('session-a', 'q', accepted)]
            else:
                await coordinator.deliver_control('session-a', 'q', lambda: asyncio.sleep(0, result='accepted'))
    parent, = coordinator._registry.select(session_id='session-a', request_id='first')
    assert parent.state is SessionExecutionState.WAITING_FOR_CONTROL
    assert not coordinator._control_claims
    assert not coordinator._sessions['session-a'].stream_control_claims
    # This tests Coordinator provenance only; product owner permission is checked
    # before this API. A control does not replace the original parent's principal.
    with authenticated_scope(second):
        assert await coordinator.deliver_control('session-a', 'q', lambda: asyncio.sleep(0, result='accepted')) == 'accepted'
    assert parent._execution_authority is first
    child, = coordinator._registry.select(session_id='session-a', request_id='q')
    assert child._execution_authority is second


@pytest.mark.asyncio
async def test_detached_observer_does_not_capture_inherited_credential(admitted):
    _, coordinator, auth, tokens, _ = admitted
    owner = principal(auth, tokens, 'alice')
    with authenticated_scope(owner):
        auth.revoke(owner)
        snapshot = coordinator.begin_detached_turn('session-a', 'observer-turn')
    handle = coordinator._registry.get(snapshot.execution_id)
    assert handle._execution_authority is None
    # An unbound observed Turn is not evidence of a credential's confirmed exit.
    assert handle.state is SessionExecutionState.RUNNING


@pytest.mark.asyncio
async def test_legacy_no_principal_admission_keeps_no_authority(admitted):
    _, coordinator, *_ = admitted
    await coordinator.run_unary('session-a', 'legacy', SessionWorkKind.SESSION_MESSAGE,
                                lambda: asyncio.sleep(0, result='ok'))
    handle, = coordinator._registry.select(session_id='session-a', request_id='legacy')
    assert handle._execution_authority is None
