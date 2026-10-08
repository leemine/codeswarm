"""Actual Git, original project/resource stores and request delivery checks."""
from pathlib import Path
from types import SimpleNamespace
import subprocess

import pytest

from jiuwenswarm.governance.contracts import TrustedIdentity
from jiuwenswarm.governance.project_git import capture_git_request
from jiuwenswarm.governance.resources import ResourceDefinition, ResourceRequest
from jiuwenswarm.governance.session_boundary import admit_session_request, delivery_scope, set_delivery_permit
from jiuwenswarm.server.runtime.session import project_store
from jiuwenswarm.server.runtime.session.project_access import ProjectAccessStore
from jiuwenswarm.server.runtime.gateway_adapter.project_adapter import ProjectAdapter
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod


def git(root, *args):
    return subprocess.run(['git', '-C', str(root), *args], capture_output=True, text=True, check=True, timeout=10).stdout.strip()


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    data = tmp_path / 'data'; data.mkdir()
    monkeypatch.setenv('JIUWENSWARM_DATA_DIR', str(data))
    monkeypatch.setattr(project_store, 'get_agent_root_dir', lambda: data)
    project_store.invalidate_cache()
    workspace = tmp_path / 'workspace'; workspace.mkdir()
    root = workspace / 'repo'; root.mkdir()
    p = project_store.create_project('Code', str(root), 'code')
    store = ProjectAccessStore(); store.initialize(p.project_id, 'alice')
    store.register_resource(p.project_id, ResourceDefinition('workspace', 'workspace', str(workspace)),
        owner_subject_id='alice', actions=('read', 'write'), delegable=True, expected_revision=0)
    identity = TrustedIdentity('alice', 'alice', 'org:test')
    current = [identity]
    host = SimpleNamespace(_storage=store)
    monkeypatch.setattr('jiuwenswarm.governance.organization_auth.configured_authenticator', lambda: object())
    yield SimpleNamespace(root=root, workspace=workspace, project=p, store=store, identity=identity,
                          current=current, host=host, resolve=lambda: current[0])
    project_store.invalidate_cache()


async def request(f, name, **params):
    method = 'project.git.' + name
    params = {'project_id': f.project.project_id, **params}
    permit = admit_session_request(method, params, identity_resolver=f.resolve, host=f.host)
    with delivery_scope():
        set_delivery_permit(permit)
        result = await ProjectAdapter(lambda r: f.current[0]).handle(AgentRequest(
            'git-test', channel_id='web', req_method=ReqMethod(method), params=params))
        assert permit.revalidate()
        return result


@pytest.mark.asyncio
async def test_actual_repository_init_branch_commit_switch_and_local_push(fixture):
    f = fixture
    probe = await request(f, 'probe')
    assert probe.payload['code'] == 'NOT_GIT_REPOSITORY'
    assert (await request(f, 'init')).ok
    git(f.root, 'config', 'user.name', 'Fixture')
    git(f.root, 'config', 'user.email', 'fixture@example.invalid')
    (f.root / 'README.md').write_text('initial\n')
    assert (await request(f, 'commit', message='initial', stage_all=True)).ok
    assert (await request(f, 'create_branch', branch='test-feature')).ok
    assert git(f.root, 'branch', '--show-current') == 'test-feature'
    assert (await request(f, 'switch_branch', branch='main', require_clean=True)).ok
    remote = f.workspace / 'remote.git'; remote.mkdir(); git(remote, 'init', '--bare')
    git(f.root, 'remote', 'add', 'origin', str(remote))
    result = await request(f, 'push', remote='origin', branch='main', set_upstream=True)
    assert result.ok, result.payload
    assert git(remote, 'rev-parse', 'refs/heads/main') == git(f.root, 'rev-parse', 'HEAD')
    assert (await request(f, 'status')).ok


def test_identity_resource_revocation_and_exact_request(fixture):
    f = fixture; params = {'project_id': f.project.project_id}
    p = capture_git_request('project.git.status', params, f.resolve, f.store)
    f.current[0] = TrustedIdentity('bob', 'bob', 'org:test')
    with pytest.raises(PermissionError): p.check()
    f.current[0] = f.identity
    with pytest.raises(PermissionError):
        with p.consume('project.git.init', params): pass
    f.store.revoke_resource(f.project.project_id, f.identity, 'workspace',
                            subject_id='alice', expected_revision=1)
    with pytest.raises(PermissionError): p.check()


@pytest.mark.asyncio
async def test_unscoped_direct_adapter_and_parent_repo_are_denied(fixture):
    f = fixture
    result = await ProjectAdapter(lambda r: f.identity).handle(AgentRequest('direct', channel_id='web',
        req_method=ReqMethod.PROJECT_GIT_STATUS, params={'project_id': f.project.project_id}))
    assert not result.ok
    git(f.workspace, 'init')
    result = await request(f, 'probe')
    assert not result.ok and result.payload['code'] == 'FORBIDDEN'


@pytest.mark.asyncio
async def test_readonly_resource_cannot_write_or_mutate_branches(fixture):
    f = fixture
    # Replace the existing grant through the original store, not a fake authorizer.
    f.store.replace_acl(f.project.project_id, 'alice', acl={'bob': ['read', 'write', 'execute']}, expected_revision=1)
    f.store.grant_resource(f.project.project_id, f.identity, 'workspace', subject_id='bob',
        actions=('read',), scope=str(f.workspace), expires_at=None, expected_revision=1)
    f.current[0] = TrustedIdentity('bob', 'bob', 'org:test')
    assert (await request(f, 'status')).payload['code'] == 'NOT_GIT_REPOSITORY'
    with pytest.raises(PermissionError): await request(f, 'init')
    assert not (f.root / '.git').exists()


@pytest.mark.asyncio
async def test_linked_git_config_filters_and_external_remote_fail_closed(fixture):
    f = fixture
    outside = f.workspace / 'other'; outside.mkdir()
    (f.root / '.git').symlink_to(outside, target_is_directory=True)
    assert not (await request(f, 'status')).ok
    (f.root / '.git').unlink()
    assert (await request(f, 'init')).ok
    git(f.root, 'config', 'filter.unsafe.clean', 'touch /tmp/must-not-run')
    assert not (await request(f, 'status')).ok
    git(f.root, 'config', '--remove-section', 'filter.unsafe')
    git(f.root, 'remote', 'add', 'origin', 'https://example.invalid/repo')
    assert not (await request(f, 'push')).ok


@pytest.mark.asyncio
async def test_local_hooks_are_not_executed(fixture):
    f = fixture
    assert (await request(f, 'init')).ok
    git(f.root, 'config', 'user.name', 'Fixture')
    git(f.root, 'config', 'user.email', 'fixture@example.invalid')
    hook = f.root / '.git/hooks/pre-commit'
    marker = f.root / 'hook-ran'
    hook.write_text('#!/bin/sh\ntouch "' + str(marker) + '"\nexit 1\n'); hook.chmod(0o700)
    (f.root / 'README').write_text('hello')
    assert (await request(f, 'commit', message='safe', stage_all=True)).ok
    assert not marker.exists()


def test_delivery_denies_acl_change_and_workspace_replacement(fixture):
    f = fixture
    params = {'project_id': f.project.project_id}
    permit = admit_session_request('project.git.status', params, identity_resolver=f.resolve, host=f.host)
    assert permit.revalidate()
    f.store.replace_acl(f.project.project_id, 'alice', acl={}, expected_revision=1)
    assert not permit.revalidate()
    permit = admit_session_request('project.git.status', params, identity_resolver=f.resolve, host=f.host)
    f.root.rename(f.workspace / 'old-repo')
    f.root.mkdir()
    assert not permit.revalidate()


@pytest.mark.asyncio
async def test_overlapping_project_owned_by_another_actor_is_denied(fixture):
    f = fixture
    other = project_store.create_project('Other', str(f.root), 'work')
    f.store.initialize(other.project_id, 'bob')
    result = await request(f, 'init')
    assert not result.ok and result.payload['code'] == 'FORBIDDEN'
    assert not (f.root / '.git').exists()
