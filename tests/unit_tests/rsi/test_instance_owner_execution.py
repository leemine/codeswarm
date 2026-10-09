"""Instance-owner admission and real Worker cancellation/queue boundaries."""
import asyncio
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from jiuwenswarm.agents.harness.common.rsi import build_rsi_service_context
from jiuwenswarm.agents.harness.common.rsi.models import RsiTask
from jiuwenswarm.governance.application_boundary import admit_application_request
from jiuwenswarm.governance.contracts import TrustedIdentity
from jiuwenswarm.governance.rsi_boundary import RSI_OWNER_METHODS, RSI_TASK_OPERATIONS, is_instance_owner
from jiuwenswarm.governance.session_boundary import delivery_scope, set_delivery_permit

OWNER = TrustedIdentity('owner', 'owner', 'test:instance')
OTHER = TrustedIdentity('other', 'other', 'test:instance')


@pytest.fixture
def setup(tmp_path, monkeypatch):
    ctx = build_rsi_service_context(tmp_path)
    config = {'instance_owner': asdict(OWNER)}
    auth = SimpleNamespace(known_actor=lambda i: i in (OWNER, OTHER), _config=lambda: config)
    monkeypatch.setattr('jiuwenswarm.governance.organization_auth.configured_authenticator', lambda: auth)
    for name, identity in [('own', OWNER), ('foreign', OTHER), ('next', OWNER)]:
        ctx.store.create(RsiTask(task_id='rsi-'+name, name=name, scenario='HARNESS',
            status='CREATED', created_at='2026-10-09', owner_identity=asdict(identity)))
    return ctx, config


@pytest.mark.parametrize('method', sorted(RSI_OWNER_METHODS))
def test_only_explicit_instance_owner_can_use_execution_methods(setup, method):
    ctx, config = setup
    params = {'task_id': 'rsi-own'} if method in RSI_TASK_OPERATIONS else {}
    permit = admit_application_request(method, params, identity_resolver=lambda: OWNER, rsi_store=ctx.store)
    assert permit.revalidate()
    with pytest.raises(PermissionError):
        admit_application_request(method, params, identity_resolver=lambda: OTHER, rsi_store=ctx.store,
                                  policy_supplier=lambda _: {'settings': ['manage'], 'rsi': ['manage']})
    config.clear()
    assert not permit.revalidate()


def test_instance_owner_cannot_take_foreign_experiments(setup):
    ctx, _ = setup
    for method in RSI_TASK_OPERATIONS:
        with pytest.raises(PermissionError):
            admit_application_request(method, {'task_id': 'rsi-foreign'},
                                      identity_resolver=lambda: OWNER, rsi_store=ctx.store)


def test_full_identity_and_no_implicit_owner(setup):
    _, config = setup
    assert is_instance_owner(OWNER)
    assert not is_instance_owner(TrustedIdentity('owner', 'owner', 'other'))
    config['instance_owner'] = 'owner'
    assert not is_instance_owner(OWNER)


def test_delete_retains_receipt_but_not_authority_over_replacement(setup):
    ctx, _ = setup
    permit = admit_application_request('rsi.task.delete', {'task_id': 'rsi-own'},
                                      identity_resolver=lambda: OWNER, rsi_store=ctx.store)
    ctx.store.delete('rsi-own')
    assert permit.revalidate()
    ctx.store.create(RsiTask(task_id='rsi-own', name='replacement', scenario='HARNESS',
                            status='CREATED', created_at='later', owner_identity=asdict(OTHER)))
    assert not permit.revalidate()


def enqueue(ctx, task_id, identity_resolver=lambda: OWNER):
    with delivery_scope():
        permit = admit_application_request('rsi.training.start', {'task_id': task_id},
                                          identity_resolver=identity_resolver, rsi_store=ctx.store)
        set_delivery_permit(permit)
        ctx.worker.enqueue(task_id)


async def close_worker(ctx):
    ctx.worker._run_task.cancel()
    await asyncio.gather(ctx.worker._run_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_queued_work_rechecks_before_invoking_provider(setup, monkeypatch):
    ctx, config = setup
    original = ctx.worker._ensure_runner
    monkeypatch.setattr(ctx.worker, '_ensure_runner', lambda: None)
    enqueue(ctx, 'rsi-own')
    config.clear()
    original()
    await asyncio.wait_for(ctx.worker._queue.join(), 2)
    assert ctx.store.get('rsi-own').status == 'TERMINATED'
    await close_worker(ctx)


@pytest.mark.asyncio
async def test_revocation_waits_for_actual_harness_exit(setup):
    ctx, _ = setup
    started, cancelling, cleanup = asyncio.Event(), asyncio.Event(), asyncio.Event()
    identity = [OWNER]
    class Adapter:
        supports_terminate = False
        def build_request(self, task, **kw): return task
        async def run(self, request, **kw):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelling.set()
                await cleanup.wait()
    ctx.worker.adapters['HARNESS'] = Adapter()
    enqueue(ctx, 'rsi-own', lambda: identity[0])
    await asyncio.wait_for(started.wait(), 2)
    identity[0] = None
    await asyncio.wait_for(cancelling.wait(), 2)
    assert ctx.store.get('rsi-own').status == 'RUNNING'
    assert not ctx.worker._execution_runners['rsi-own'].done()
    cleanup.set()
    await asyncio.wait_for(ctx.worker._queue.join(), 2)
    assert ctx.store.get('rsi-own').status == 'TERMINATED'
    assert not ctx.worker._execution_authorities
    await close_worker(ctx)


@pytest.mark.asyncio
async def test_next_task_keeps_its_own_principal_after_first_credential_revoked(setup):
    ctx, _ = setup
    first, release = asyncio.Event(), asyncio.Event()
    calls = []
    class Adapter:
        supports_terminate = False
        def build_request(self, task, **kw): return task
        async def run(self, request, **kw):
            calls.append(request.task_id)
            if request.task_id == 'rsi-own':
                first.set()
                await release.wait()
            return SimpleNamespace(status='COMPLETED')
    ctx.worker.adapters['HARNESS'] = Adapter()
    identity = [OWNER]
    enqueue(ctx, 'rsi-own', lambda: identity[0])
    await asyncio.wait_for(first.wait(), 2)
    enqueue(ctx, 'rsi-next')
    identity[0] = None
    await asyncio.wait_for(ctx.worker._queue.join(), 3)
    assert calls == ['rsi-own', 'rsi-next']
    assert ctx.store.get('rsi-own').status == 'TERMINATED'
    assert ctx.store.get('rsi-next').status == 'COMPLETED'
    await close_worker(ctx)
