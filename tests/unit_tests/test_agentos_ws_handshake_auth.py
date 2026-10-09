# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

from typing import Any

import pytest

from jiuwenswarm.extensions.agentos.auth.credential_authenticator import AuthResult
from jiuwenswarm.gateway.app_gateway import GatewayServer, GatewayServerConfig, RouteConfig
from jiuwenswarm.gateway.channel_manager.base import RobotMessageRouter
from jiuwenswarm.gateway.channel_manager.web.web_connect import WebChannel, WebChannelConfig


async def _deny(**_kwargs: Any) -> AuthResult:
    return AuthResult(success=False, error="缺少 token")


@pytest.mark.asyncio
async def test_gateway_server_process_request_rejects_tui_not_acp() -> None:
    tui = WebChannel(WebChannelConfig(enabled=True), RobotMessageRouter())
    tui.set_handshake_auth(_deny)
    server = GatewayServer(
        GatewayServerConfig(
            enabled=True,
            routes={
                "/tui": RouteConfig(path="/tui", channel_id="tui", ws_channel=tui),
                "/acp": RouteConfig(path="/acp", channel_id="acp"),
            },
        ),
        RobotMessageRouter(),
    )
    status, _headers, body = await server._process_request("/tui", {})
    assert int(status) == 401
    assert await server._process_request("/acp", {}) is None


@pytest.mark.asyncio
async def test_verified_iam_user_wins_over_client_routing_hints(monkeypatch):
    from types import SimpleNamespace
    monkeypatch.delenv('JIUWENSWARM_ORGANIZATION_AUTH_FILE', raising=False)
    channel = WebChannel(WebChannelConfig(enabled=True), RobotMessageRouter())
    async def allow(**kwargs):
        return AuthResult(True, user_id='alice', extensions={'auth_method': 'token'})
    channel.set_handshake_auth(allow)
    ws = SimpleNamespace(path='/ws?user_id=bob', request_headers={'X-User-Id': 'bob'})
    await channel.bind_authenticated_identity(ws)
    assert channel._resolve_connection_user_id({'user_id': 'bob'}, ws) == 'alice'


@pytest.mark.asyncio
async def test_failed_or_empty_iam_identity_cannot_bind_a_connection(monkeypatch):
    from types import SimpleNamespace
    monkeypatch.delenv('JIUWENSWARM_ORGANIZATION_AUTH_FILE', raising=False)
    channel = WebChannel(WebChannelConfig(enabled=True), RobotMessageRouter())
    for result in (AuthResult(False), AuthResult(True, extensions={'auth_method': 'token'})):
        async def auth(**kwargs):
            return result
        channel.set_handshake_auth(auth)
        with pytest.raises(PermissionError):
            await channel.bind_authenticated_identity(SimpleNamespace(path='/ws', request_headers={}))
