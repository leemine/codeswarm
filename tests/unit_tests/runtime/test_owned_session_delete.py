"""Real owner sidecar/lifecycle/Runtime/Provisioner; synthetic Provider exit only."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.governance.session_sharing import SessionSharingDenied
from jiuwenswarm.server.runtime.session import lifecycle as lc
from jiuwenswarm.server.runtime.session.session_archive import SessionArchiveService
from tests.unit_tests.runtime import test_continuation_transaction as txns
from tests.unit_tests.runtime.test_runtime_session_provisioner import Lifecycle

setup = txns.setup
transaction = txns.transaction


@pytest.fixture
async def deletion(transaction, monkeypatch):
    tx = transaction
    result = await txns.create(tx)
    sid = result.session_id
    tx.manager.stop_existing_session_runtime = AsyncMock()
    tx.manager.release_subagent_runtime_for_session = AsyncMock()
    tx.manager.cleanup_session_runtime = AsyncMock(return_value=True)
    tx.manager.get_agent_for_session_nowait = lambda *_: None
    tx.runtime._forget_agent_execution_owner = AsyncMock()
    tx.runtime._session_provisioner._delete_lifecycle = Lifecycle([])
    monkeypatch.setattr('jiuwenswarm.server.runtime.agent_adapter.interface_deep.ensure_persistent_checkpointer', AsyncMock())
    for name in ('begin', 'commit', 'abort'):
        monkeypatch.setattr(f'jiuwenswarm.observability.session_delete.{name}_trajectory_session_delete', lambda *_: None)
    release = AsyncMock()
    monkeypatch.setattr('openjiuwen.core.runner.Runner.release', release)
    service = SessionArchiveService(tx.runtime)
    def request():
        return AgentRequest(request_id='delete-original', session_id=sid, channel_id='web',
                            req_method=ReqMethod.SESSION_DELETE, params={'session_id': sid})
    async def run():
        req = request()
        authority = tx.runtime.prepare_session_deletion(req)
        return await service.session(sid, 'delete', 'web', _deletion_authority=authority)
    yield SimpleNamespace(**locals())
    await service.close()


@pytest.mark.asyncio
async def test_revoked_source_owner_deletes_through_actual_transaction(deletion):
    d = deletion
    d.tx.setup.host.store.revoke(d.tx.request.share_id, txns.ALICE, expected_revision=1)
    assert not d.tx.setup.host.owner_current(d.sid, txns.BOB)
    result = await d.run()
    assert result == {'session_id': d.sid, 'ok': True, 'deleted': True, 'exit_confirmed': True}
    assert not (d.tx.root / d.sid).exists()
    assert txns.owner(d.tx)['retired'] is True
    assert lc.state('session', d.sid)['deleted'] is True
    d.tx.manager.stop_existing_session_runtime.assert_awaited_once()
    d.release.assert_awaited_once_with(d.sid)
    assert not d.tx.setup.host.owner_current(d.sid, txns.BOB)
    assert await d.run() == result
    d.release.assert_awaited_once()


@pytest.mark.asyncio
async def test_direct_runtime_uses_original_archive_locks_and_receipt(deletion):
    result = await deletion.tx.runtime.delete_session(channel_id='web', session_id=deletion.sid)
    assert result.ok and result.deleted
    assert lc.state('session', deletion.sid)['deleted'] is True
    assert txns.owner(deletion.tx)['retired'] is True


@pytest.mark.asyncio
async def test_provider_exit_failure_preserves_directory_binding_and_retry(deletion):
    d = deletion
    d.tx.manager.stop_existing_session_runtime.side_effect = RuntimeError('exit not confirmed')
    with pytest.raises(Exception, match='exit not confirmed'):
        await d.run()
    assert (d.tx.root / d.sid).exists()
    d.tx.manager.cleanup_session_runtime.assert_not_awaited()
    d.release.assert_not_awaited()
    first = txns.owner(d.tx)['source']['epoch']
    d.tx.manager.stop_existing_session_runtime.side_effect = None
    assert (await d.run())['exit_confirmed'] is True
    assert txns.owner(d.tx)['source']['epoch'] == first


@pytest.mark.asyncio
@pytest.mark.parametrize('phase', ['observer', 'retire', 'lifecycle'])
async def test_metadata_less_commit_retry_keeps_original_project_and_operation(deletion, monkeypatch, phase):
    d = deletion
    if phase == 'observer':
        obj = d.tx.runtime._session_provisioner._delete_lifecycle
        original = obj.commit_session_delete
        monkeypatch.setattr(obj, 'commit_session_delete', AsyncMock(side_effect=OSError('observer unavailable')))
    elif phase == 'retire':
        obj = d.tx.setup.host
        original = obj.commit_deletion
        monkeypatch.setattr(obj, 'commit_deletion', lambda *_: (_ for _ in ()).throw(OSError('retire unavailable')))
    else:
        obj = lc
        original = lc.complete
        monkeypatch.setattr(lc, 'complete', lambda *_a, **_k: (_ for _ in ()).throw(OSError('lifecycle unavailable')))
    with pytest.raises(Exception):
        await d.run()
    assert not (d.tx.root / d.sid).exists()
    before = txns.owner(d.tx)['deletion']
    if phase == 'observer':
        monkeypatch.setattr(obj, 'commit_session_delete', original)
    elif phase == 'retire':
        monkeypatch.setattr(obj, 'commit_deletion', original)
    else:
        monkeypatch.setattr(lc, 'complete', original)
    # A new service claims the same persistent operation; no metadata fallback
    # to the default project or new Provider allocation is permitted.
    d.service._owner_id = 'replacement-service'
    result = await d.run()
    assert result['exit_confirmed'] is True
    after = txns.owner(d.tx)['deletion']
    assert after['deletion_id'] == before['deletion_id']
    assert after['operation_id'] == before['operation_id']
    assert after['source_epoch'] == before['source_epoch']
    assert after['binding']['project_id'] == d.tx.setup.target.project_id
    d.release.assert_awaited_once()


@pytest.mark.asyncio
async def test_identity_change_while_waiting_original_project_lock_prevents_lifecycle(deletion, monkeypatch):
    from contextlib import asynccontextmanager
    d = deletion
    original = d.service.lock
    @asynccontextmanager
    async def lock(kind, resource):
        async with original(kind, resource):
            if kind == 'project':
                d.tx.setup.identities[0] = txns.ALICE
            yield
    monkeypatch.setattr(d.service, 'lock', lock)
    with pytest.raises(PermissionError):
        await d.run()
    assert not lc.state('session', d.sid).get('operation')
    assert (d.tx.root / d.sid).exists()
    d.release.assert_not_awaited()


@pytest.mark.asyncio
async def test_stale_capture_cannot_delete_new_runtime_generation(deletion):
    d = deletion
    request = d.request()
    authority = d.tx.runtime.prepare_session_deletion(request)
    await d.tx.runtime._session_coordinator.close_session(d.sid)
    await d.tx.runtime._register_session(channel_id='web', session_id=d.sid)
    with pytest.raises(SessionSharingDenied, match='newer execution'):
        await d.service.session(d.sid, 'delete', 'web', _deletion_authority=authority)
    assert (d.tx.root / d.sid).exists()
    d.release.assert_not_awaited()


@pytest.mark.asyncio
async def test_organization_background_recovery_cannot_impersonate_owner(deletion):
    with pytest.raises(lc.LifecycleError, match='authenticated Single'):
        await deletion.service.session(deletion.sid, 'delete', 'web')
    deletion.release.assert_not_awaited()
    assert (deletion.tx.root / deletion.sid).exists()


@pytest.mark.asyncio
async def test_transport_channel_cannot_select_another_cached_execution(deletion):
    d = deletion
    request = d.request()
    request.channel_id = 'another-transport'
    authority = d.tx.runtime.prepare_session_deletion(request)
    assert authority.channel_id == 'web'
    result = await d.service.session(d.sid, 'delete', 'another-transport', _deletion_authority=authority)
    assert result['exit_confirmed'] is True
    d.tx.manager.stop_existing_session_runtime.assert_awaited_once_with(channel_id='web', session_id=d.sid)
    d.tx.manager.cleanup_session_runtime.assert_awaited_once_with(channel_id='web', session_id=d.sid)


@pytest.mark.asyncio
async def test_owned_transaction_retries_real_heartbeat_after_destructive_failure(deletion, monkeypatch):
    from unittest.mock import Mock
    from jiuwenswarm.agents.harness.code.rails.heartbeat import runtime as heartbeat_module
    d = deletion
    monkeypatch.setattr(heartbeat_module, 'get_config', lambda: {})
    monkeypatch.setattr(heartbeat_module, 'get_heartbeat_jobs_path',
                        lambda: d.tx.setup.tmp_path / 'delete-heartbeat.sqlite')
    heartbeat = heartbeat_module.HeartbeatRailRuntime(SimpleNamespace(
        get_agent_manager=lambda: SimpleNamespace(unpin_agent=Mock())))
    heartbeat._available = True
    heartbeat.scheduler.on_session_deleted = AsyncMock()
    heartbeat.store.count_active_jobs_for_session = AsyncMock(return_value=0)
    heartbeat.admission.block_heartbeats = AsyncMock(wraps=heartbeat.admission.block_heartbeats)
    d.tx.runtime._session_provisioner._delete_lifecycle = heartbeat
    commits = []
    def commit(_):
        commits.append(d.sid)
        if len(commits) == 1:
            raise OSError('synthetic trajectory commit unavailable')
    monkeypatch.setattr('jiuwenswarm.observability.session_delete.commit_trajectory_session_delete', commit)
    with pytest.raises(Exception):
        await d.run()
    assert not (d.tx.root / d.sid).exists()
    before = txns.owner(d.tx)['deletion']
    assert heartbeat._deleting_sessions[d.sid].ready
    assert heartbeat.admission._states[d.sid].heartbeat_blocked
    assert (await d.run())['exit_confirmed'] is True
    after = txns.owner(d.tx)['deletion']
    assert after['deletion_id'] == before['deletion_id']
    assert after['operation_id'] == before['operation_id']
    assert lc.state('session', d.sid)['deleted'] is True
    assert d.sid not in heartbeat._deleting_sessions
    heartbeat.admission.block_heartbeats.assert_awaited_once_with(d.sid)
    d.release.assert_awaited_once_with(d.sid)
