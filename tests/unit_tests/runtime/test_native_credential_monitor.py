"""Real credentials/Coordinator monitor; synthetic Provider receipt only."""
import asyncio
import hashlib
import json
import secrets
import time
from types import SimpleNamespace

import pytest

from jiuwenswarm.governance.organization_auth import (
    CONFIG_ENV, OrganizationAuthenticator, authenticated_scope, current_principal,
)
from jiuwenswarm.runtime.session import RuntimeSessionCoordinator, SessionWorkKind
from jiuwenswarm.runtime.session.model import SessionExecutionEndedError, SessionExecutionState, SessionCloseTimeoutError
from openjiuwen.harness_protocol import TurnEventKind


@pytest.fixture
async def case(tmp_path, monkeypatch):
    monkeypatch.setattr('jiuwenswarm.governance.session_boundary.organization_sharing_host', lambda: None)
    config = {
        'authority': 'organization:synthetic-monitor', 'signing_key': secrets.token_hex(32),
        'credentials': [],
    }
    tokens = [secrets.token_urlsafe(40) for _ in range(2)]
    for token in tokens:
        config['credentials'].append({'actor_id': 'same-actor',
            'sha256': hashlib.sha256(token.encode()).hexdigest(),
            'expires_at': time.time() + 3600, 'revoked': False})
    path = tmp_path / 'organization.json'
    path.write_text(json.dumps(config))
    path.chmod(0o600)
    monkeypatch.setenv(CONFIG_ENV, str(path))
    auth = OrganizationAuthenticator(path)
    principals = [auth.principal({'Authorization': 'Bearer ' + value}) for value in tokens]
    assert principals[0].identity() == principals[1].identity()
    assert principals[0].credential_digest != principals[1].credential_digest
    c = RuntimeSessionCoordinator(cancel_timeout=.025)
    c._set_execution_authority_capture(current_principal)
    runs = []
    obj = SimpleNamespace(c=c, auth=auth, principals=principals, path=path, config=config, runs=runs)
    yield obj
    for run in runs:
        run.release.set()
        if not run.admission.confirmed.is_set():
            # Synthetic actual completion only for fixture-owned Provider work.
            run.lifecycle.on_terminal(run.owned, TurnEventKind.FINISHED)
        run.body.set()
        if getattr(run, "tail", None) is not None:
            run.tail.set()
    await asyncio.gather(*(r.task for r in runs), return_exceptions=True)
    for run in runs:
        if run.admission.exit_task is not None:
            await asyncio.gather(run.admission.exit_task, return_exceptions=True)
    await c.close()


async def wait_for(predicate):
    async with asyncio.timeout(1):
        while not predicate():
            await asyncio.sleep(.002)


async def start(case, principal, sid, rid='original', *, kind=SessionWorkKind.CHAT_UNARY, tail=None):
    c = case.c
    await c.register_session(sid, 'process')
    record = c._sessions[sid]
    c._ensure_authority_monitor(record, interval=.01)
    entered, release, body = asyncio.Event(), asyncio.Event(), asyncio.Event()
    value = SimpleNamespace(entered=entered, release=release, body=body, calls=[], record=record, tail=tail, tail_entered=asyncio.Event())
    async def abort(owned):
        assert owned is value.owned
        value.calls.append(owned)
        await release.wait()
        value.lifecycle.on_terminal(owned, TurnEventKind.ABORTED)
    native = SimpleNamespace(abort_owned_request_turn=abort)
    async def produce():
        lifecycle = c.native_request_lifecycle(sid, rid, native, lambda: None)
        owner = lifecycle.source.host_value
        owned = SimpleNamespace(source=lifecycle.source)
        value.lifecycle, value.owner, value.owned = lifecycle, owner, owned
        value.admission = owner._native_admission
        lifecycle.on_bound(owned)
        entered.set()
        try:
            await body.wait()
            return 'ordinary body completed'
        finally:
            if tail is not None:
                value.tail_entered.set()
                await tail.wait()
    with authenticated_scope(principal):
        value.task = asyncio.create_task(c.run_unary(sid, rid, kind, produce))
        await asyncio.wait_for(entered.wait(), 1)
    assert value.owner._execution_authority is principal
    case.runs.append(value)
    return value


@pytest.mark.asyncio
@pytest.mark.parametrize('same_session', [False, True])
async def test_same_actor_other_credential_keeps_its_exact_execution(case, same_session):
    kind = SessionWorkKind.GOAL_ATTACH if same_session else SessionWorkKind.CHAT_UNARY
    a = await start(case, case.principals[0], 'token-a', 'first', kind=kind)
    b = await start(case, case.principals[1], 'token-a' if same_session else 'token-b', 'second', kind=kind)
    case.auth.revoke(case.principals[0])
    with pytest.raises(PermissionError):
        a.lifecycle.source._check_current()
    b.lifecycle.source._check_current()
    await wait_for(lambda: bool(a.calls))
    original_exit = a.admission.exit_task
    assert a.owner.cancellation_requested and not a.owner.state.terminal
    assert not a.task.done() and not a.admission.producer.cancelling()
    assert case.c._registry.get(a.owner.execution_id) is a.owner
    assert not b.owner.cancellation_requested and not b.calls and not b.task.done()
    await wait_for(lambda: a.record.authority_error is not None)
    assert a.admission.exit_task is original_exit
    assert a.record.authority_task is not None and not a.record.authority_task.done()
    a.release.set()
    await wait_for(lambda: a.owner.state.terminal)
    assert a.owner.state is SessionExecutionState.CANCELLED
    assert a.calls == [a.owned]
    assert not b.owner.cancellation_requested and not b.calls
    b.lifecycle.source._check_current()


@pytest.mark.asyncio
async def test_invalid_original_config_keeps_monitor_owner_and_does_not_resurrect_after_repair(case):
    a = await start(case, case.principals[0], 'bad-config')
    case.path.write_text('{ invalid configuration')
    with pytest.raises(json.JSONDecodeError):
        a.lifecycle.source._check_current()
    await wait_for(lambda: bool(a.calls))
    await wait_for(lambda: a.record.authority_error is not None)
    original_exit = a.admission.exit_task
    assert not a.record.authority_task.done()
    assert case.c._registry.get(a.owner.execution_id) is a.owner
    assert not a.owner.state.terminal and not a.admission.producer.cancelling()
    case.path.write_text(json.dumps(case.config))
    assert case.principals[0].identity() == case.principals[1].identity()
    with pytest.raises(SessionExecutionEndedError):
        a.lifecycle.source._check_current()
    a.release.set()
    await wait_for(lambda: a.owner.state.terminal)
    assert a.owner.state is SessionExecutionState.CANCELLED
    assert a.admission.exit_task is original_exit and a.calls == [a.owned]


@pytest.mark.asyncio
async def test_expired_original_credential_not_same_actor_lookup(case):
    a = await start(case, case.principals[0], 'expired-a')
    b = await start(case, case.principals[1], 'unexpired-b')
    content = json.loads(case.path.read_text())
    content['credentials'][0]['expires_at'] = time.time() - 1
    case.path.write_text(json.dumps(content))
    await wait_for(lambda: bool(a.calls))
    assert not b.calls and not b.owner.cancellation_requested
    assert case.auth.resolve_actor(b.owner._execution_authority.identity(), 'same-actor') is not None
    with pytest.raises(PermissionError):
        case.principals[0].identity()
    with pytest.raises(SessionExecutionEndedError):
        a.lifecycle.source._check_current()
    b.lifecycle.source._check_current()
    a.release.set()
    await wait_for(lambda: a.owner.state.terminal)


@pytest.mark.asyncio
async def test_stale_original_record_and_generation_cannot_stop_replacement(case):
    a = await start(case, case.principals[0], 'same-session')
    original_monitor = a.record.authority_task
    case.auth.revoke(case.principals[0])
    await wait_for(lambda: bool(a.calls))
    a.release.set()
    await wait_for(lambda: a.owner.state.terminal)
    await case.c.close_session('same-session', generation=a.owner.generation)
    b = await start(case, case.principals[1], 'same-session', rid='successor')
    assert b.owner.generation > a.owner.generation
    with pytest.raises(SessionExecutionEndedError):
        a.admission.start_exit()
    await case.c._revalidate_record_authorities(a.record)
    await asyncio.sleep(.02)
    assert original_monitor.done()
    assert not b.calls and not b.owner.cancellation_requested
    b.lifecycle.source._check_current()


@pytest.mark.asyncio
async def test_revoked_principal_rejected_before_registering_new_handle(case):
    a = await start(case, case.principals[0], 'rejected-new')
    case.auth.revoke(case.principals[0])
    before = tuple(case.c._registry.select(session_id='rejected-new'))
    async def must_not_run():
        raise AssertionError('revoked request admitted')
    with authenticated_scope(case.principals[0]):
        with pytest.raises(PermissionError):
            await case.c.run_unary('rejected-new', 'forbidden', SessionWorkKind.CHAT_UNARY, must_not_run)
    assert tuple(case.c._registry.select(session_id='rejected-new')) == before
    a.release.set()
    await wait_for(lambda: a.owner.state.terminal)


@pytest.mark.asyncio
async def test_same_session_invalid_credentials_all_fenced_even_when_first_exit_stuck(case):
    # Existing non-scheduled goal admissions can coexist; no fabricated handles.
    a = await start(case, case.principals[0], 'same-session-two', 'first', kind=SessionWorkKind.GOAL_ATTACH)
    b = await start(case, case.principals[1], 'same-session-two', 'second', kind=SessionWorkKind.GOAL_ATTACH)
    case.path.write_text('{ invalid configuration')
    await wait_for(lambda: bool(a.calls))
    with pytest.raises(ExceptionGroup) as caught:
        await case.c._revalidate_record_authorities(a.record)
    def leaves(error):
        if isinstance(error, BaseExceptionGroup):
            return [leaf for child in error.exceptions for leaf in leaves(child)]
        return [error]
    errors = leaves(caught.value)
    assert errors and all(isinstance(error, SessionCloseTimeoutError) for error in errors)
    assert {eid for error in errors for eid in error.execution_ids} == {a.owner.execution_id, b.owner.execution_id}
    assert b.owner.cancellation_requested, 'first timed-out exit starves second original credential fence'
    assert b.calls == [b.owned], 'second original exit was never requested'
    assert a.admission.exit_task is not b.admission.exit_task


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", [SessionWorkKind.CHAT_UNARY, SessionWorkKind.GOAL_ATTACH])
async def test_explicit_timeout_retry_keeps_same_original_native_exit_task(case, kind):
    a = await start(case, case.principals[0], 'retry-original', kind=kind)
    first = await case.c.cancel_execution('retry-original', execution_id=a.owner.execution_id,
                                          generation=a.owner.generation)
    assert first.timed_out == (a.owner.execution_id,)
    exit_task = a.admission.exit_task
    assert not a.admission.producer.cancelling() and not a.task.done()
    assert not a.owner.state.terminal
    a.release.set()
    second = await case.c.cancel_execution('retry-original', execution_id=a.owner.execution_id,
                                           generation=a.owner.generation)
    assert not second.timed_out
    assert a.admission.exit_task is exit_task and a.calls == [a.owned]
    assert a.owner.state is SessionExecutionState.CANCELLED


@pytest.mark.asyncio
async def test_confirmed_direct_native_retry_does_not_recancel_producer_finalizer(case):
    tail = asyncio.Event()
    a = await start(case, case.principals[0], 'direct-tail', kind=SessionWorkKind.GOAL_ATTACH, tail=tail)
    a.release.set()
    first = await case.c.cancel_execution('direct-tail', execution_id=a.owner.execution_id,
                                          generation=a.owner.generation)
    assert first.timed_out == (a.owner.execution_id,)
    assert a.tail_entered.is_set(), 'provider exit fence is not producer cancel delivery'
    assert a.admission.producer.cancelling() == 1
    second = await case.c.cancel_execution('direct-tail', execution_id=a.owner.execution_id,
                                           generation=a.owner.generation)
    assert second.timed_out == first.timed_out
    assert a.admission.producer.cancelling() == 1 and not a.admission.producer.done()
    assert not a.owner.state.terminal and a.calls == [a.owned]
    tail.set()
    await wait_for(lambda: a.admission.producer.done())
    await wait_for(lambda: a.owner.state.terminal)
    assert a.owner.state is SessionExecutionState.CANCELLED
