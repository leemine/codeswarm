"""Legacy ingress checks use real temporary project ACLs, not routing claims."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.governance.contracts import TrustedIdentity
from jiuwenswarm.governance.host_identity import local_instance_identity
from jiuwenswarm.governance.project_boundary import (
    ProjectAccessDenied, authorize_resource_request,
)
from jiuwenswarm.server.runtime.session import project_store, session_metadata
from jiuwenswarm.server.runtime.session.project_access import ProjectAccessStore


def test_local_identity_is_host_scoped_and_remote_bind_has_no_implicit_identity(tmp_path):
    first = local_instance_identity("127.0.0.1", tmp_path)
    assert first == local_instance_identity("::1", tmp_path)
    assert first != local_instance_identity("127.0.0.1", tmp_path / "other")
    assert local_instance_identity("0.0.0.0", tmp_path) is None
    assert local_instance_identity("localhost", tmp_path) is None


@pytest.fixture
def protected(tmp_path, monkeypatch):
    monkeypatch.setattr(project_store, "get_agent_root_dir", lambda: tmp_path / "agent")
    project_store.invalidate_cache()
    directory = tmp_path / "private"
    directory.mkdir()
    item = project_store.create_project("private", str(directory))
    store = ProjectAccessStore()
    store.migrate_legacy({item.project_id: "owner"})
    store.replace_acl(item.project_id, "owner", acl={"reader": ["read"]}, expected_revision=1)
    monkeypatch.setattr(session_metadata, "get_session_metadata", lambda sid, **kw: (
        {"project_id": item.project_id, "project_dir": str(directory)}
        if sid == "private-session" else {}
    ))
    yield item, store
    project_store.invalidate_cache()


def request(method, *, session_id=None, **params):
    return AgentRequest("r1", channel_id="web", req_method=method,
                        session_id=session_id, user_id="owner", params=params,
                        metadata={"trusted_identity": {"actor_id": "owner"}})


@pytest.mark.parametrize("method", [
    ReqMethod.HISTORY_GET, ReqMethod.SESSION_GET_METADATA, ReqMethod.SESSION_DELETE,
    ReqMethod.CHAT_SEND, ReqMethod.CHAT_ANSWER,
])
def test_stored_project_cannot_be_hidden_by_forged_wire_identity(protected, method):
    item, store = protected
    req = request(method, session_id="private-session", project_id="default")
    with pytest.raises(ProjectAccessDenied):
        authorize_resource_request(req, None, access_store=store)
    authorize_resource_request(req, TrustedIdentity("owner", "owner", "test-host"), access_store=store)


def test_reader_cannot_execute_or_delete_and_revocation_is_immediate(protected):
    item, store = protected
    reader = TrustedIdentity("reader", "reader", "test-host")
    read = request(ReqMethod.HISTORY_GET, session_id="private-session")
    authorize_resource_request(read, reader, access_store=store)
    for method in (ReqMethod.CHAT_SEND, ReqMethod.PROJECT_DELETE):
        with pytest.raises(ProjectAccessDenied):
            authorize_resource_request(request(method, project_id=item.project_id), reader, access_store=store)
    store.replace_acl(item.project_id, "owner", acl={}, expected_revision=2)
    with pytest.raises(ProjectAccessDenied):
        authorize_resource_request(read, reader, access_store=ProjectAccessStore())


@pytest.mark.parametrize("method", [
    ReqMethod.SESSION_LIST, ReqMethod.SESSION_ARCHIVED_LIST,
    ReqMethod.FILE_DOWNLOAD_VERIFIED_CHUNK, ReqMethod.FILES_GET,
])
def test_unscoped_inventory_and_token_requests_cannot_use_fake_project_hint(protected, method):
    with pytest.raises(ProjectAccessDenied):
        authorize_resource_request(request(method, project_id="default", token="opaque"), None)


def test_directory_alias_and_lifecycle_inventory_cannot_hide_protected_project(protected):
    item, store = protected
    for req in (
        request(ReqMethod.CHAT_SEND, project_dir=item.project_dir),
        request(ReqMethod.PROJECT_LIFECYCLE, project_id="default", inventory=True),
    ):
        with pytest.raises(ProjectAccessDenied):
            authorize_resource_request(req, None)


@pytest.mark.asyncio
async def test_actual_agentserver_dispatch_rejects_before_lifecycle_mutation(protected):
    from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer

    item, _store = protected
    server = AgentWebSocketServer.__new__(AgentWebSocketServer)
    server._trusted_identity_resolver = lambda _request: None
    sent = []

    async def send(payload):
        sent.append(json.loads(payload) if isinstance(payload, str) else payload)

    await server._handle_message(
        SimpleNamespace(send=send),
        json.dumps({"request_id": "delete-denied", "channel_id": "web",
                    "req_method": "project.delete", "user_id": "owner",
                    "params": {"project_id": item.project_id}}),
        asyncio.Lock(),
    )
    assert sent and "PROJECT_ACCESS_DENIED" in json.dumps(sent)
    assert project_store.get_project_by_id(item.project_id, cache_bust=True) is not None


@pytest.mark.parametrize("method", [ReqMethod.MEMORY_LIST, ReqMethod.MEMORY_EDIT, ReqMethod.MEMORY_OPEN, ReqMethod.HARMONYOS_PROJECT_INIT])
def test_legacy_memory_and_workspace_initialization_respect_acl(protected, method):
    item, _store = protected
    with pytest.raises(ProjectAccessDenied):
        authorize_resource_request(request(method, trusted_dirs=[item.project_dir]), None)


def test_protected_session_cannot_be_rebound_to_unmanaged_project(protected):
    req = request(ReqMethod.SESSION_REBIND_PROJECT, session_id="private-session", project_dir="/new")
    with pytest.raises(ProjectAccessDenied, match="rebinding"):
        authorize_resource_request(req, TrustedIdentity("owner", "owner", "test-host"))


def test_parent_workspace_cannot_hide_nested_protected_project(protected):
    from pathlib import Path
    item, _store = protected
    with pytest.raises(ProjectAccessDenied):
        authorize_resource_request(request(ReqMethod.CHAT_SEND, project_id="default", project_dir=str(Path(item.project_dir).parent)), None)


def test_orphaned_protected_project_keeps_inventory_closed(protected):
    item, _store = protected
    project_store.delete_project(item.project_id)
    with pytest.raises(ProjectAccessDenied):
        authorize_resource_request(request(ReqMethod.SESSION_LIST), None)
