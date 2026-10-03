"""Real temporary-file ACL, legacy API and cross-process revision coverage."""
import json
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

from jiuwenswarm.governance.contracts import TrustedIdentity
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.runtime.session import project_store
from jiuwenswarm.server.runtime.session.project_access import (
    ProjectAccessStore, ProjectAccessDenied, ProjectRevisionConflict,
)
from jiuwenswarm.server.runtime.gateway_adapter import project_adapter


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(project_store, 'get_agent_root_dir', lambda: tmp_path)
    project_store.invalidate_cache()
    yield ProjectAccessStore()
    project_store.invalidate_cache()


def project(store, owner='owner'):
    item = project_store.create_project('Private', '/private')
    store.migrate_legacy({item.project_id: owner})
    return item


def test_migration_preserves_legacy_unknown_and_idempotence(store):
    item = project_store.create_project('Old', '/old')
    path = project_store._projects_file()
    raw = json.loads(path.read_text())
    raw['projects'][0]['future_extension'] = {'keep': True}
    path.write_text(json.dumps(raw))
    assert store.migrate_legacy() == 1
    assert store.migrate_legacy({item.project_id: 'claimant'}) == 0
    assert not store.authorize(item.project_id, 'claimant', 'read').allowed
    assert not store.authorize(item.project_id, '', 'read').allowed
    item.name = 'Renamed'
    project_store.save_project(item)
    assert json.loads(path.read_text())['projects'][0]['future_extension'] == {'keep': True}
    assert store._load()['projects'][item.project_id]['owner_id'] is None


def test_explicit_grants_no_execute_and_revision_check(store):
    item = project(store)
    revision = store.replace_acl(item.project_id, 'owner', acl={'reader': ['read']}, expected_revision=1)
    assert revision == 2
    assert store.authorize(item.project_id, 'reader', 'read').allowed
    assert not store.authorize(item.project_id, 'reader', 'execute').allowed
    assert not store.authorize(item.project_id, 'reader', 'write').allowed
    assert 'acl' not in store.get(item.project_id, 'reader')
    with pytest.raises(ProjectRevisionConflict):
        store.replace_acl(item.project_id, 'owner', acl={}, expected_revision=1)
    with pytest.raises(ProjectAccessDenied):
        store.update(item.project_id, 'reader', goal='bad', extensions={})
    store.update(item.project_id, 'owner', goal='Compare sources', extensions={'research': {'sources': ['a']}})
    fresh = ProjectAccessStore()
    assert fresh.get(item.project_id, 'owner')['goal'] == 'Compare sources'
    store.replace_acl(item.project_id, 'owner', acl={}, expected_revision=2)
    assert not fresh.authorize(item.project_id, 'reader', 'read').allowed


def test_unknown_schema_corruption_and_incomplete_creation_fail_closed(store):
    item = project(store)
    store.path.write_text('{broken')
    assert store.is_protected(item.project_id)
    assert not store.authorize(item.project_id, 'owner', 'read').allowed
    store.path.unlink()
    path = project_store._projects_file()
    raw = json.loads(path.read_text())
    raw['projects'][0]['access_managed'] = True
    path.write_text(json.dumps(raw))
    assert store.is_protected(item.project_id)
    assert not store.authorize(item.project_id, 'owner', 'read').allowed


def test_cross_process_revoke_is_seen_without_cache_bust(store):
    item = project(store)
    store.replace_acl(item.project_id, 'owner', acl={'reader': ['read']}, expected_revision=1)
    assert store.authorize(item.project_id, 'reader', 'read').allowed
    script = '''
import sys
from pathlib import Path
from jiuwenswarm.server.runtime.session import project_store
from jiuwenswarm.server.runtime.session.project_access import ProjectAccessStore
project_store.get_agent_root_dir = lambda: Path(sys.argv[1])
ProjectAccessStore().replace_acl(sys.argv[2], 'owner', acl={}, expected_revision=2)
'''
    result = subprocess.run([sys.executable, '-c', script, str(store.path.parent), item.project_id], capture_output=True, text=True, timeout=30, env=os.environ.copy())
    assert result.returncode == 0, result.stderr
    decision = store.authorize(item.project_id, 'reader', 'read')
    assert not decision.allowed and decision.revision == 3


def request(method, **params):
    return AgentRequest(request_id='req', channel_id='web', user_id='owner', req_method=method, params=params)


@pytest.mark.asyncio
async def test_existing_project_api_denies_wire_identity_and_filters_sessions(store, monkeypatch):
    item = project(store)
    monkeypatch.setattr(project_adapter, 'collect_all_sessions_metadata', lambda: [
        {'session_id': 'secret', 'project_id': item.project_id, 'channel_id': 'web', 'pinned': True},
    ])
    api = project_adapter.ProjectAdapter()
    for method in (ReqMethod.PROJECT_INFO, ReqMethod.PROJECT_RENAME, ReqMethod.PROJECT_GET_SESSIONS, ReqMethod.PROJECT_GIT_STATUS, ReqMethod.PROJECT_GIT_INIT):
        response = await api.handle(request(method, project_id=item.project_id, name='Stolen', actor_id='owner', identity={'actor_id': 'owner'}))
        assert not response.ok and response.payload['code'] == 'FORBIDDEN'
    listed = await api.handle(request(ReqMethod.PROJECT_LIST))
    assert all(p['project_id'] != item.project_id for p in listed.payload['projects'])
    pinned = await api.handle(request(ReqMethod.PROJECT_PINNED_SESSIONS))
    assert pinned.payload['sessions'] == []
    assert project_store.get_project_by_id(item.project_id, cache_bust=True).name == 'Private'


@pytest.mark.asyncio
async def test_owner_legacy_api_and_extensions_real_consumption(store, monkeypatch):
    item = project(store)
    monkeypatch.setattr(project_adapter, 'collect_all_sessions_metadata', lambda: [])
    api = project_adapter.ProjectAdapter(lambda _: TrustedIdentity('owner', 'owner', 'test-host'))
    response = await api.handle(request(ReqMethod.PROJECT_RENAME, project_id=item.project_id, name='Renamed'))
    assert response.ok
    # New method strings become enum values at the integrated public protocol.
    def extension_request(method, **params):
        return SimpleNamespace(request_id='ext', channel_id='web', user_id='spoof', req_method=method, params=params, metadata={})
    updated = await api.handle(extension_request('project.extensions.update', project_id=item.project_id, goal='Research', extensions={'citations': ['one']}))
    assert updated.ok
    loaded = await api.handle(extension_request('project.extensions.get', project_id=item.project_id))
    assert loaded.payload['goal'] == 'Research'
    changed = await api.handle(extension_request('project.acl.update', project_id=item.project_id, acl={'viewer': ['read']}, expected_revision=1))
    assert changed.ok and changed.payload['acl_revision'] == 2
    assert ProjectAccessStore().authorize(item.project_id, 'viewer', 'read').allowed


@pytest.mark.asyncio
async def test_trusted_create_records_owner_before_git_side_effect(store, tmp_path, monkeypatch):
    directory = tmp_path / 'work'
    directory.mkdir()
    from jiuwenswarm.server.runtime.session import project_git
    monkeypatch.setattr(project_git, 'get_project_git_service', lambda: SimpleNamespace(ensure_on_project_create=lambda _: None))
    api = project_adapter.ProjectAdapter(lambda _: TrustedIdentity('owner', 'owner', 'test-host'))
    response = await api.handle(request(ReqMethod.PROJECT_CREATE, name='Created', project_dir=str(directory), work_mode='work'))
    assert response.ok, response.payload
    created = project_store.list_projects(cache_bust=True)[0]
    assert store.is_protected(created.project_id)
    assert store.authorize(created.project_id, 'owner', 'admin').allowed
    assert not store.authorize(created.project_id, '', 'read').allowed


def test_missing_sidecar_never_reopens_migrated_project(store):
    item = project(store)
    store.path.unlink()
    assert store.is_protected(item.project_id)
    assert not store.authorize(item.project_id, 'owner', 'read').allowed
    assert not store.authorize(item.project_id, '', 'write').allowed


@pytest.mark.asyncio
@pytest.mark.parametrize('trusted', [False, True])
async def test_create_cannot_alias_protected_workspace(store, tmp_path, trusted):
    directory = tmp_path / 'private'
    child = directory / 'child'
    child.mkdir(parents=True)
    item = project_store.create_project('Private', str(directory))
    store.migrate_legacy({item.project_id: 'owner'})
    resolver = (lambda _: TrustedIdentity('other', 'other', 'host')) if trusted else None
    api = project_adapter.ProjectAdapter(resolver)
    for candidate in (directory, child, tmp_path):
        response = await api.handle(request(ReqMethod.PROJECT_CREATE, name='Alias', project_dir=str(candidate), work_mode='code'))
        assert not response.ok and response.payload['code'] == 'FORBIDDEN'
    assert len(project_store.list_projects(cache_bust=True)) == 1


@pytest.mark.asyncio
async def test_trusted_create_cannot_add_protected_alias_to_legacy_workspace(store, tmp_path):
    directory = tmp_path / 'legacy'
    directory.mkdir()
    project_store.create_project('Legacy', str(directory))
    api = project_adapter.ProjectAdapter(lambda _: TrustedIdentity('owner', 'owner', 'host'))
    response = await api.handle(request(ReqMethod.PROJECT_CREATE, name='New', project_dir=str(directory), work_mode='code'))
    assert not response.ok and response.payload['code'] == 'FORBIDDEN'
    assert len(project_store.list_projects(cache_bust=True)) == 1


def test_protected_ids_include_deleted_registry_history(store):
    item = project(store)
    project_store.delete_project(item.project_id)
    assert store.protected_ids() == (item.project_id,)
    assert store.authorize(item.project_id, 'owner', 'read').allowed


def test_protected_ids_include_marker_when_sidecar_is_missing(store):
    item = project(store)
    store.path.unlink()
    assert store.protected_ids() == (item.project_id,)
    legacy = project_store.create_project('Legacy', '/legacy')
    assert legacy.project_id not in store.protected_ids()


@pytest.mark.parametrize('file', ['registry', 'sidecar'])
def test_protected_ids_storage_corruption_fails_closed(store, file):
    project(store)
    path = store.path if file == 'sidecar' else project_store._projects_file()
    path.write_text('{broken')
    with pytest.raises(ProjectAccessDenied):
        store.protected_ids()


def test_deleted_project_preserves_only_owner_history_and_cleanup(store):
    item = project(store)
    store.replace_acl(item.project_id, 'owner', acl={'member': ['read', 'write', 'admin', 'execute']}, expected_revision=1)
    project_store.delete_project(item.project_id)
    for action in ('read', 'write', 'admin'):
        assert store.authorize(item.project_id, 'owner', action).allowed
        assert not store.authorize(item.project_id, 'member', action).allowed
        assert not store.authorize(item.project_id, '', action).allowed
    assert not store.authorize(item.project_id, 'owner', 'execute').allowed
    assert not store.authorize(item.project_id, 'member', 'execute').allowed
    assert project_store.get_project_by_id(item.project_id, cache_bust=True) is None


@pytest.mark.asyncio
async def test_deleted_project_owner_inventory_and_detail_not_found(store, monkeypatch):
    item = project(store)
    project_store.delete_project(item.project_id)
    monkeypatch.setattr(project_adapter, 'collect_all_sessions_metadata', lambda: [])
    owner = project_adapter.ProjectAdapter(lambda _: TrustedIdentity('owner', 'owner', 'host'))
    result = await owner.handle(request(ReqMethod.PROJECT_LIST))
    assert result.ok
    assert all(p['project_id'] != item.project_id for p in result.payload['projects'])
    result = await owner.handle(request(ReqMethod.PROJECT_INFO, project_id=item.project_id))
    assert not result.ok and result.payload['code'] == 'NOT_FOUND'
    anonymous = project_adapter.ProjectAdapter()
    result = await anonymous.handle(request(ReqMethod.PROJECT_INFO, project_id=item.project_id))
    assert not result.ok and result.payload['code'] == 'FORBIDDEN'
