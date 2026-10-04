"""Real Coordinator controls and monitor; synthetic bounded Provider receipt only."""
import asyncio
from types import SimpleNamespace
import pytest
from jiuwenswarm.runtime.session import RuntimeSessionCoordinator, SessionWorkKind
from jiuwenswarm.runtime.session.model import SessionExecutionState, RuntimeSessionState
from openjiuwen.harness_protocol import TurnEventKind

@pytest.fixture(autouse=True)
def isolated_owner_store(monkeypatch):
    monkeypatch.setattr('jiuwenswarm.governance.session_boundary.organization_sharing_host', lambda: None)

async def paused(*, stuck=False):
    c = RuntimeSessionCoordinator(cancel_timeout=.015)
    c._set_execution_authority_capture(lambda: SimpleNamespace(identity=lambda: 'synthetic-stable-original-principal'))
    await c.register_session('review-native', 'process')
    record = c._sessions['review-native']
    aborts = []
    release = asyncio.Event()
    state = {}
    async def abort(owned):
        aborts.append(owned)
        if stuck:
            await release.wait()
        state['admission'].terminal(owned, TurnEventKind.ABORTED)
    native = SimpleNamespace(abort_owned_request_turn=abort)
    async def produce():
        owner = c._registry.select(session_id='review-native', request_id='original')[0]
        lifecycle = c.native_request_lifecycle('review-native', 'original', native, lambda: None)
        admission = owner._native_admission
        assert owner.preserve_control_origin is True
        assert lifecycle.source is admission.source
        lifecycle.on_bound(SimpleNamespace(source=lifecycle.source))
        state['admission'] = admission
        yield 'question'
    assert [x async for x in c.run_stream('review-native', 'original', SessionWorkKind.CHAT_STREAM,
         produce, suspension_key=lambda _: 'answer')] == ['question']
    assert state['admission'].producer.done()
    assert state['admission'].owner.state is SessionExecutionState.WAITING_FOR_CONTROL
    return c, record, state['admission'], aborts, release

async def cleanup(c, admission, release):
    release.set()
    if not admission.confirmed.is_set():
        admission.terminal(admission.owned_turn, TurnEventKind.FINISHED)
    if admission.exit_task is not None:
        await asyncio.gather(admission.exit_task, return_exceptions=True)
    if admission.record.resource_close_task is not None:
        await c.close_session(admission.owner.session_id, release_resources=lambda: asyncio.sleep(0))
    await c.close()

@pytest.mark.asyncio
async def test_legitimate_control_ack_must_not_abort_resumed_turn():
    c, record, admission, aborts, release = await paused()
    try:
        async def accepted():
            yield 'accepted'
        assert [x async for x in c.deliver_control_stream('review-native', 'answer', accepted)] == ['accepted']
        assert not admission.owner.waiting_control_id
        # A streamed answer ACK is not a Provider terminal; actual original Turn remains live.
        await c._revalidate_native_authorities(record)
        assert not aborts, f'ordinary control resumed but monitor aborted it; deferred={admission.deferred_terminal!r}'
    finally:
        await cleanup(c, admission, release)

@pytest.mark.asyncio
async def test_native_exit_timeout_must_not_starve_existing_session_revocation_watch():
    c, record, admission, aborts, release = await paused(stuck=True)
    checked = []
    class Watch:
        def check_authority(self):
            checked.append(True)
            raise PermissionError('revoked source')
        def check_owner(self): pass
        async def release(self): pass
    record.authority_watch = Watch()
    admission.check_authority = lambda: (_ for _ in ()).throw(PermissionError('revoked source'))
    try:
        with pytest.raises(Exception):
            await c.revalidate_session_authorities()
        assert checked, 'Native timeout bypassed the original Session watch completely'
        assert record.state is RuntimeSessionState.QUIESCING
    finally:
        record.authority_watch = None
        await cleanup(c, admission, release)

@pytest.mark.asyncio
async def test_real_terminal_keeps_root_until_original_control_consumer_tail_exits():
    c, record, admission, aborts, release = await paused()
    entered, tail = asyncio.Event(), asyncio.Event()
    async def answer():
        entered.set()
        yield 'accepted'
        await tail.wait()
    async def consume():
        return [x async for x in c.deliver_control_stream('review-native', 'answer', answer)]
    consumer = asyncio.create_task(consume())
    try:
        await entered.wait()
        admission.terminal(admission.owned_turn, TurnEventKind.FINISHED)
        assert admission.confirmed.is_set() and admission.producer.done()
        assert not admission.owner.state.terminal
        await c._revalidate_native_authorities(record)
        assert not aborts
        tail.set()
        assert await consumer == ['accepted']
        assert admission.owner.state is SessionExecutionState.SUCCEEDED
    finally:
        tail.set()
        await asyncio.gather(consumer, return_exceptions=True)
        await cleanup(c, admission, release)

@pytest.mark.asyncio
@pytest.mark.parametrize('required', [False, True])
async def test_coordinator_explicit_trusted_sdk_mode_does_not_implicitly_weaken_organization_mode(required):
    from jiuwenswarm.runtime.session.model import SessionExecutionEndedError
    c = RuntimeSessionCoordinator(cancel_timeout=.02)
    await c.register_session('review-sdk', 'sdk')
    calls = []
    async def operation():
        lifecycle = c.native_request_lifecycle('review-sdk', 'req', object(),
             lambda: calls.append('checked'), require_principal=required)
        lifecycle.source._check_current()
        lifecycle.on_not_admitted()
        return 'ordinary not-admitted finish'
    try:
        if required:
            with pytest.raises(SessionExecutionEndedError):
                await c.run_unary('review-sdk', 'req', SessionWorkKind.CHAT_UNARY, operation)
            assert not calls
        else:
            assert await c.run_unary('review-sdk', 'req', SessionWorkKind.CHAT_UNARY, operation) == 'ordinary not-admitted finish'
            assert len(calls) >= 2
    finally:
        await c.close()
