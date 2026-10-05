"""Actual Runtime/registry completion, with a bounded synthetic Provider tail."""
import asyncio
from types import SimpleNamespace

import pytest

from openjiuwen.core.controller.schema.execution_origin import ExecutionOrigin
from jiuwenswarm.runtime.native_execution_origin import NativeExecutionAdmission
from jiuwenswarm.runtime.session import RuntimeSessionCoordinator, SessionWorkKind
from jiuwenswarm.runtime.session.model import SessionExecutionState


@pytest.fixture
async def coordinator(monkeypatch):
    monkeypatch.setattr('jiuwenswarm.governance.session_boundary.organization_sharing_host', lambda: None)
    value = RuntimeSessionCoordinator(cancel_timeout=.02)
    await value.register_session('original', 'process')
    yield value
    await value.close()


async def admit(coordinator, *, bind=True):
    entered, body, tail = asyncio.Event(), asyncio.Event(), asyncio.Event()
    calls = []
    result = SimpleNamespace()

    async def abort(owned):
        calls.append(owned)
        await tail.wait()
        result.admission.terminal(owned, None)

    async def run():
        owner, = coordinator._registry.select(session_id='original', request_id='request')
        admission = NativeExecutionAdmission(coordinator, coordinator._sessions['original'],
            owner, SimpleNamespace(abort_owned_request_turn=abort), lambda: None, asyncio.current_task())
        owner._native_admission = admission
        admission.source = ExecutionOrigin(owner, _checker=admission.check_current)
        admission.producer.add_done_callback(admission.settle_terminal)
        owned = SimpleNamespace(source=admission.source)
        result.owner, result.admission, result.owned = owner, admission, owned
        if bind:
            admission.bind(owned)
        entered.set()
        await body.wait()
        return 'finished body'

    task = asyncio.create_task(coordinator.run_unary('original', 'request', SessionWorkKind.CHAT_UNARY, run))
    await entered.wait()
    result.task, result.body, result.tail, result.calls = task, body, tail, calls
    return result


@pytest.mark.asyncio
async def test_body_result_cannot_terminal_or_evict_before_provider_receipt(coordinator):
    run = await admit(coordinator)
    run.body.set()
    assert await run.task == 'finished body'
    assert not run.owner.state.terminal
    assert not run.owner.terminal_event.is_set()
    assert coordinator._registry.get(run.owner.execution_id) is run.owner
    run.admission.terminal(run.owned, None)
    assert run.owner.state is SessionExecutionState.SUCCEEDED


@pytest.mark.asyncio
async def test_cancel_timeout_retains_original_provider_and_producer_for_retry(coordinator):
    run = await admit(coordinator)
    first = await coordinator.cancel_execution('original', execution_id=run.owner.execution_id)
    assert first.timed_out == (run.owner.execution_id,)
    assert not run.admission.producer.done() and not run.admission.producer.cancelling()
    assert not run.owner.state.terminal
    original = run.admission.exit_task
    assert run.calls == [run.owned]
    run.tail.set()
    second = await coordinator.cancel_execution('original', execution_id=run.owner.execution_id)
    assert not second.timed_out
    assert run.admission.exit_task is original
    with pytest.raises(asyncio.CancelledError):
        await run.task
    assert run.owner.state is SessionExecutionState.CANCELLED


@pytest.mark.asyncio
async def test_revoke_before_receipt_waits_for_original_admission_without_cancelling_submit(coordinator):
    run = await admit(coordinator, bind=False)
    first = await coordinator.cancel_execution('original', execution_id=run.owner.execution_id)
    assert first.timed_out == (run.owner.execution_id,)
    assert not run.admission.producer.cancelling()
    assert run.calls == []
    run.admission.bind(run.owned)
    run.tail.set()
    second = await coordinator.cancel_execution('original', execution_id=run.owner.execution_id)
    assert not second.timed_out
    assert run.calls == [run.owned]
    with pytest.raises(asyncio.CancelledError):
        await run.task


@pytest.mark.asyncio
async def test_session_close_keeps_resources_until_original_provider_exits(coordinator):
    run = await admit(coordinator)
    released = []
    async def release():
        released.append(True)
    first = await coordinator.close_session('original', release_resources=release)
    assert first.timed_out == (run.owner.execution_id,) and not released
    assert not run.admission.producer.cancelling()
    run.tail.set()
    second = await coordinator.close_session('original', release_resources=release)
    assert not second.timed_out and released == [True]
    with pytest.raises(asyncio.CancelledError):
        await run.task


@pytest.mark.asyncio
async def test_proven_unaccepted_submission_can_finish_without_provider_abort(coordinator):
    run = await admit(coordinator, bind=False)
    run.admission.not_admitted()
    run.body.set()
    await run.task
    assert run.owner.state is SessionExecutionState.SUCCEEDED
    assert not run.calls


@pytest.mark.asyncio
@pytest.mark.parametrize('kind,state', [('FAILED', SessionExecutionState.FAILED),
                                      ('ABORTED', SessionExecutionState.CANCELLED)])
async def test_successful_output_consumption_preserves_actual_provider_failure(coordinator, kind, state):
    from openjiuwen.harness_protocol import TurnEventKind
    run = await admit(coordinator)
    run.owner.preserve_control_origin = True
    run.admission.terminal(run.owned, TurnEventKind[kind])
    assert not run.owner.state.terminal  # The Runtime body still owns its tail.
    run.body.set()
    await run.task
    assert run.owner.state is state
