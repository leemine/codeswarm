"""Organization mode denies unscoped Gateway paths before storage/backend IO."""

from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.governance.organization_auth import (
    CONFIG_ENV,
    configured_authenticator,
)
from jiuwenswarm.gateway.routing.agent_client import WebSocketAgentServerClient
from jiuwenswarm.gateway.routing.e2a_proxy import (
    _run_legacy_shared_directory_adapter,
    fetch_agent_unary,
    proxy_unary_request,
)
from jiuwenswarm.gateway.channel_manager.web.app_web_handlers import (
    WebHandlersBindParams,
    _register_web_handlers,
)
from jiuwenswarm.gateway.channel_manager.web.trajectory_http import (
    attach_trajectory_routes,
    TrajectoryHttpService,
)
from jiuwenswarm.gateway.channel_manager.web.container_file_http import (
    attach_container_file_routes,
)
from jiuwenswarm.gateway.channel_manager.web.git_ws_handler import (
    GitDiffWebSocketHandler,
)
from jiuwenswarm.observability.config import TrajectoryStoreSettings

CODE = "ORGANIZATION_AUTHORITY_REQUIRED"


@pytest.fixture
def organization(tmp_path, monkeypatch):
    path = tmp_path / "organization.json"
    path.write_text(
        json.dumps(
            {
                "authority": "organization:boundary-test",
                "signing_key": "b2" * 32,
                "credentials": [
                    {
                        "actor_id": "alice",
                        "sha256": hashlib.sha256(
                            b"only-a-test-credential-00000000000"
                        ).hexdigest(),
                        "expires_at": 4102444800,
                        "revoked": False,
                    }
                ],
            }
        )
    )
    path.chmod(0o600)
    monkeypatch.setenv(CONFIG_ENV, str(path))
    authenticator = configured_authenticator()
    assert authenticator is not None
    principal = authenticator.principal(
        {"Authorization": "Bearer only-a-test-credential-00000000000"}
    )
    assert principal.identity().actor_id == "alice"
    return path


class Channel:
    channel_id = "web"

    def __init__(self):
        self.methods = {}
        self.responses = []

    def register_method(self, name, handler):
        self.methods[name] = handler

    def on_connect(self, handler):
        pass

    def on_disconnect(self, handler):
        pass

    async def send_response(self, ws, req_id, **kwargs):
        self.responses.append(kwargs)

    async def send_event(self, *args, **kwargs):
        pass

    def is_session_busy(self, session_id):
        return False


def forbidden(*args, **kwargs):
    pytest.fail("unscoped private data access reached")


def block_local_session_reads(monkeypatch):
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.session_metadata.get_all_sessions_metadata",
        forbidden,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.gateway_adapter.session_adapter.get_all_sessions_metadata",
        forbidden,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.session_rename.apply_session_rename",
        forbidden,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.session_metadata.set_session_pinned",
        forbidden,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method",
    [
        ReqMethod.SESSION_LIST,
        ReqMethod.SESSION_GET_METADATA,
        ReqMethod.SESSION_RENAME,
        ReqMethod.SESSION_PIN,
        ReqMethod.FILES_GET,
        ReqMethod.CONFIG_GET,
    ],
)
async def test_all_legacy_adapter_methods_deny_before_dispatch(
    organization, monkeypatch, method
):
    block_local_session_reads(monkeypatch)
    result = await _run_legacy_shared_directory_adapter(
        channel_id="web",
        req_id="test",
        params={
            "session_id": "private",
            "user_id": "alice",
            "organization_auth": False,
        },
        session_id="private",
        user_id="alice",
        req_method=method,
    )
    assert result[0] is False and result[1]["code"] == CODE


@pytest.mark.asyncio
async def test_offline_proxy_and_fetch_do_not_fall_back(organization, monkeypatch):
    block_local_session_reads(monkeypatch)
    client = WebSocketAgentServerClient()
    channel = Channel()
    await proxy_unary_request(
        channel=channel,
        agent_client=client,
        ws=object(),
        req_id="offline",
        params={"actor_id": "alice"},
        session_id="private",
        user_id="alice",
        req_method=ReqMethod.SESSION_LIST,
    )
    assert (
        channel.responses[-1]["ok"] is False and channel.responses[-1]["code"] == CODE
    )
    ok, payload = await fetch_agent_unary(
        agent_client=client,
        req_method=ReqMethod.SESSION_LIST,
        params={},
        session_id="private",
        user_id="alice",
        channel_id="web",
    )
    assert not ok and payload["code"] == CODE


@pytest.mark.asyncio
async def test_live_proxy_failure_never_downgrades_to_local_storage(
    organization, monkeypatch
):
    block_local_session_reads(monkeypatch)
    client = WebSocketAgentServerClient()
    monkeypatch.setattr(type(client), "server_ready", property(lambda _: True))
    client.send_request = AsyncMock(side_effect=RuntimeError("test transport failure"))
    channel = Channel()
    await proxy_unary_request(
        channel=channel,
        agent_client=client,
        ws=object(),
        req_id="failed",
        params={},
        session_id="private",
        user_id="alice",
        req_method=ReqMethod.SESSION_LIST,
    )
    assert channel.responses[-1]["code"] == CODE


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["session.list", "session.rename", "session.pin"])
async def test_direct_web_offline_paths_deny_before_read_or_write(
    organization, monkeypatch, method
):
    block_local_session_reads(monkeypatch)
    channel = Channel()
    _register_web_handlers(
        WebHandlersBindParams(
            channel=channel, agent_client=WebSocketAgentServerClient()
        )
    )
    await channel.methods[method](
        object(),
        "request",
        {
            "session_id": "private",
            "title": "stolen",
            "pinned": True,
            "organization": False,
            "identity": {"actor_id": "alice"},
        },
        "private",
        user_id="alice",
    )
    assert (
        channel.responses[-1]["ok"] is False and channel.responses[-1]["code"] == CODE
    )


@pytest.mark.asyncio
async def test_configured_but_missing_auth_file_does_not_restore_fallback(
    organization, monkeypatch
):
    organization.unlink()
    block_local_session_reads(monkeypatch)
    result = await _run_legacy_shared_directory_adapter(
        channel_id="web",
        req_id="test",
        params={},
        session_id="private",
        user_id="alice",
        req_method=ReqMethod.SESSION_LIST,
    )
    assert result[0] is False and result[1]["code"] == CODE


def trajectory_settings(tmp_path):
    return TrajectoryStoreSettings(
        enabled=True,
        database_path=tmp_path / "private.sqlite",
        retention_days=7,
        queue_size=16,
        batch_size=8,
        flush_interval_ms=20,
    )


@pytest.mark.asyncio
async def test_trajectory_entire_surface_and_direct_service_fail_closed(
    organization, tmp_path
):
    app = FastAPI()
    settings = trajectory_settings(tmp_path)
    attach_trajectory_routes(
        app,
        SimpleNamespace(),
        settings=settings,
        reader=SimpleNamespace(),
        metadata_loader=forbidden,
    )

    @app.get("/health")
    async def health():
        return {"ok": True}

    routes = [
        route.path.replace("{session_id}", "private")
        .replace("{subject_id}", "subject")
        .replace("{trace_id}", "a" * 32)
        .replace("{span_id}", "b" * 16)
        for route in app.routes
        if route.path.startswith("/api/trajectory")
    ]
    routes += ["/api/trajectory", "/api/trajectory/future-alias"]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        for route in routes:
            for method in ("GET", "POST", "HEAD"):
                response = await client.request(
                    method,
                    route,
                    params={"user_id": "alice", "share_id": "forged"},
                    headers={"X-User-Id": "alice"},
                )
                assert response.status_code == 403, (method, route, response.text)
                assert response.headers["cache-control"] == "no-store"
                if method != "HEAD":
                    assert response.json()["code"] == CODE
        assert (await client.get("/health")).json() == {"ok": True}
    service = TrajectoryHttpService(
        settings, reader=SimpleNamespace(), metadata_loader=forbidden
    )
    assert (await service.list_subjects("private", after_revision=0)).status_code == 403
    assert not settings.database_path.exists()


@pytest.mark.asyncio
async def test_file_surface_denies_tokens_ranges_unknown_routes_before_container_auth(
    organization,
):
    from jiuwenswarm.extensions.agentos.agentos_router.router_client import (
        AgentOSRouterClient,
    )

    router = object.__new__(AgentOSRouterClient)
    router.authenticate_http = AsyncMock(
        side_effect=AssertionError("container auth reached")
    )
    app = FastAPI()
    attach_container_file_routes(app, SimpleNamespace(container_file_client=router))

    @app.get("/health")
    async def health():
        return {"ok": True}

    routes = [route.path for route in app.routes if route.path.startswith("/file-api")]
    routes += ["/file-api", "/file-api/future-alias"]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        for route in routes:
            for method in ("GET", "POST", "HEAD"):
                response = await client.request(
                    method,
                    route,
                    params={
                        "user_id": "alice",
                        "session_id": "private",
                        "token": "copied",
                        "inline": "1",
                    },
                    headers={
                        "Range": "bytes=0-10",
                        "X-User-Id": "alice",
                        "Authorization": "Bearer copied",
                    },
                )
                assert response.status_code == 403, (method, route, response.text)
                assert response.headers["cache-control"] == "no-store"
                if method != "HEAD":
                    assert response.json()["code"] == CODE
        assert (await client.get("/health")).status_code == 200
    router.authenticate_http.assert_not_called()


@pytest.mark.asyncio
async def test_git_connection_and_direct_messages_reject_before_watchers(organization):
    channel = Channel()
    registry = SimpleNamespace(
        add_watch=AsyncMock(side_effect=AssertionError("watch created"))
    )
    handler = GitDiffWebSocketHandler(channel, registry)
    ws = SimpleNamespace(close=AsyncMock())
    # Deliberately no async iterator: the guard must precede the receive loop.
    await handler.handle_connection(
        ws, {"user_id": "alice", "session_id": "private", "organization": "false"}
    )
    ws.close.assert_awaited_once()
    assert ws.close.call_args.kwargs["code"] == 1008
    for method in (
        "project.git.diff_watch",
        "project.git.diff_detail_watch",
        "project.git.diff_files_watch",
        "project.git.discard_turn_changes",
        "project.git.redo_turn_changes",
        "future.alias",
    ):
        await handler._handle_message(
            ws,
            json.dumps(
                {
                    "type": "req",
                    "id": "r",
                    "method": method,
                    "params": {
                        "session_id": "private",
                        "actor_id": "alice",
                        "organization": False,
                    },
                }
            ),
        )
    assert ws.close.await_count == 7 and not channel.responses
    registry.add_watch.assert_not_called()


@pytest.mark.asyncio
async def test_unconfigured_git_keeps_original_dispatch_and_ignores_request_mode(
    monkeypatch,
):
    monkeypatch.delenv(CONFIG_ENV, raising=False)
    handler = GitDiffWebSocketHandler(Channel(), SimpleNamespace())
    handler._handle_diff_watch = AsyncMock()
    ws = SimpleNamespace(close=AsyncMock())
    params = {"session_id": "legacy", "organization": True}
    await handler._handle_message(
        ws,
        json.dumps(
            {
                "type": "req",
                "id": "r",
                "method": "project.git.diff_watch",
                "params": params,
            }
        ),
    )
    handler._handle_diff_watch.assert_awaited_once_with(ws, "r", params)
    ws.close.assert_not_called()


@pytest.mark.asyncio
async def test_organization_preserves_online_agent_forwarding(organization):
    channel = Channel()
    client = SimpleNamespace(
        server_ready=True,
        send_request=AsyncMock(
            return_value=SimpleNamespace(ok=True, payload={"sessions": [], "total": 0})
        ),
    )
    ws = SimpleNamespace(close=AsyncMock())
    await proxy_unary_request(
        channel=channel,
        agent_client=client,
        ws=ws,
        req_id="online",
        params={},
        session_id="own-session",
        user_id="alice",
        req_method=ReqMethod.SESSION_LIST,
    )
    assert channel.responses[-1]["ok"] is True
    client.send_request.assert_awaited_once()
    ws.close.assert_not_called()
