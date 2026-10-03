# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from httpx import AsyncClient, ASGITransport

from jiuwenswarm.governance.organization_auth import (
    ASSERTION,
    CONFIG_ENV,
    OrganizationAuthenticator,
    authenticated_scope,
    configured_authenticator,
    current_identity,
)


@pytest.fixture
def credentials(tmp_path, monkeypatch):
    tokens = {name: secrets.token_urlsafe(32) for name in ("alice", "bob")}
    path = tmp_path / "organization.json"
    config = {
        "authority": "organization:example",
        "signing_key": secrets.token_hex(32),
        "credentials": [
            {
                "actor_id": name,
                "sha256": hashlib.sha256(token.encode()).hexdigest(),
                "expires_at": time.time() + 3600,
                "revoked": False,
            }
            for name, token in tokens.items()
        ],
    }
    path.write_text(json.dumps(config))
    path.chmod(0o600)
    monkeypatch.setenv(CONFIG_ENV, str(path))
    auth = configured_authenticator()
    return auth, tokens, path


def principal(auth, tokens, name):
    return auth.principal(
        {"Authorization": "Bearer " + tokens[name], "X-User-Id": "forged"}
    )


def test_identity_ignores_routing_and_rechecks_current_revocation(credentials):
    auth, tokens, path = credentials
    alice = principal(auth, tokens, "alice")
    assert alice.identity().actor_id == "alice"
    assert tokens["alice"] not in repr(alice)
    with authenticated_scope(alice):
        assert current_identity().actor_id == "alice"
        auth.revoke(alice)
        with pytest.raises(PermissionError):
            current_identity()
    with pytest.raises(PermissionError):
        OrganizationAuthenticator(path).principal(
            {"Authorization": "Bearer " + tokens["alice"]}
        )
    assert principal(auth, tokens, "bob").identity().actor_id == "bob"


def test_sharing_target_directory_never_accepts_supplied_authority_or_expired_actor(credentials):
    from jiuwenswarm.governance.contracts import TrustedIdentity

    auth, tokens, _ = credentials
    alice = principal(auth, tokens, "alice").identity()
    assert auth.resolve_actor(alice, "bob") == TrustedIdentity("bob", "bob", alice.authority)
    assert auth.resolve_actor(alice, "unknown") is None
    assert auth.resolve_actor(TrustedIdentity("alice", "alice", "foreign"), "bob") is None
    auth.revoke(principal(auth, tokens, "bob"))
    assert auth.resolve_actor(alice, "bob") is None
    assert auth.known_actor(TrustedIdentity("bob", "bob", alice.authority)) is True
    assert auth.known_actor(TrustedIdentity("bob", "other", alice.authority)) is False
    assert auth.known_actor(TrustedIdentity("bob", "bob", "foreign")) is False
    assert auth.known_actor(TrustedIdentity("unknown", "unknown", alice.authority)) is False


@pytest.mark.parametrize(
    "mutation", ["body", "signature", "audience", "expired", "missing"]
)
def test_assertion_rejects_tampering(credentials, mutation):
    auth, tokens, _ = credentials
    with authenticated_scope(principal(auth, tokens, "alice")):
        wire = auth.sign({"request_id": "one", "method": "project.list", "params": {}})
    if mutation == "body":
        wire["params"] = {"actor_id": "bob"}
    elif mutation == "signature":
        wire[ASSERTION]["signature"] = "00" * 32
    elif mutation == "audience":
        wire[ASSERTION]["claims"]["aud"] = "elsewhere"
    elif mutation == "expired":
        wire[ASSERTION]["claims"]["exp"] = 0
    else:
        del wire[ASSERTION]
    with pytest.raises(PermissionError):
        auth.verify(wire)


def test_assertion_replay_restart_and_revocation(credentials):
    auth, tokens, path = credentials
    alice = principal(auth, tokens, "alice")
    with authenticated_scope(alice):
        wire = auth.sign({"request_id": "one"})
        pending = auth.sign({"request_id": "two"})
    assert auth.verify(wire).identity().actor_id == "alice"
    with pytest.raises(PermissionError):
        auth.verify(wire)
    with pytest.raises(PermissionError):
        OrganizationAuthenticator(path).verify(pending)
    auth.revoke(alice)
    with pytest.raises(PermissionError):
        auth.verify(pending)


def test_service_assertion_does_not_become_local_human(credentials):
    auth, _, _ = credentials
    assert auth.verify(auth.sign({"method": "config.get"})) is None
    assert current_identity() is None


def test_expired_and_unavailable_authority_fail_closed(credentials):
    auth, tokens, path = credentials
    config = json.loads(path.read_text())
    config["credentials"][0]["expires_at"] = 0
    path.write_text(json.dumps(config))
    with pytest.raises(PermissionError):
        principal(auth, tokens, "alice")
    path.unlink()
    with pytest.raises(OSError):
        principal(auth, tokens, "bob")


@pytest.mark.asyncio
async def test_queue_preserves_two_independent_contexts_and_rechecks(credentials):
    from jiuwenswarm.gateway.app_gateway import _InboundGatewayServer

    auth, tokens, _ = credentials
    results = []

    async def handle(message):
        await asyncio.sleep(0)
        try:
            results.append((message, current_identity().actor_id))
        except PermissionError:
            results.append((message, "denied"))

    server = _InboundGatewayServer(handle)
    for name in ("alice", "bob"):
        with authenticated_scope(principal(auth, tokens, name)):
            await server.handle_message(name)
    auth.revoke(principal(auth, tokens, "bob"))
    await server.start()
    try:
        async with asyncio.timeout(3):
            while not server._queue.empty() or len(results) < 1:
                await asyncio.sleep(0.01)
    finally:
        await server.stop()
    assert results == [("alice", "alice")]
    assert current_identity() is None


@pytest.mark.asyncio
async def test_http_login_logout_and_direct_api(credentials):
    from jiuwenswarm.gateway.channel_manager.web.organization_auth_http import (
        register_organization_auth,
    )

    auth, tokens, _ = credentials
    app = FastAPI()
    register_organization_auth(app)

    @app.get("/private")
    async def private():
        return {"actor": current_identity().actor_id}

    prefix = "/api/v1/auth/organization"
    async with (
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as alice,
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as bob,
    ):
        assert (await alice.get("/private")).status_code == 401
        assert (
            await alice.post(prefix + "/login", json={"token": tokens["alice"]})
        ).status_code == 403
        for client, name in ((alice, "alice"), (bob, "bob")):
            response = await client.post(
                prefix + "/login",
                json={"token": tokens[name]},
                headers={"X-Jiuwen-Auth": "1"},
            )
            assert response.status_code == 200
            assert "httponly" in response.headers["set-cookie"].lower()
            assert (
                await client.get("/private", headers={"X-User-Id": "forged"})
            ).json() == {"actor": name}
        assert (
            await alice.post(prefix + "/logout", headers={"X-Jiuwen-Auth": "1"})
        ).status_code == 200
        assert (await alice.get("/private")).status_code == 401
        assert (
            await alice.post(
                prefix + "/login",
                json={"token": tokens["alice"]},
                headers={"X-Jiuwen-Auth": "1"},
            )
        ).status_code == 401
        assert (await bob.get("/private")).json() == {"actor": "bob"}


@pytest.mark.asyncio
async def test_agentserver_rejects_unsigned_and_restores_context(credentials):
    from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer

    auth, tokens, _ = credentials
    server = object.__new__(AgentWebSocketServer)
    seen = []

    async def handle(ws, raw, lock):
        seen.append((current_identity().actor_id, json.loads(raw)))
        await asyncio.sleep(0)

    server._handle_authenticated_message = handle
    ws = SimpleNamespace(close=AsyncMock())
    await server._handle_message(
        ws, json.dumps({"request_id": "forged", "actor_id": "alice"}), asyncio.Lock()
    )
    ws.close.assert_awaited_once()

    async def send(name):
        with authenticated_scope(principal(auth, tokens, name)):
            wire = auth.sign({"request_id": name})
        await server._handle_message(ws, json.dumps(wire), asyncio.Lock())

    await asyncio.gather(send("alice"), send("bob"))
    assert sorted(seen) == [
        ("alice", {"request_id": "alice"}),
        ("bob", {"request_id": "bob"}),
    ]
    assert current_identity() is None


@pytest.mark.asyncio
async def test_web_routing_uses_authenticated_actor_and_active_connection_revokes(
    credentials,
):
    from jiuwenswarm.gateway.channel_manager.web.web_connect import (
        WebChannel,
        WebChannelConfig,
    )
    from jiuwenswarm.gateway.channel_manager.base import RobotMessageRouter

    auth, tokens, _ = credentials
    channel = WebChannel(WebChannelConfig(), RobotMessageRouter())
    ws = SimpleNamespace(
        request_headers={"Authorization": "Bearer " + tokens["alice"]},
        close=AsyncMock(),
    )
    assert channel._resolve_connection_user_id({"user_id": "bob"}, ws) == "alice"
    channel._handle_authenticated_raw_message = AsyncMock()
    await channel._handle_raw_message(ws, "{}", {})
    channel._handle_authenticated_raw_message.assert_awaited_once()
    auth.revoke(principal(auth, tokens, "alice"))
    await channel._handle_raw_message(ws, "{}", {})
    ws.close.assert_awaited_once()
    channel._handle_authenticated_raw_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_agentserver_upgrade_rejects_direct_and_replayed_handshake(
    credentials, monkeypatch
):
    from jiuwenswarm.server import agent_ws_server as module

    auth, _, _ = credentials
    monkeypatch.setattr(module, "is_origin_check_enabled", lambda: False)
    server = object.__new__(module.AgentWebSocketServer)
    result = await server._process_request("/", {})
    assert int(result[0]) == 401
    proof = json.dumps(auth.sign({"upgrade": "agentserver"}))
    assert (
        await server._process_request("/", {"X-Jiuwen-Gateway-Assertion": proof})
        is None
    )
    result = await server._process_request("/", {"X-Jiuwen-Gateway-Assertion": proof})
    assert int(result[0]) == 401


@pytest.mark.asyncio
async def test_http_failed_revoke_does_not_claim_logged_out(credentials):
    from jiuwenswarm.gateway.channel_manager.web.organization_auth_http import (
        register_organization_auth,
    )

    _, tokens, path = credentials
    app = FastAPI()
    register_organization_auth(app)
    prefix = "/api/v1/auth/organization"
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        await client.post(
            prefix + "/login",
            json={"token": tokens["alice"]},
            headers={"X-Jiuwen-Auth": "1"},
        )
        path.chmod(0o400)
        response = await client.post(prefix + "/logout", headers={"X-Jiuwen-Auth": "1"})
        assert response.status_code == 503
        assert "set-cookie" not in response.headers
        assert (await client.get(prefix + "/status")).json()["authenticated"] is True


def test_config_disabled_preserves_existing_mode(monkeypatch):
    monkeypatch.delenv(CONFIG_ENV, raising=False)
    assert configured_authenticator() is None


@pytest.mark.asyncio
async def test_http_stream_rechecks_revocation_before_next_chunk(credentials):
    from jiuwenswarm.gateway.channel_manager.web.organization_auth_http import (
        OrganizationAuthenticationMiddleware,
    )

    auth, tokens, _ = credentials
    alice = principal(auth, tokens, "alice")

    async def streaming(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"before", "more_body": True})
        auth.revoke(alice)
        await send(
            {
                "type": "http.response.body",
                "body": b"secret-after-revoke",
                "more_body": False,
            }
        )

    output = []

    async def send(message):
        output.append(message)

    await OrganizationAuthenticationMiddleware(streaming)(
        {
            "type": "http",
            "path": "/private",
            "headers": [(b"authorization", ("Bearer " + tokens["alice"]).encode())],
        },
        AsyncMock(),
        send,
    )
    assert [message.get("body") for message in output] == [None, b"before", b""]


def test_existing_connection_cannot_switch_actor_via_configuration(credentials):
    from jiuwenswarm.governance.organization_auth import connection_principal

    _, tokens, path = credentials
    ws = SimpleNamespace(request_headers={"Authorization": "Bearer " + tokens["alice"]})
    assert connection_principal(ws).identity().actor_id == "alice"
    config = json.loads(path.read_text())
    config["credentials"][0]["actor_id"] = "bob"
    path.write_text(json.dumps(config))
    with pytest.raises(PermissionError):
        connection_principal(ws)


def test_legacy_static_file_routes_require_scoped_replacement(credentials):
    from unittest.mock import Mock
    from jiuwenswarm.channels.web.app_web import _SpaStaticHandler

    handler = object.__new__(_SpaStaticHandler)
    handler._write_json = Mock()
    for path in ("/file-api/read?path=/private", "/share-api/read?token=old"):
        handler.path = path
        assert handler._reject_organization_local_files() is True
        assert handler._write_json.call_args.args[0] == 403
    handler.path = "/api/v1/auth/organization/status"
    assert handler._reject_organization_local_files() is False


@pytest.mark.asyncio
async def test_browser_cookie_origin_and_status_do_not_expose_credentials(credentials):
    from jiuwenswarm.gateway.channel_manager.web.organization_auth_http import (
        register_organization_auth,
    )

    _, tokens, _ = credentials
    app = FastAPI()
    register_organization_auth(app)
    prefix = "/api/v1/auth/organization"
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://localhost"
    ) as client:
        denied = await client.post(
            prefix + "/login",
            json={"token": tokens["alice"]},
            headers={"X-Jiuwen-Auth": "1", "Origin": "https://attacker.invalid"},
        )
        assert denied.status_code == 403
        response = await client.post(
            prefix + "/login",
            json={"token": tokens["alice"]},
            headers={"X-Jiuwen-Auth": "1", "Origin": "https://localhost"},
        )
        assert response.status_code == 200
        cookie = response.headers["set-cookie"].lower()
        assert (
            "httponly" in cookie and "secure" in cookie and "samesite=strict" in cookie
        )
        status = await client.get(prefix + "/status")
        assert status.json() == {
            "enabled": True,
            "authenticated": True,
            "actor_id": "alice",
        }
        assert tokens["alice"] not in response.text + status.text
        assert (
            hashlib.sha256(tokens["alice"].encode()).hexdigest()
            not in response.text + status.text
        )
        assert (
            await client.post(
                prefix + "/logout",
                headers={"X-Jiuwen-Auth": "1", "Origin": "https://attacker.invalid"},
            )
        ).status_code == 403
        assert (await client.get(prefix + "/status")).json()["authenticated"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize('reuse_message', [False, True])
async def test_gateway_queue_retains_each_live_principal_without_wire_fields(credentials, reuse_message):
    from dataclasses import asdict
    from jiuwenswarm.common.schema.message import Message, ReqMethod
    from jiuwenswarm.gateway.message_handler.message_handler import MessageHandler
    auth, tokens, _ = credentials
    handler = object.__new__(MessageHandler)
    handler._user_messages = asyncio.Queue()
    handler._running = True
    seen, tasks = [], []
    handler._is_session_input_message = lambda _: False
    async def control(msg):
        async def child():
            signed = auth.sign({'request_id': msg.id})
            seen.append((msg.id, auth.verify(signed).identity().actor_id))
        tasks.append(asyncio.create_task(child()))
        if msg.id == 'bob':
            handler._running = False
        return True
    handler._handle_channel_control = control
    reused = Message('pending', 'req', 'web', None, {}, time.time(), True, req_method=ReqMethod.SESSION_CREATE)
    for actor in ('alice', 'bob'):
        msg = reused if reuse_message else Message(actor, 'req', 'web', None, {}, time.time(), True, req_method=ReqMethod.SESSION_CREATE)
        msg.id = actor
        msg._queued_organization_principal = object()  # Ignore any preexisting value.
        with authenticated_scope(principal(auth, tokens, actor)):
            handler.publish_user_messages_nowait(msg)
        assert '_queued_organization_principal' not in asdict(msg)
    await handler._forward_loop()
    await asyncio.gather(*tasks)
    assert seen == [('alice', 'alice'), ('bob', 'bob')]
    assert current_identity() is None


@pytest.mark.asyncio
async def test_gateway_queue_rechecks_revoked_principal_before_controls(credentials):
    from jiuwenswarm.common.schema.message import Message, ReqMethod
    from jiuwenswarm.gateway.message_handler.message_handler import MessageHandler
    auth, tokens, _ = credentials
    handler = object.__new__(MessageHandler)
    handler._user_messages = asyncio.Queue()
    handler._running = True
    control = AsyncMock(return_value=True)
    handler._is_session_input_message = lambda _: False
    handler._handle_channel_control = control
    errors = []
    async def error_response(msg):
        errors.append(msg)
        handler._running = False
    handler.publish_robot_messages = error_response
    msg = Message('revoked', 'req', 'web', None, {}, time.time(), True, req_method=ReqMethod.SESSION_CREATE)
    alice = principal(auth, tokens, 'alice')
    with authenticated_scope(alice):
        await handler.publish_user_messages(msg)
    auth.revoke(alice)
    await handler._forward_loop()
    assert len(errors) == 1 and not errors[0].ok
    control.assert_not_awaited()
    assert current_identity() is None
