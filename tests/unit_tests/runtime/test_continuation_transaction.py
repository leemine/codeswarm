"""B3 transaction with real Runtime/Provisioner/claim lock and durable sidecar.

Only allocation and external model configuration are synthetic. No Provider is
started: these tests prove transaction facts, not actual Provider acceptance.
"""
import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from jiuwenswarm.governance.continuation_publication import ContinuationPublication
from jiuwenswarm.governance.preparation import GovernanceError
from jiuwenswarm.governance.resources import ResourceDefinition
from jiuwenswarm.governance.session_sharing import SessionSharingConflict
from jiuwenswarm.runtime import AgentRuntime
from jiuwenswarm.runtime.session_provisioner import SessionProvisionState
from jiuwenswarm.server.runtime.agent_manager import AgentManager
from jiuwenswarm.server.runtime.agent_warm_pool import AgentWarmPool
from jiuwenswarm.server.runtime.session import continuation_publication, lifecycle, session_metadata
from tests.unit_tests.server import test_continuation_source as source

setup = source.setup
ALICE, BOB, CAROL = source.ALICE, source.BOB, source.CAROL


@pytest.fixture
async def transaction(setup, monkeypatch):
    from jiuwenswarm.common import config, utils
    from jiuwenswarm.governance import session_boundary
    from jiuwenswarm.agents.harness.common import session_ops_service

    assert session_metadata.flush_pending_writes()
    root = setup.tmp_path / 'sessions'
    monkeypatch.setattr(utils, 'get_agent_sessions_dir', lambda: root)
    monkeypatch.setattr(session_metadata, 'get_agent_sessions_dir', lambda: root)
    monkeypatch.setattr(session_boundary, 'organization_sharing_host', lambda: setup.host)
    (setup.tmp_path / 'target').mkdir()
    catalog = {'execution': {'default_profile_id': 'native', 'profiles': {
        'native': {'provider_id': 'native', 'config_revision': 'test-v1',
                   'requested_mode': 'normal', 'provider_config': {}}}},
        'permissions': {'enabled': True}, 'models': {'defaults': [{
            'model_client_config': {'model_name': 'synthetic', 'client_provider': 'OpenAI',
                'api_base': 'https://models.example/v1', 'credential_reference': 'model-account:bob',
                'credential_encoding': 'plain', 'api_key': 'SYNTHETIC-UNCONSUMED'},
            'model_config_obj': {'model_name': 'synthetic', 'temperature': 0}}]}}
    monkeypatch.setattr(config, 'get_config_raw', lambda: catalog)
    monkeypatch.setattr(config, 'get_config', lambda: catalog)
    for revision, item in enumerate((
        ResourceDefinition('workspace', 'workspace', str(setup.tmp_path / 'target')),
        ResourceDefinition('model', 'credential', 'model-account:bob'),
    )):
        setup.access.register_resource(setup.target.project_id, item, owner_subject_id=BOB.actor_id,
            actions=('read',) if revision == 0 else ('use',), expected_revision=revision * 2,
            delegable=True)
        setup.access.grant_resource(setup.target.project_id, replace(BOB, subject_id=BOB.actor_id),
            item.resource_id, subject_id=BOB.subject_id,
            actions=('read',) if revision == 0 else ('use',), expected_revision=revision * 2 + 1)
    allocated, released = [], []
    async def allocate(**kwargs):
        sid = f'continued-{len(allocated) + 1}'
        allocated.append(sid)
        return sid
    async def release(sid):
        released.append(sid)
    manager = object.__new__(AgentManager)
    manager._session_create_token_lock = asyncio.Lock()
    manager._session_create_tokens = {}
    manager.create_session = AsyncMock(side_effect=allocate)
    manager.cancel_all_inflight_work = AsyncMock()
    manager.cleanup = AsyncMock()
    manager.warm_pool = SimpleNamespace(make_key=AgentWarmPool.make_key,
        claim=AsyncMock(side_effect=AssertionError('Provider prewarm forbidden')),
        clear_marker=Mock(), release_claim_pin=release)
    monkeypatch.setattr(session_ops_service, 'fork_session', Mock(side_effect=AssertionError('fork forbidden')))
    monkeypatch.setattr(session_ops_service, 'copy_session_state', AsyncMock(side_effect=AssertionError('state copy forbidden')))
    monkeypatch.setattr(session_ops_service, 'copy_session_context', AsyncMock(side_effect=AssertionError('context copy forbidden')))
    runtime = AgentRuntime(agent_manager=manager, initializer=AsyncMock(),
        plan_controller=SimpleNamespace(reset_session=lambda _: None),
        trusted_identity_resolver=lambda _: setup.identities[0],
        project_authorizer=setup.access, resource_authorizer=setup.access)
    request = replace(setup.request, model_name='synthetic#0', mode='agent.work.normal')
    receipts = []
    original_prepare = runtime.prepare_session_create
    async def prepare(params):
        prepared = await original_prepare(params)
        receipts.append(prepared)
        return prepared
    monkeypatch.setattr(runtime, 'prepare_session_create', prepare)
    yield SimpleNamespace(**locals())
    await runtime.close()
    assert session_metadata.flush_pending_writes()
    for sid in allocated:
        session_metadata.remove_session_metadata_cache(sid)


def owner(tx, sid=None):
    sid = sid or tx.allocated[-1]
    return tx.setup.access._load()['session_sharing']['owners'][sid]


async def create(tx, request=None):
    if not tx.runtime._started:
        await tx.runtime.start()
    return await tx.runtime.continue_session(request or tx.request)


@pytest.mark.asyncio
async def test_real_publication_order_and_private_seed(transaction, monkeypatch):
    tx = transaction
    events = []
    original_flush = session_metadata.flush_pending_writes
    def flush():
        result = original_flush()
        events.append('metadata-durable')
        return result
    original_write = ContinuationPublication.write_seed
    def write(scope):
        assert events[-1] == 'metadata-durable'
        assert lifecycle.raw_metadata(scope.session_id)['model'] == 'synthetic#0'
        assert owner(tx)['continuation']['state'] == 'pending'
        events.append('seed')
        return original_write(scope)
    original_commit = ContinuationPublication.commit
    def commit(scope):
        assert tx.receipts[-1]._finalize_lock.locked()
        assert tx.receipts[-1].state is SessionProvisionState.COMMITTING
        assert (tx.root / scope.session_id / 'continuation.json').is_file()
        events.append('publication')
        return original_commit(scope)
    monkeypatch.setattr(session_metadata, 'flush_pending_writes', flush)
    monkeypatch.setattr(ContinuationPublication, 'write_seed', write)
    monkeypatch.setattr(ContinuationPublication, 'commit', commit)
    source_before = tx.setup.path.read_bytes()
    result = await create(tx)
    result.revalidate()
    assert events == ['metadata-durable', 'seed', 'publication']
    assert owner(tx)['continuation']['state'] == 'committed'
    assert tx.receipts[-1].state is SessionProvisionState.COMMITTED
    assert tx.runtime._owns_session(result.session_id)
    assert tx.setup.host.owner_current(result.session_id, BOB)
    assert not tx.setup.host.owner_current(result.session_id, ALICE)
    seed = continuation_publication.read_seed(tx.setup.host, result.session_id, BOB)
    assert [m.content for m in seed.messages] == [f'text-{i}' for i in range(205)] + ['final']
    raw = (tx.root / result.session_id / 'continuation.json').read_text()
    assert all(secret not in raw for secret in ('child-secret', 'never-copy', 'not-context', 'SYNTHETIC-UNCONSUMED'))
    assert tx.setup.path.read_bytes() == source_before
    assert tx.manager.warm_pool.claim.await_count == 0
    assert tx.allocated == [result.session_id] and tx.released == []
    payload = result.to_payload()
    assert payload['continued_from']['share_id'] == tx.request.share_id
    assert 'credential' not in json.dumps(payload) and 'seed' not in payload


@pytest.mark.asyncio
async def test_lost_response_retries_committed_session_without_allocation(transaction, monkeypatch):
    tx = transaction
    original = tx.runtime._register_session
    first = True
    async def lost(**kwargs):
        nonlocal first
        await original(**kwargs)
        if first:
            first = False
            raise ConnectionError('response lost after business commit')
    monkeypatch.setattr(tx.runtime, '_register_session', lost)
    with pytest.raises(ConnectionError, match='response lost'):
        await create(tx)
    assert owner(tx)['continuation']['state'] == 'committed'
    assert tx.released == []
    result = await create(tx)
    result.revalidate()
    assert tx.allocated == [result.session_id]
    assert not tx.runtime._pending_session_provisions


@pytest.mark.asyncio
@pytest.mark.parametrize('phase', ['metadata', 'seed', 'publication'])
async def test_failure_before_business_commit_compensates_original_receipt(transaction, monkeypatch, phase):
    tx = transaction
    original_flush = session_metadata.flush_pending_writes
    if phase == 'metadata':
        monkeypatch.setattr(session_metadata, 'flush_pending_writes', lambda: False)
    elif phase == 'seed':
        monkeypatch.setattr(ContinuationPublication, 'write_seed', Mock(side_effect=OSError('seed disk')))
    else:
        monkeypatch.setattr(ContinuationPublication, 'commit', Mock(side_effect=OSError('publication disk')))
    with pytest.raises((PermissionError, OSError)):
        await create(tx)
    assert tx.released == tx.allocated
    assert owner(tx)['retired'] is True
    assert not tx.setup.host.owner_current(tx.allocated[0], BOB)
    assert not tx.runtime._pending_session_provisions
    if phase == 'metadata':
        monkeypatch.setattr(session_metadata, 'flush_pending_writes', original_flush)


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['title', 'model', 'share'])
async def test_same_token_different_input_never_reuses_private_session(transaction, change):
    tx = transaction
    await create(tx)
    if change == 'title':
        request = replace(tx.request, title='different')
    elif change == 'model':
        request = replace(tx.request, model_name='unknown#0')
    else:
        grant = tx.setup.host.store.grant('source-session', ALICE, BOB, actions={'view', 'execute'},
            history=tx.setup.scope, expires_at=200)
        request = replace(tx.request, share_id=grant['share_id'])
    with pytest.raises((PermissionError, SessionSharingConflict)):
        await create(tx, request)
    assert len(tx.allocated) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['source', 'target', 'identity', 'expiry', 'resource', 'config'])
async def test_delivery_and_same_token_retry_recheck_current_authority(transaction, change):
    tx = transaction
    result = await create(tx)
    if change == 'source':
        tx.setup.host.store.revoke(tx.request.share_id, ALICE, expected_revision=1)
    elif change == 'target':
        tx.setup.access.replace_acl(tx.setup.target.project_id, 'admin', acl={'bob': ['read']}, expected_revision=2)
    elif change == 'identity':
        tx.setup.identities[0] = replace(BOB, subject_id='another-login')
    elif change == 'expiry':
        tx.setup.clock[0] = 200
    elif change == 'resource':
        tx.setup.access.revoke_resource(tx.setup.target.project_id, BOB, 'model',
            subject_id=BOB.subject_id, expected_revision=4)
    else:
        tx.catalog['execution']['profiles']['native']['config_revision'] = 'changed'
    with pytest.raises((PermissionError, ValueError)):
        result.revalidate()
    with pytest.raises((PermissionError, ValueError)):
        await create(tx)
    assert len(tx.allocated) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('phase', ['after_prepare', 'after_seed', 'commit_lock'])
@pytest.mark.parametrize('change', ['source', 'target', 'identity'])
async def test_revocation_at_transaction_windows_compensates(transaction, monkeypatch, phase, change):
    tx = transaction
    def revoke():
        if change == 'source':
            tx.setup.host.store.revoke(tx.request.share_id, ALICE, expected_revision=1)
        elif change == 'target':
            tx.setup.access.replace_acl(tx.setup.target.project_id, 'admin', acl={'bob': ['read']}, expected_revision=2)
        else:
            tx.setup.identities[0] = replace(BOB, subject_id='different')
    if phase == 'after_prepare':
        original = tx.runtime.prepare_session_create
        async def prepare(params):
            receipt = await original(params)
            revoke()
            return receipt
        monkeypatch.setattr(tx.runtime, 'prepare_session_create', prepare)
    elif phase == 'after_seed':
        original = ContinuationPublication.write_seed
        def write(scope):
            original(scope)
            revoke()
        monkeypatch.setattr(ContinuationPublication, 'write_seed', write)
    else:
        # Hold the existing finalize lock, then revoke while its caller awaits.
        original = tx.runtime.commit_session_provision
        async def commit(receipt, **kwargs):
            await receipt._finalize_lock.acquire()
            async def release():
                await asyncio.sleep(0)
                revoke()
                receipt._finalize_lock.release()
            mutation = asyncio.create_task(release())
            try:
                return await original(receipt, **kwargs)
            finally:
                await mutation
        monkeypatch.setattr(tx.runtime, 'commit_session_provision', commit)
    with pytest.raises((PermissionError, GovernanceError)):
        await create(tx)
    assert owner(tx)['retired'] is True
    assert tx.released == tx.allocated
    assert not tx.runtime._pending_session_provisions


@pytest.mark.asyncio
async def test_second_authenticated_actor_same_token_gets_independent_session(transaction):
    tx = transaction
    first = await create(tx)
    tx.setup.access.replace_acl(tx.setup.target.project_id, 'admin',
        acl={'bob': ['read', 'execute'], 'carol': ['read', 'execute']}, expected_revision=2)
    for index, resource in enumerate(('workspace', 'model')):
        # Carol has an explicit actor grant and a delegated subject grant.
        tx.setup.access.grant_resource(tx.setup.target.project_id, replace(BOB, subject_id=BOB.actor_id),
            resource, subject_id=CAROL.actor_id, actions=('read',) if index == 0 else ('use',),
            expected_revision=4 + index * 2, delegable=True)
        tx.setup.access.grant_resource(tx.setup.target.project_id, replace(CAROL, subject_id=CAROL.actor_id),
            resource, subject_id=CAROL.subject_id, actions=('read',) if index == 0 else ('use',),
            expected_revision=5 + index * 2)
    share = tx.setup.host.store.grant('source-session', ALICE, CAROL, actions={'view', 'execute'},
        history=tx.setup.scope, expires_at=200)
    tx.setup.identities[0] = CAROL
    second = await create(tx, replace(tx.request, share_id=share['share_id']))
    assert second.session_id != first.session_id
    assert tx.setup.host.owner_current(second.session_id, CAROL)
    assert not tx.setup.host.owner_current(second.session_id, BOB)
    assert len(tx.manager._session_create_tokens) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('field', ['api_base', 'temperature'])
async def test_same_model_name_cannot_reapprove_changed_catalog_on_retry(transaction, field):
    tx = transaction
    await create(tx)
    model = tx.catalog['models']['defaults'][0]
    if field == 'api_base':
        model['model_client_config'][field] = 'https://changed.example/v1'
    else:
        model['model_config_obj'][field] = 0.7
    with pytest.raises(PermissionError):
        await create(tx)
    assert len(tx.allocated) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('revoke_source', [False, True])
async def test_commit_failure_cleanup_outage_preserves_owned_retry(transaction, monkeypatch, revoke_source):
    tx = transaction
    primary = OSError('publication unavailable')
    monkeypatch.setattr(ContinuationPublication, 'commit', Mock(side_effect=primary))
    rollback = tx.setup.host.compensate_owner_registration
    def unavailable(*args, **kwargs):
        raise OSError('temporary sidecar outage')
    monkeypatch.setattr(tx.setup.host, 'compensate_owner_registration', unavailable)
    with pytest.raises(OSError) as caught:
        await create(tx)
    assert caught.value is primary
    # A failed compensation must remain represented by an owned outstanding
    # receipt; otherwise the terminal commit path silently loses its cleanup.
    receipt = tx.receipts[-1]
    monkeypatch.setattr(tx.setup.host, 'compensate_owner_registration', rollback)
    assert receipt.state is SessionProvisionState.ABORTING
    assert receipt in tx.runtime._pending_session_provisions
    if revoke_source:
        tx.setup.host.store.revoke(tx.request.share_id, ALICE, expected_revision=1)
    await tx.runtime.abort_session_provision(receipt)
    assert receipt.state is SessionProvisionState.ABORTED
    assert not tx.runtime._pending_session_provisions
    assert owner(tx)['retired'] is True
    assert tx.released == tx.allocated


@pytest.mark.asyncio
async def test_concurrent_same_token_pending_cannot_hijack_first_scope(transaction, monkeypatch):
    tx = transaction
    await tx.runtime.start()
    entered, release = asyncio.Event(), asyncio.Event()
    original = tx.runtime.prepare_session_create
    async def paused(params):
        receipt = await original(params)
        entered.set()
        await release.wait()
        return receipt
    monkeypatch.setattr(tx.runtime, 'prepare_session_create', paused)
    first = asyncio.create_task(tx.runtime.continue_session(tx.request))
    await entered.wait()
    try:
        with pytest.raises(SessionSharingConflict, match='pending'):
            await tx.runtime.continue_session(tx.request)
        assert not tx.setup.host.owner_current(tx.allocated[0], BOB)
        assert owner(tx)['continuation']['state'] == 'pending'
    finally:
        release.set()
    result = await first
    result.revalidate()
    assert tx.allocated == [result.session_id]
    assert tx.released == []


@pytest.mark.asyncio
async def test_restart_retry_uses_durable_result_not_manager_token_cache(transaction):
    tx = transaction
    first = await create(tx)
    await tx.runtime.close()
    tx.manager._session_create_tokens.clear()
    second_runtime = AgentRuntime(agent_manager=tx.manager, initializer=AsyncMock(),
        plan_controller=SimpleNamespace(reset_session=lambda _: None),
        trusted_identity_resolver=lambda _: tx.setup.identities[0],
        project_authorizer=tx.setup.access, resource_authorizer=tx.setup.access)
    await second_runtime.start()
    try:
        second = await second_runtime.continue_session(tx.request)
        second.revalidate()
        assert second.to_payload() == first.to_payload()
        assert tx.allocated == [first.session_id]
    finally:
        await second_runtime.close()
