"""Regression coverage for retained TUI aliases and directory-only history."""
import asyncio
from types import SimpleNamespace

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.governance.contracts import TrustedIdentity
from jiuwenswarm.governance.project_boundary import (
    authorize_resource_request, ProjectAccessDenied as BoundaryDenied,
)
from jiuwenswarm.server.runtime.gateway_adapter import project_adapter
from jiuwenswarm.server.runtime.session import project_store, project_git
from jiuwenswarm.server.runtime.session.project_access import ProjectAccessStore


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(project_store, 'get_agent_root_dir', lambda: tmp_path)
    project_store.invalidate_cache()
    directory = tmp_path / 'private'
    directory.mkdir()
    original = project_store.create_project('Private', str(directory), work_mode='work')
    store = ProjectAccessStore()
    store.initialize(original.project_id, 'owner')
    store.replace_acl(original.project_id, 'owner', acl={'reader': ['read'], 'writer': ['write']}, expected_revision=1)
    # Exercise the real retained TUI registration path. Only the unrelated Git
    # probe is replaced; registry creation and persistence use production code.
    monkeypatch.setattr(project_git, 'get_project_git_service', lambda: SimpleNamespace(probe=lambda _: None))
    alias = project_store.find_or_create_code_project_for_dir(str(directory))
    assert alias.project_id != original.project_id
    assert not store.is_protected(alias.project_id)
    monkeypatch.setattr(project_adapter, 'collect_all_sessions_metadata', lambda: [])
    yield store, original, alias
    project_store.invalidate_cache()


def adapter(actor):
    return project_adapter.ProjectAdapter(None if actor is None else lambda _: TrustedIdentity(actor, actor, 'test-host'))


def request(method, **params):
    return AgentRequest(request_id='alias', channel_id='web', req_method=method, params=params)


@pytest.mark.asyncio
@pytest.mark.parametrize('actor,read,write', [(None,False,False), ('owner',True,True), ('reader',True,False), ('writer',False,True)])
async def test_real_tui_alias_requires_original_action(setup, actor, read, write):
    store, original, alias = setup
    api = adapter(actor)
    response = await api.handle(request(ReqMethod.PROJECT_INFO, project_id=alias.project_id))
    assert response.ok is read
    if not read:
        assert response.payload['code'] == 'FORBIDDEN'
    response = await api.handle(request(ReqMethod.PROJECT_LIST))
    ids = {p['project_id'] for p in response.payload['projects']}
    assert (alias.project_id in ids) is read
    assert (original.project_id in ids) is read
    response = await api.handle(request(ReqMethod.PROJECT_RENAME, project_id=alias.project_id, name='Renamed'))
    assert response.ok is write
    if not write:
        assert response.payload['code'] == 'FORBIDDEN'
    current = project_store.get_project_by_id(alias.project_id, cache_bust=True)
    assert (current.name == 'Renamed') is write


@pytest.mark.asyncio
async def test_alias_revocation_rechecked_at_sync_io(setup, monkeypatch):
    store, original, alias = setup
    real_thread = asyncio.to_thread
    async def revoke_then_dispatch(fn, *args, **kwargs):
        store.replace_acl(original.project_id, 'owner', acl={}, expected_revision=2)
        return await real_thread(fn, *args, **kwargs)
    monkeypatch.setattr(project_adapter.asyncio, 'to_thread', revoke_then_dispatch)
    response = await adapter('writer').handle(request(ReqMethod.PROJECT_RENAME, project_id=alias.project_id, name='Forbidden'))
    assert not response.ok and response.payload['code'] == 'FORBIDDEN'
    assert project_store.get_project_by_id(alias.project_id, cache_bust=True).name != 'Forbidden'


@pytest.mark.asyncio
@pytest.mark.parametrize('actor,visible', [(None,False), ('owner',True), ('reader',False), ('writer',False)])
async def test_orphan_directory_history_stays_private(setup, monkeypatch, actor, visible):
    store, original, alias = setup
    project_store.delete_project(original.project_id)
    project_store.delete_project(alias.project_id)
    monkeypatch.setattr(project_adapter, 'collect_all_sessions_metadata', lambda: [
        {'session_id':'old-dir-only', 'project_dir':original.project_dir,
         'channel_id':'web', 'work_mode':'work', 'pinned':False, 'title':'Private history'},
    ])
    api = adapter(actor)
    response = await api.handle(request(ReqMethod.PROJECT_GET_SESSIONS, project_id='default'))
    assert response.ok
    assert bool(response.payload['sessions']) is visible
    response = await api.handle(request(ReqMethod.PROJECT_LIST))
    default = next(p for p in response.payload['projects'] if p['project_id'] == 'default')
    assert default['session_count'] == int(visible)


@pytest.mark.parametrize('actor,allowed', [(None,False), ('owner',True), ('reader',True), ('writer',False)])
def test_memory_edit_content_preview_uses_read_permission(setup, actor, allowed):
    store, original, alias = setup
    identity = None if actor is None else TrustedIdentity(actor, actor, 'test-host')
    req = request(ReqMethod.MEMORY_EDIT, project_dir=original.project_dir, path=original.project_dir + '/JIUWENSWARM.md')
    if allowed:
        authorize_resource_request(req, identity)
    else:
        with pytest.raises(BoundaryDenied):
            authorize_resource_request(req, identity)
