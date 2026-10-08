"""Authenticated projectless sessions retain owner and resource isolation."""
import json
from pathlib import Path

import pytest

from tests.unit_tests.server.test_organization_inventory import inventory
from jiuwenswarm.governance.session_publication import SessionOwnerPublication
from jiuwenswarm.governance.resources import ResourceGuard, ResourceRequest
from jiuwenswarm.governance import session_boundary
from jiuwenswarm.server.runtime.session import lifecycle


@pytest.fixture
def private(inventory, tmp_path, monkeypatch):
    monkeypatch.setenv('JIUWENSWARM_TASKS_DIR', str(tmp_path / 'tasks'))
    monkeypatch.setenv('JIUWENSWARM_TASK_REGISTRY_DIR', str(tmp_path / 'registry'))
    def create(sid='private-one', actor='alice', work_mode='work'):
        identity = inventory.principals[actor].identity()
        pid = 'default_code' if work_mode == 'code' else 'default'
        publication = SessionOwnerPublication(inventory.host)
        with publication.scope(lambda: identity):
            receipt = publication.before_publish(sid, pid, True)
        folder = lifecycle.get_agent_sessions_dir() / sid
        folder.mkdir(exist_ok=True)
        (folder / 'metadata.json').write_text(json.dumps({'session_id': sid, 'project_id': pid,
            'project_dir': '', 'channel_id': 'web', 'mode': 'agent', 'work_mode': work_mode,
            'user_id': 'untrusted', 'title': 'private'}))
        (folder / 'history.jsonl').write_text(json.dumps({'role': 'user', 'content': 'Hi'})+'\n')
        with publication.scope(lambda: identity):
            publication.before_publish(sid, pid, False)
        return identity, pid, inventory.host.resource_authorizer(sid), receipt
    return create


@pytest.mark.parametrize('work_mode', ['work', 'code'])
def test_projectless_creation_owner_history_and_workspace(private, inventory, work_mode):
    identity, pid, resources, _ = private(work_mode=work_mode)
    assert inventory.host.owner_current('private-one', identity, 'execute')
    assert not inventory.host.owner_current('private-one', inventory.principals['bob'].identity())
    permit = session_boundary.admit_session_request('history.get', {'session_id': 'private-one'},
        identity_resolver=lambda: identity, host=inventory.host)
    assert permit.revalidate()
    root = inventory.host.private_workspace('private-one', identity)
    assert ResourceGuard(resources).check(pid, identity, ResourceRequest('session-workspace','write',str(Path(root)/'work/a.txt'))).allowed
    with pytest.raises(PermissionError):
        ResourceGuard(resources).check(pid, identity, ResourceRequest('session-workspace','read',str(Path(root).parent/'other/a.txt')))
    assert not inventory.access._load()['projects'].get(pid), 'must not create a hidden default Project'


def test_no_credential_inheritance_and_explicit_policy_revocation(private, inventory):
    identity, pid, resources, _ = private()
    request = ResourceRequest('model-private', 'use')
    with pytest.raises(PermissionError):
        ResourceGuard(resources).check(pid, identity, request)
    auth = inventory.principals['alice']
    path = Path(auth.config_path)
    config = json.loads(path.read_text())
    config['private_session_resources'] = {'alice': {'revision': 1, 'resources': [
        {'resource_id':'model-private','kind':'credential','reference':'model:fixture','actions':['use']} ]}}
    path.write_text(json.dumps(config))
    assert ResourceGuard(resources).check(pid, identity, request).reference == 'model:fixture'
    with pytest.raises(PermissionError):
        ResourceGuard(resources).check(pid, inventory.principals['bob'].identity(), request)
    config['private_session_resources']['alice']['resources'] = []
    path.write_text(json.dumps(config))
    with pytest.raises(PermissionError):
        ResourceGuard(resources).check(pid, identity, request)


def test_workspace_replacement_and_compensation_fail_closed(private, inventory):
    identity, pid, resources, rollback = private()
    root = Path(inventory.host.private_workspace('private-one', identity))
    root.rename(root.with_name(root.name+'-old'))
    root.mkdir()
    assert not inventory.host.owner_current('private-one', identity)
    with pytest.raises(PermissionError):
        resources.resource_grants(pid, identity)
    rollback()
    assert not inventory.host.owner_current('private-one', identity)


def test_private_history_can_be_shared_without_resource_transfer(private, inventory):
    identity, pid, resources, _ = private()
    history = inventory.host.prepare_source('private-one', identity)
    assert history.session_id == 'private-one'
    assert inventory.host.resolve_source('private-one').actions == frozenset({'view','manage','execute'})
    # Received view authority never establishes execution ownership.
    bob = inventory.principals['bob'].identity()
    with pytest.raises(PermissionError):
        resources.resource_grants(pid, bob)


@pytest.mark.asyncio
async def test_project_create_cannot_claim_private_workspace(private, inventory):
    from jiuwenswarm.common.schema.agent import AgentRequest
    from jiuwenswarm.common.schema.message import ReqMethod
    from jiuwenswarm.server.runtime.gateway_adapter.project_adapter import ProjectAdapter
    identity, _, _, _ = private()
    root = Path(inventory.host.private_workspace('private-one', identity))
    for actor in ['alice', 'bob']:
        caller = inventory.principals[actor].identity()
        for path in [root, root / 'work', root.parent]:
            params = {'name': 'alias', 'work_mode': 'code', 'project_dir': str(path)}
            permit = session_boundary.admit_session_request('project.create', params,
                identity_resolver=lambda: caller, host=inventory.host)
            with session_boundary.delivery_scope():
                session_boundary.set_delivery_permit(permit)
                result = await ProjectAdapter(lambda _: caller).handle(AgentRequest('private-alias',
                    channel_id='web', req_method=ReqMethod.PROJECT_CREATE, params=params))
            assert not result.ok and result.payload['code'] == 'FORBIDDEN'
    assert not (root / '.git').exists()


def test_private_share_create_read_and_revoke(private, inventory):
    # Use the actual store so private source authority follows the same grant chain.
    identity, _, resources, _ = private()
    history = inventory.host.prepare_source('private-one', identity)
    bob = inventory.principals['bob'].identity()
    store = inventory.host.store
    grant = store.grant('private-one', identity, bob, actions=['view'], history=history, expires_at=None)
    assert store.list_for_actor(bob)
    store.revoke(grant['share_id'], identity, expected_revision=1)
    assert not store.list_for_actor(bob)
    with pytest.raises(PermissionError):
        resources.resource_grants('default', bob)


def test_private_download_is_owner_session_scoped_and_revocable(private, inventory):
    from jiuwenswarm.governance.workspace_download import (
        WorkspaceArtifactIssuer, WorkspaceDownloadPermit, WorkspaceDownloadDenied,
    )
    from jiuwenswarm.agents.harness.common.tools.web_file_download import WebFileDownloadManager
    identity, _, _, _ = private()
    sid = 'private-one'
    root = Path(inventory.host.private_workspace(sid, identity))
    file = root / 'report.txt'
    file.write_text('private fixture')
    alice = inventory.principals['alice']
    issuer = WorkspaceArtifactIssuer.capture(inventory.host, alice.identity, sid,
        channel_id='web', workspace=str(root), source_check=lambda: True,
        actual_paths=(str(file),))
    manager = WebFileDownloadManager(secret='synthetic-private-download-key')
    token = manager.generate_token(str(file), sid, artifact_issuer=issuer)
    permit = WorkspaceDownloadPermit.capture(inventory.host, alice.identity, sid,
        token, token_validator=manager.validate_token)
    assert permit.read(0, 7) == b'private'
    with pytest.raises(WorkspaceDownloadDenied):
        WorkspaceDownloadPermit.capture(inventory.host, inventory.principals['bob'].identity,
            sid, token, token_validator=manager.validate_token)
    private(sid='private-two')
    with pytest.raises(WorkspaceDownloadDenied):
        WorkspaceDownloadPermit.capture(inventory.host, alice.identity, 'private-two',
            token, token_validator=manager.validate_token)
    root.rename(root.with_name(root.name+'-retired'))
    with pytest.raises(WorkspaceDownloadDenied):
        permit.read(0, 7)


@pytest.mark.asyncio
async def test_private_runtime_binds_trusted_subject_without_project_snapshot(private, inventory, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from jiuwenswarm.runtime import AgentRuntime
    from jiuwenswarm.common.schema.agent import AgentRequest
    from jiuwenswarm.common.schema.message import ReqMethod
    identity, pid, _, _ = private()
    runtime = AgentRuntime(agent_manager=SimpleNamespace(), initializer=AsyncMock(),
        trusted_identity_resolver=lambda _: identity, organization_session_host=inventory.host)
    await runtime.start()
    capture = AsyncMock(return_value=('agent', None, object()))
    monkeypatch.setattr('jiuwenswarm.runtime.request.prepare_chat_turn', capture)
    request = AgentRequest('private-request', channel_id='web', session_id='private-one',
        req_method=ReqMethod.CHAT_SEND, params={'query':'Hi','project_id':pid,'user_id':'bob'})
    await runtime._prepare_chat_turn(request, 'web')
    assert capture.call_args.kwargs['trusted_subject_id'] == identity.subject_id
    assert request._project_content_snapshot is None
    await runtime._session_coordinator.close()
