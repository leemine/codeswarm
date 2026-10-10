"""Login must not re-install shared-directory Session governance in AgentOS."""

import asyncio
import hashlib
import json
import secrets
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from jiuwenswarm.governance.organization_auth import (
    CONFIG_ENV,
    GATEWAY_AUTH_ENV,
    configured_authenticator,
    configured_gateway_authenticator,
    connection_principal,
)
from jiuwenswarm.gateway.channel_manager.base import RobotMessageRouter
from jiuwenswarm.gateway.channel_manager.web.web_connect import (
    WebChannel,
    WebChannelConfig,
)
from jiuwenswarm.gateway.channel_manager.web.organization_auth_http import (
    register_organization_auth,
)


@pytest.fixture
def login(tmp_path, monkeypatch):
    token = secrets.token_urlsafe(32)
    path = tmp_path / "login.json"
    path.write_text(
        json.dumps(
            {
                "authority": "test:instance",
                "signing_key": secrets.token_hex(32),
                "credentials": [
                    {
                        "actor_id": "alice",
                        "sha256": hashlib.sha256(token.encode()).hexdigest(),
                        "expires_at": time.time() + 300,
                        "revoked": False,
                    }
                ],
            }
        )
    )
    path.chmod(0o600)
    monkeypatch.delenv(CONFIG_ENV, raising=False)
    monkeypatch.setenv(GATEWAY_AUTH_ENV, str(path))
    return configured_gateway_authenticator(), token, path


class Socket:
    def __init__(self, token):
        self.request_headers = {"Authorization": "Bearer " + token}
        self.remote_address = ("127.0.0.1", 1234)
        self.closed = False
        self.frames = []
        self.delivered = asyncio.Event()

    async def send(self, raw):
        self.frames.append(json.loads(raw))
        self.delivered.set()

    async def close(self, **kwargs):
        self.closed = True


def test_login_does_not_enable_runtime_governance(login):
    auth, token, _ = login
    assert configured_authenticator() is None
    from jiuwenswarm.governance.session_boundary import organization_sharing_host

    assert organization_sharing_host() is None
    ws = Socket(token)
    assert WebChannel._resolve_connection_user_id({"user_id": "bob"}, ws) == "alice"
    auth.revoke(connection_principal(ws))
    with pytest.raises(PermissionError):
        connection_principal(ws)


def test_conflicting_deployment_configuration_is_rejected(login, monkeypatch):
    monkeypatch.setenv(CONFIG_ENV, str(login[2]))
    with pytest.raises(PermissionError, match="cannot be combined"):
        configured_gateway_authenticator()


@pytest.mark.asyncio
async def test_websocket_upgrade_requires_login(login):
    channel = WebChannel(WebChannelConfig(enabled=True), RobotMessageRouter())
    assert await channel.handshake_auth_denied(
        path="/ws", headers={"X-User-Id": "alice"}
    )
    assert not await channel.handshake_auth_denied(
        path="/ws", headers={"Authorization": "Bearer " + login[1]}
    )


def test_frontend_proxies_all_instance_files_and_rejects_sharing(login):
    from jiuwenswarm.channels.web.app_web import _SpaStaticHandler
    from unittest.mock import Mock

    handler = object.__new__(_SpaStaticHandler)
    handler.command = "GET"
    handler.path = "/file-api/raw-file?path=/etc/passwd"
    handler._proxy_http = Mock()
    handler._write_json = Mock()
    assert handler._reject_organization_local_files() is True
    handler._proxy_http.assert_called_once_with()
    handler.path = "/share-api/session/image"
    assert handler._reject_organization_local_files() is True
    assert handler._write_json.call_args.args[0] == 501
    assert handler._write_json.call_args.args[1]["code"] == "NOT_SUPPORTED"


@pytest.mark.asyncio
async def test_instance_create_response_delivered_without_local_owner(login):
    channel = WebChannel(WebChannelConfig(enabled=True), RobotMessageRouter())
    ws = Socket(login[1])
    ws._jiuwen_ws_id = "instance-create"
    queue = asyncio.Queue()
    channel._send_queues[ws._jiuwen_ws_id] = queue
    response = {
        "type": "res",
        "id": "create",
        "ok": True,
        "payload": {"session_id": "remote_alice_session"},
    }
    channel._enqueue_send(ws, response)
    await queue.put(None)
    await channel._writer_loop(ws, ws._jiuwen_ws_id)
    assert ws.frames == [response]


@pytest.mark.asyncio
async def test_revocation_while_response_queued_prevents_delivery(login):
    channel = WebChannel(WebChannelConfig(enabled=True), RobotMessageRouter())
    ws = Socket(login[1])
    principal = connection_principal(ws)
    ws._jiuwen_ws_id = "revoked"
    queue = asyncio.Queue()
    channel._send_queues[ws._jiuwen_ws_id] = queue
    channel._enqueue_send(
        ws, {"type": "res", "id": "history", "ok": True, "payload": {"private": True}}
    )
    login[0].revoke(principal)
    await queue.put(None)
    await channel._writer_loop(ws, ws._jiuwen_ws_id)
    assert ws.frames == []


@pytest.mark.asyncio
async def test_share_is_explicitly_unsupported(login):
    channel = WebChannel(WebChannelConfig(enabled=True), RobotMessageRouter())
    channel.send_response = AsyncMock()
    channel.on_message(lambda msg: pytest.fail("sharing entered ordinary execution"))
    ws = Socket(login[1])
    await channel._handle_raw_message(
        ws,
        json.dumps(
            {
                "type": "req",
                "id": "share",
                "method": "session.share.create",
                "params": {"session_id": "other"},
            }
        ),
        {},
    )
    assert channel.send_response.call_args.kwargs["code"] == "NOT_SUPPORTED"


@pytest.mark.asyncio
async def test_login_status_files_identity_and_logout(login):
    from jiuwenswarm.extensions.agentos.agentos_router.router_client import (
        AgentOSRouterClient,
    )
    from jiuwenswarm.gateway.channel_manager.web.container_file_http import (
        attach_container_file_routes,
    )

    router = object.__new__(AgentOSRouterClient)
    router.list_container_files = AsyncMock(return_value=[])
    app = FastAPI()
    attach_container_file_routes(app, SimpleNamespace(container_file_client=router))
    register_organization_auth(app)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost"
    ) as client:
        assert (
            await client.get("/file-api/list-files", params={"dir": "/home/agentos"})
        ).status_code == 401
        result = await client.post(
            "/api/v1/auth/organization/login",
            headers={"x-jiuwen-auth": "1"},
            json={"token": login[1]},
        )
        assert result.status_code == 200
        status = (await client.get("/api/v1/auth/organization/status")).json()
        assert status["authenticated"] and status["execution_mode"] == "user_instance"
        assert status["sharing_enabled"] is False
        assert (await client.get("/share-api/example")).status_code == 501
        result = await client.get(
            "/file-api/list-files", params={"dir": "/home/agentos"}
        )
        assert result.status_code == 200, result.text
        assert router.list_container_files.call_args.kwargs["user_id"] == "alice"
        router.list_container_files.reset_mock()
        for kwargs in (
            {"params": {"dir": "/home/agentos", "user_id": "bob"}},
            {"params": {"dir": "/home/agentos"}, "headers": {"X-User-Id": "bob"}},
        ):
            assert (
                await client.get("/file-api/list-files", **kwargs)
            ).status_code == 403
        router.list_container_files.assert_not_called()
        assert (
            await client.post(
                "/api/v1/auth/organization/logout", headers={"x-jiuwen-auth": "1"}
            )
        ).status_code == 200
        assert (
            await client.get(
                "/file-api/list-files",
                headers={"Authorization": "Bearer " + login[1]},
                params={"dir": "/home/agentos"},
            )
        ).status_code == 401
