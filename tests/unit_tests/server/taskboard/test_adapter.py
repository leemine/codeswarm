from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.governance.contracts import TrustedIdentity
from jiuwenswarm.extensions.taskboard.extension import TaskboardApplicationPlugin
from jiuwenswarm.server.runtime.gateway_adapter import taskboard_adapter as module
from jiuwenswarm.extensions.sdk.application_plugin import ApplicationPluginServices


@pytest.fixture
def instance(tmp_path, monkeypatch):
    monkeypatch.setenv("JIUWENSWARM_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(module, "get_agent_root_dir", lambda: tmp_path)
    from jiuwenswarm.server.runtime.session import project_store

    monkeypatch.setattr(project_store, "get_agent_root_dir", lambda: tmp_path)
    monkeypatch.setattr(module, "taskboard_enabled", lambda: True)
    monkeypatch.setattr(
        module,
        "readable_reference_snapshot",
        lambda actor: (
            [],
            [
                {
                    "session_id": "own",
                    "channel_id": "web",
                    "work_mode": "code",
                    "title": "own",
                },
                {
                    "session_id": "foreign",
                    "channel_id": "web",
                    "work_mode": "code",
                    "title": "foreign",
                },
                {"session_id": "work", "channel_id": "web", "work_mode": "work"},
            ],
        ),
    )
    return tmp_path


def req(method, params):
    return AgentRequest(
        request_id="r",
        channel_id="web",
        user_id="forged",
        req_method=ReqMethod(method),
        params=params,
    )


@pytest.mark.asyncio
async def test_adapter_uses_trusted_owner_and_rejects_other_sessions(instance):
    alice = TrustedIdentity("alice", "alice", "test")

    class Host:
        def owner_revision(self, sid, identity):
            if sid != "own":
                raise PermissionError()
            return 1

    adapter = module.TaskboardAdapter(
        identity_resolver=lambda r: alice, session_host=Host()
    )
    response = await adapter.handle(
        req("taskboard.create", {"title": "first", "client_create_id": "r"})
    )
    assert response.ok
    task = response.payload["task"]
    response = await adapter.handle(
        req(
            "taskboard.update",
            {
                "task_id": task["task_id"],
                "expected_version": 1,
                "patch": {"linked_session_id": "foreign"},
            },
        )
    )
    assert not response.ok and response.payload["code"] == "REFERENCE_UNAVAILABLE"
    response = await adapter.handle(
        req(
            "taskboard.update",
            {
                "task_id": task["task_id"],
                "expected_version": 1,
                "patch": {"linked_session_id": "own"},
            },
        )
    )
    assert response.ok and response.payload["task"]["linked_session"]["title"] == "own"
    bob = module.TaskboardAdapter(
        identity_resolver=lambda r: TrustedIdentity("bob", "bob", "test"),
        session_host=Host(),
    )
    assert (await bob.handle(req("taskboard.list", {"status": "todo"}))).payload[
        "tasks"
    ] == []
    assert not (
        await module.TaskboardAdapter(session_host=Host()).handle(
            req("taskboard.list", {"status": "todo"})
        )
    ).ok


@pytest.mark.asyncio
async def test_disabled_preserves_store(instance, monkeypatch):
    adapter = module.TaskboardAdapter()
    assert (
        await adapter.handle(
            req("taskboard.create", {"title": "one", "client_create_id": "one"})
        )
    ).ok
    monkeypatch.setattr(module, "taskboard_enabled", lambda: False)
    result = await adapter.handle(req("taskboard.list", {"status": "todo"}))
    assert not result.ok and result.payload["code"] == "FEATURE_DISABLED"
    assert (instance / "taskboard.sqlite3").exists()


@pytest.mark.asyncio
async def test_plugin_unavailable_does_not_fallback(instance):
    handlers = {}
    channel = SimpleNamespace(
        register_method=lambda name, handler: handlers.update({name: handler}),
        send_response=AsyncMock(),
    )
    plugin = TaskboardApplicationPlugin()
    plugin.bind_web_channel(
        channel,
        ApplicationPluginServices(agent_client=SimpleNamespace(server_ready=False)),
    )
    await handlers["taskboard.create"](
        None, "r", {"title": "x", "client_create_id": "x"}, None, user_id="alice"
    )
    assert channel.send_response.call_args.kwargs["code"] == "SERVICE_UNAVAILABLE"
    assert not (instance / "taskboard.sqlite3").exists()


@pytest.mark.asyncio
async def test_application_wrapper_preserves_host_route_user_id():
    from jiuwenswarm.extensions.registry import _ApplicationPluginChannel

    registered = {}
    channel = SimpleNamespace(
        register_method=lambda name, handler, **kw: registered.update({name: handler}),
        send_response=AsyncMock(),
    )
    handler = AsyncMock()

    async def routed(ws, req_id, params, session_id, user_id=None):
        await handler(user_id)

    _ApplicationPluginChannel(channel, TaskboardApplicationPlugin()).register_method(
        "taskboard.get", routed
    )
    await registered["taskboard.get"](
        None, "r", {}, None, user_id="trusted-connection-user"
    )
    handler.assert_awaited_once_with("trusted-connection-user")


@pytest.mark.asyncio
async def test_reference_acl_revocation_and_corruption_are_live(instance, monkeypatch):
    from jiuwenswarm.server.runtime.session import project_store
    from jiuwenswarm.server.runtime.session.project_access import ProjectAccessStore
    from jiuwenswarm.server.runtime.gateway_adapter.project_adapter import (
        readable_reference_snapshot,
    )

    project_store.invalidate_cache()
    project = project_store.create_project(
        "Private Code", str(instance / "private"), work_mode="code"
    )
    access = ProjectAccessStore()
    access.migrate_legacy({project.project_id: "owner"})
    access.replace_acl(
        project.project_id, "owner", acl={"reader": ["read"]}, expected_revision=1
    )
    monkeypatch.setattr(
        module, "readable_reference_snapshot", readable_reference_snapshot
    )
    reader = module.TaskboardAdapter(
        identity_resolver=lambda _: TrustedIdentity("reader", "reader", "test")
    )
    created = await reader.handle(
        req(
            "taskboard.create",
            {
                "title": "private reference",
                "project_id": project.project_id,
                "client_create_id": "acl-task",
            },
        )
    )
    assert created.ok
    task = created.payload["task"]
    assert task["project"]["title"] == "Private Code"
    access.replace_acl(project.project_id, "owner", acl={}, expected_revision=2)
    read = await reader.handle(req("taskboard.get", {"task_id": task["task_id"]}))
    assert read.ok and read.payload["task"]["project"] == {
        "id": project.project_id,
        "available": False,
        "title": "",
    }
    denied = await reader.handle(
        req(
            "taskboard.create",
            {
                "title": "denied",
                "project_id": project.project_id,
                "client_create_id": "denied",
            },
        )
    )
    assert not denied.ok and denied.payload["code"] == "REFERENCE_UNAVAILABLE"
    access.path.write_text("{broken")
    failed_closed = await reader.handle(
        req("taskboard.get", {"task_id": task["task_id"]})
    )
    assert not failed_closed.ok and failed_closed.payload["code"] == "FORBIDDEN"
    project_store.invalidate_cache()


@pytest.mark.asyncio
async def test_wrapper_preserves_legacy_signature_and_disabled_policy(monkeypatch):
    from jiuwenswarm.extensions.registry import _ApplicationPluginChannel

    registered = {}
    channel = SimpleNamespace(
        register_method=lambda name, handler, **kw: registered.update({name: handler}),
        send_response=AsyncMock(),
    )
    called = AsyncMock()

    async def legacy(ws, req_id, params, session_id):
        await called(req_id)

    plugin = TaskboardApplicationPlugin()
    monkeypatch.setattr(plugin, "is_enabled", lambda: True)
    _ApplicationPluginChannel(channel, plugin).register_method("legacy", legacy)
    await registered["legacy"](None, "one", {}, None, user_id="identity")
    called.assert_awaited_once_with("one")
    monkeypatch.setattr(plugin, "is_enabled", lambda: False)
    await registered["legacy"](None, "two", {}, None, user_id="identity")
    called.assert_awaited_once_with("one")
    assert (
        channel.send_response.call_args.kwargs["code"] == "APPLICATION_PLUGIN_DISABLED"
    )
