# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Owner admission runs before existing create/fork publication and is compensated."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from jiuwenswarm.runtime.session_provisioner import (
    SessionForkInput,
    SessionProvisionCommitTiming,
    SessionProvisionState,
)
from tests.unit_tests.runtime.test_session_create_provisioner import (
    _State, _input, _install_product_hooks, _provisioner,
)


class Host:
    def __init__(self, events):
        self.events = events
        self.owners = set()
        self.denied = set()
        self.rollback_error = None
        self.calls = []
        self.rollbacks = []

    def before_publish(self, session_id, project_id, created):
        self.events.append(f"owner:{session_id}:{created}")
        self.calls.append((session_id, project_id, created))
        if session_id in self.denied:
            raise PermissionError("owner revoked")
        if not created:
            if session_id not in self.owners:
                raise PermissionError("unknown owner")
            return None
        if session_id in self.owners:
            raise PermissionError("owner reservation already exists")
        self.owners.add(session_id)

        def rollback():
            self.rollbacks.append(session_id)
            if self.rollback_error is not None:
                raise self.rollback_error
            self.owners.remove(session_id)

        return rollback


@pytest.fixture
def create_setup(monkeypatch, tmp_path):
    from jiuwenswarm.server.runtime.session import session_metadata

    assert session_metadata.flush_pending_writes()
    session_metadata.remove_session_metadata_cache('created-session')
    state = _State()
    _install_product_hooks(monkeypatch, tmp_path, state)
    provisioner = _provisioner(state)
    host = Host(state.events)
    provisioner.set_owner_lifecycle(host)
    yield provisioner, host, state
    assert session_metadata.flush_pending_writes()
    session_metadata.remove_session_metadata_cache('created-session')


@pytest.mark.asyncio
async def test_create_owner_is_persistent_before_metadata_and_abort_leaves_incomplete(
    create_setup, monkeypatch, tmp_path,
):
    from jiuwenswarm.server.runtime.session import session_metadata

    provisioner, host, state = create_setup
    original = session_metadata.init_session_metadata

    def publish(**kwargs):
        assert kwargs['session_id'] in host.owners
        assert not (tmp_path / 'created-session' / 'metadata.json').exists()
        state.events.append('metadata.publish')
        return original(**kwargs)

    monkeypatch.setattr(session_metadata, 'init_session_metadata', publish)
    prepared = await provisioner.prepare_session_create(_input(user_id='untrusted'))
    assert state.events.index('owner:created-session:True') < state.events.index('metadata.publish')
    assert all(len(call) == 3 for call in host.calls)  # no channel/user_id authority input
    await provisioner.abort_session_provision(prepared)
    await provisioner.abort_session_provision(prepared)
    assert prepared.state is SessionProvisionState.ABORTED
    assert host.rollbacks == ['created-session']
    assert not host.owners
    assert (tmp_path / 'created-session' / 'metadata.json').exists()
    # A leftover metadata file is never an invitation to reclaim its owner.
    with pytest.raises(PermissionError, match='unknown owner'):
        await provisioner.prepare_session_create(_input())
    assert host.calls[-1][2] is False


@pytest.mark.asyncio
async def test_create_commit_keeps_owner_and_existing_retry_only_verifies(create_setup):
    provisioner, host, _ = create_setup
    first = await provisioner.prepare_session_create(_input())
    await provisioner.commit_session_provision(
        first, timing=SessionProvisionCommitTiming.AFTER_RESULT_DELIVERY,
    )
    second = await provisioner.prepare_session_create(_input())
    assert not second.result.created
    await provisioner.abort_session_provision(second)
    assert host.owners == {'created-session'}
    assert host.rollbacks == []


@pytest.mark.asyncio
@pytest.mark.parametrize('existing_reservation', [False, True])
async def test_create_denied_owner_prevents_metadata_and_activation(
    create_setup, tmp_path, existing_reservation,
):
    provisioner, host, state = create_setup
    if existing_reservation:
        host.owners.add('created-session')
    else:
        host.denied.add('created-session')
    with pytest.raises(PermissionError):
        await provisioner.prepare_session_create(_input())
    assert not (tmp_path / 'created-session' / 'metadata.json').exists()
    assert 'activate:created-session' not in state.events
    assert state.released == ['created-session']
    assert host.rollbacks == []


@pytest.mark.asyncio
@pytest.mark.parametrize('error', [OSError('disk full'), asyncio.CancelledError('cancel')])
async def test_create_publication_failure_rolls_back_even_on_cancellation(
    create_setup, monkeypatch, error,
):
    from jiuwenswarm.server.runtime.session import session_metadata

    provisioner, host, state = create_setup

    def fail(**kwargs):
        raise error

    monkeypatch.setattr(session_metadata, 'init_session_metadata', fail)
    with pytest.raises(type(error)) as caught:
        await provisioner.prepare_session_create(_input())
    assert caught.value is error
    assert host.rollbacks == ['created-session']
    assert not host.owners
    assert state.released == ['created-session']


@pytest.mark.asyncio
async def test_cleanup_failure_keeps_primary_error_and_never_reports_success(
    create_setup, monkeypatch,
):
    from jiuwenswarm.server.runtime.session import session_metadata

    provisioner, host, state = create_setup
    from jiuwenswarm.runtime import session_provisioner as module

    log = Mock()
    monkeypatch.setattr(module, 'logger', log)
    primary = asyncio.CancelledError('cancel during publish')
    host.rollback_error = OSError('sidecar unavailable')

    def fail(**kwargs):
        raise primary

    monkeypatch.setattr(session_metadata, 'init_session_metadata', fail)
    with pytest.raises(asyncio.CancelledError) as caught:
        await provisioner.prepare_session_create(_input())
    assert caught.value is primary
    assert state.released == ['created-session']
    assert 'compensation incomplete' in str(log.warning.call_args_list)
    assert 'created-session' in str(log.warning.call_args_list)


@pytest.mark.asyncio
async def test_abort_failed_receipt_is_retryable_and_not_committed(create_setup):
    provisioner, host, state = create_setup
    prepared = await provisioner.prepare_session_create(_input())
    host.rollback_error = OSError('sidecar unavailable')
    with pytest.raises(OSError):
        await provisioner.abort_session_provision(prepared)
    assert prepared.state is SessionProvisionState.ABORTING
    assert state.released == ['created-session']
    host.rollback_error = None
    await provisioner.abort_session_provision(prepared)
    assert prepared.state is SessionProvisionState.ABORTED
    assert not host.owners


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['missing', 'async_hook', 'async_receipt'])
async def test_configured_hook_requires_synchronous_receipt(
    create_setup, tmp_path, kind,
):
    provisioner, _, _ = create_setup

    async def async_hook(*args):
        return lambda: None

    async def async_receipt():
        pass

    hooks = {
        'missing': lambda *args: None,
        'async_hook': async_hook,
        'async_receipt': lambda *args: async_receipt,
    }
    provisioner.set_owner_lifecycle(SimpleNamespace(before_publish=hooks[kind]))
    with pytest.raises(TypeError, match='synchronous'):
        await provisioner.prepare_session_create(_input())
    assert not (tmp_path / 'created-session' / 'metadata.json').exists()


@pytest.mark.asyncio
async def test_create_captures_host_and_revalidates_after_async_prepare(
    create_setup, monkeypatch,
):
    provisioner, host, _ = create_setup
    replacement = Host([])

    async def pause(**kwargs):
        provisioner.set_owner_lifecycle(replacement)
        host.denied.add('created-session')
        await asyncio.sleep(0)
        return None, None

    monkeypatch.setattr(provisioner, '_prepare_create_owner', pause)
    with pytest.raises(PermissionError, match='revoked'):
        await provisioner.prepare_session_create(_input())
    assert host.rollbacks == ['created-session']
    assert replacement.calls == []


@pytest.fixture
def fork_setup(create_setup, monkeypatch, tmp_path):
    from jiuwenswarm.agents.harness.common import session_ops_service as ops
    from jiuwenswarm.server.runtime.session import session_metadata

    provisioner, host, state = create_setup
    host.owners.add('fork-source')
    monkeypatch.setattr(session_metadata, 'get_session_metadata', lambda *a, **k: {'project_id': 'p'})
    allocated = AsyncMock(return_value='fork-target')
    provisioner._agent_manager.create_session = allocated
    provisioner._agent_manager.get_agent_nowait = lambda channel: None

    def publish(**kwargs):
        assert 'fork-target' in host.owners
        state.events.append('fork.publish')
        (tmp_path / 'fork-target').mkdir(exist_ok=True)
        return {'session_id': 'fork-target', 'source_session_id': 'fork-source'}

    monkeypatch.setattr(ops, 'fork_session', publish)
    monkeypatch.setattr(ops, 'copy_session_state', AsyncMock())
    monkeypatch.setattr(ops, 'copy_session_context', AsyncMock())
    return provisioner, host, state, ops, allocated


def fork_input():
    return SessionForkInput(channel_id='web', source_session_id='fork-source')


@pytest.mark.asyncio
async def test_fork_denied_source_precedes_allocation_and_history_read(fork_setup):
    provisioner, host, state, _, allocated = fork_setup
    host.denied.add('fork-source')
    with pytest.raises(PermissionError):
        await provisioner.prepare_session_fork(fork_input())
    allocated.assert_not_awaited()
    assert 'fork.publish' not in state.events


@pytest.mark.asyncio
async def test_fork_target_registered_before_publish_and_abort_rolls_back(fork_setup, tmp_path):
    provisioner, host, state, _, _ = fork_setup
    prepared = await provisioner.prepare_session_fork(fork_input())
    assert state.events.index('owner:fork-target:True') < state.events.index('fork.publish')
    await provisioner.abort_session_provision(prepared)
    assert host.owners == {'fork-source'}
    assert (tmp_path / 'fork-target').is_dir()  # owned receipt, not recursive deletion


@pytest.mark.asyncio
@pytest.mark.parametrize('phase', ['allocate', 'ensure', 'copy', 'state', 'commit'])
@pytest.mark.parametrize('target', ['fork-source', 'fork-target'])
async def test_fork_rechecks_revocation_across_async_boundaries(
    fork_setup, monkeypatch, phase, target,
):
    provisioner, host, state, ops, allocated = fork_setup

    async def revoke(*args, **kwargs):
        host.denied.add(target)
        await asyncio.sleep(0)
        return SimpleNamespace(card=SimpleNamespace())

    if phase == 'allocate':
        async def allocate(**kwargs):
            await revoke()
            return 'fork-target'
        allocated.side_effect = allocate
    elif phase == 'ensure':
        provisioner._agent_manager.get_agent_nowait = lambda channel: SimpleNamespace(ensure_instance=revoke)
    elif phase == 'copy':
        provisioner._agent_manager.get_agent_nowait = lambda channel: SimpleNamespace(
            ensure_instance=AsyncMock(return_value=SimpleNamespace(card=SimpleNamespace())),
        )
        monkeypatch.setattr(ops, 'copy_session_context', revoke)
    elif phase == 'state':
        monkeypatch.setattr(ops, 'copy_session_state', revoke)

    if phase == 'commit':
        prepared = await provisioner.prepare_session_fork(fork_input())
        host.denied.add(target)
        with pytest.raises(PermissionError):
            await provisioner.commit_session_provision(
                prepared, timing=SessionProvisionCommitTiming.BEFORE_RESULT_DELIVERY,
            )
        assert prepared.state is not SessionProvisionState.COMMITTED
        assert host.rollbacks == ['fork-target']
    else:
        with pytest.raises(PermissionError):
            await provisioner.prepare_session_fork(fork_input())
    if phase == 'allocate':
        assert 'fork.publish' not in state.events
    assert host.owners == {'fork-source'}


@pytest.mark.asyncio
async def test_fork_copy_cancel_compensates_and_preserves_original(fork_setup, monkeypatch):
    provisioner, host, _, ops, _ = fork_setup
    from jiuwenswarm.runtime import session_provisioner as module

    log = Mock()
    monkeypatch.setattr(module, 'logger', log)
    primary = asyncio.CancelledError('copy cancelled')
    host.rollback_error = RuntimeError('cleanup failed')
    monkeypatch.setattr(ops, 'copy_session_state', AsyncMock(side_effect=primary))
    with pytest.raises(asyncio.CancelledError) as caught:
        await provisioner.prepare_session_fork(fork_input())
    assert caught.value is primary
    assert host.rollbacks == ['fork-target']
    assert 'compensation incomplete' in str(log.warning.call_args_list)


@pytest.mark.asyncio
async def test_commit_rejection_cannot_revive_after_rights_restore(fork_setup):
    provisioner, host, _, _, _ = fork_setup
    prepared = await provisioner.prepare_session_fork(fork_input())
    host.denied.add('fork-source')
    host.rollback_error = OSError('temporary cleanup outage')
    with pytest.raises(PermissionError) as first:
        await provisioner.commit_session_provision(
            prepared, timing=SessionProvisionCommitTiming.BEFORE_RESULT_DELIVERY,
        )
    # Both principals now pass again, but failed commit may only retry cleanup.
    host.denied.clear()
    host.rollback_error = None
    with pytest.raises(PermissionError) as second:
        await provisioner.commit_session_provision(
            prepared, timing=SessionProvisionCommitTiming.BEFORE_RESULT_DELIVERY,
        )
    assert second.value is first.value
    assert prepared.state is SessionProvisionState.COMMITTING
    assert host.owners == {'fork-source'}


@pytest.mark.asyncio
async def test_abort_preserves_owner_failure_if_claim_release_also_fails(create_setup):
    provisioner, host, _ = create_setup
    prepared = await provisioner.prepare_session_create(_input())
    primary = OSError('owner abort failed')
    host.rollback_error = primary
    provisioner._agent_manager.release_session_prewarm_claim = AsyncMock(
        side_effect=RuntimeError('claim cleanup failed'),
    )
    with pytest.raises(OSError) as caught:
        await provisioner.abort_session_provision(prepared)
    assert caught.value is primary
    assert prepared.state is SessionProvisionState.ABORTING


@pytest.mark.asyncio
async def test_existing_fork_target_never_claims_unknown_owner(fork_setup, tmp_path):
    provisioner, host, state, _, _ = fork_setup
    (tmp_path / 'fork-target').mkdir()
    with pytest.raises(PermissionError, match='unknown owner'):
        await provisioner.prepare_session_fork(fork_input())
    assert host.calls[-1] == ('fork-target', 'p', False)
    assert host.owners == {'fork-source'}
    assert 'fork.publish' not in state.events


@pytest.mark.asyncio
async def test_fork_publish_failure_rolls_back_before_exposing_result(fork_setup, monkeypatch):
    provisioner, host, _, ops, _ = fork_setup
    primary = OSError('history write failed')
    monkeypatch.setattr(ops, 'fork_session', Mock(side_effect=primary))
    with pytest.raises(OSError) as caught:
        await provisioner.prepare_session_fork(fork_input())
    assert caught.value is primary
    assert host.rollbacks == ['fork-target']
    assert host.owners == {'fork-source'}


@pytest.mark.asyncio
async def test_successful_fork_commit_keeps_receipt_owner(fork_setup):
    provisioner, host, _, _, _ = fork_setup
    prepared = await provisioner.prepare_session_fork(fork_input())
    await provisioner.commit_session_provision(
        prepared, timing=SessionProvisionCommitTiming.BEFORE_RESULT_DELIVERY,
    )
    assert prepared.state is SessionProvisionState.COMMITTED
    assert host.owners == {'fork-source', 'fork-target'}
    assert host.rollbacks == []
