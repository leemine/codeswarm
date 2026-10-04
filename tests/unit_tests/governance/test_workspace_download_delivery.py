"""Actual owner permits and consumers; synthetic transport and temporary files."""

from io import BytesIO
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from jiuwenswarm.agents.harness.common.tools.web_file_download import (
    WebFileDownloadManager,
)
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.governance.organization_auth import current_identity
from jiuwenswarm.governance.session_boundary import admit_session_request
from jiuwenswarm.governance.project_boundary import authorize_resource_request
from jiuwenswarm.gateway.channel_manager.web.container_file_http import (
    attach_container_file_routes,
)
from jiuwenswarm.gateway.channel_manager.web.organization_auth_http import (
    register_organization_auth,
)
from jiuwenswarm.server.runtime.gateway_adapter.workspace_file_adapter import (
    WorkspaceFileAdapter,
)
from tests.unit_tests.governance import test_workspace_download as sources

credentials, setup = sources.credentials, sources.setup


@pytest.fixture
def installed(setup, monkeypatch):
    s = setup
    monkeypatch.setattr(WebFileDownloadManager, "_instance", s.manager)
    adapter = WorkspaceFileAdapter(
        sharing_host=s.host, identity_resolver=lambda _: current_identity()
    )
    calls = []

    async def fetch(**kwargs):
        request = AgentRequest(
            request_id="bounded",
            channel_id=kwargs["channel_id"],
            session_id=kwargs["session_id"],
            req_method=kwargs["req_method"],
            params=kwargs["params"],
        )
        permit = admit_session_request(
            request.req_method.value,
            request.params,
            identity_resolver=current_identity,
            host=s.host,
            envelope_session=request.session_id,
        )
        authorize_resource_request(
            request, current_identity(), access_store=s.access, session_permit=permit
        )
        response = await adapter.handle(request)
        if response.ok:
            response._delivery_guard()
        calls.append(request)
        return response.ok, response.payload

    monkeypatch.setattr(
        "jiuwenswarm.gateway.routing.e2a_proxy.fetch_agent_unary", fetch
    )
    monkeypatch.setattr(
        "jiuwenswarm.gateway.channel_manager.web.workspace_download_http.organization_sharing_host",
        lambda: s.host,
    )
    app = FastAPI()
    from jiuwenswarm.gateway.routing.agent_client import WebSocketAgentServerClient

    route = WebSocketAgentServerClient()
    route._uri = "ws://127.0.0.1:12345"
    route._ws = SimpleNamespace(remote_address=("127.0.0.1", 12345))
    route._server_ready = True
    channel = SimpleNamespace(channel_id="web", agent_client=route)
    attach_container_file_routes(app, channel)
    register_organization_auth(app)
    return SimpleNamespace(**locals())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method,range_value",
    [
        ("GET", None),
        ("HEAD", None),
        ("GET", "bytes=2-8"),
        ("GET", "bytes=-9"),
        ("GET", "bytes=999999-"),
    ],
)
async def test_owner_download_actual_adapter_and_http(installed, method, range_value):
    i = installed
    s = i.s
    headers = {"Authorization": "Bearer " + s.tokens["alice"]}
    if range_value:
        headers["Range"] = range_value
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=i.app), base_url="http://local"
    ) as client:
        reply = await client.request(
            method,
            "/file-api/download",
            params={"token": s.token(), "session_id": "alice-session"},
            headers=headers,
        )
    expected = 416 if range_value == "bytes=999999-" else 206 if range_value else 200
    assert reply.status_code == expected, reply.text
    if method == "HEAD" or expected == 416:
        assert reply.content == b""
    elif range_value == "bytes=2-8":
        assert reply.content == s.file.read_bytes()[2:9]
    elif range_value == "bytes=-9":
        assert reply.content == s.file.read_bytes()[-9:]
    else:
        assert reply.content == s.file.read_bytes()
    assert all(c.params["limit"] <= 65536 for c in i.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change", ["bob", "sid", "old_token", "channel", "extra", "sealed"]
)
async def test_other_owner_and_unscoped_selectors_denied_before_read(installed, change):
    i = installed
    s = i.s
    params = {"token": s.token(), "session_id": "alice-session"}
    actor = "alice"
    if change == "bob":
        actor = "bob"
    elif change == "sid":
        params["session_id"] = "bob-session"
    elif change == "old_token":
        params["token"] = s.manager._sign_payload(
            {"path": str(s.file), "sid": "alice-session"}
        )
    elif change == "channel":
        i.channel.channel_id = "other"
    elif change == "extra":
        params["path"] = str(s.file)
    elif change == "sealed":
        params["token"] = s.manager._sign_payload(
            {"kind": "verified_asset_v1", "path": str(s.file), "sid": "alice-session"}
        )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=i.app), base_url="http://local"
    ) as client:
        reply = await client.get(
            "/file-api/download",
            params=params,
            headers={"Authorization": "Bearer " + s.tokens[actor]},
        )
    assert reply.status_code == 403
    assert not i.calls


@pytest.mark.asyncio
async def test_adapter_offload_return_rechecks_original_permit(installed, monkeypatch):
    from jiuwenswarm.governance.organization_auth import authenticated_scope

    i = installed
    s = i.s
    original = i.adapter._handle_workspace_download_chunk.__globals__[
        "asyncio"
    ].to_thread

    async def after_read(func, *args):
        data = await original(func, *args)
        s.auth.revoke(s.alice)
        return data

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.gateway_adapter.workspace_file_adapter.asyncio.to_thread",
        after_read,
    )
    request = AgentRequest(
        request_id="r",
        channel_id="web",
        session_id="alice-session",
        req_method=ReqMethod.FILE_DOWNLOAD_WORKSPACE_CHUNK,
        params={"token": s.token(), "offset": 0, "limit": 3},
    )
    with authenticated_scope(s.alice):
        reply = await i.adapter.handle(request)
    assert not reply.ok and "data" not in reply.payload


@pytest.mark.asyncio
async def test_actual_asgi_final_body_gate_drops_buffer_after_file_change(installed):
    i = installed
    s = i.s

    # This middleware is inside the original auth final-send guard.
    class ChangeBeforeSend:
        def __init__(self, app):
            self.app = app

        async def __call__(self, scope, receive, send):
            changed = False

            async def mutate(message):
                nonlocal changed
                if (
                    message["type"] == "http.response.body"
                    and message.get("body")
                    and not changed
                ):
                    changed = True
                    s.file.write_bytes(b"replaced")
                await send(message)

            await self.app(scope, receive, mutate)

    app = FastAPI()
    attach_container_file_routes(app, i.channel)
    app.add_middleware(ChangeBeforeSend)
    register_organization_auth(app)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://local",
    ) as client:
        reply = await client.get(
            "/file-api/download",
            params={"token": s.token(), "session_id": "alice-session"},
            headers={"Authorization": "Bearer " + s.tokens["alice"]},
        )
    assert reply.content == b""
    assert len(i.calls) <= 1


def test_static_proxy_discards_chunk_changed_after_read(setup):
    from jiuwenswarm.channels.web.app_web import _SpaStaticHandler

    s = setup
    permit = s.capture()
    handler = _SpaStaticHandler.__new__(_SpaStaticHandler)
    handler.headers = {}
    handler.command = "GET"
    handler.wfile = BytesIO()
    handler.close_connection = False
    handler.send_response = lambda *_: None
    handler.send_header = lambda *_: None
    handler.end_headers = lambda: None
    handler._write_json = lambda *_: pytest.fail("headers already sent")

    class Upstream:
        status = 200

        def getheader(self, key):
            return str(permit.size) if key == "Content-Length" else None

        def read(self, size):
            data = s.file.read_bytes()[:size]
            s.file.write_bytes(b"changed")
            return data

    handler._proxy_workspace_response(Upstream(), permit)
    assert handler.wfile.getvalue() == b"" and handler.close_connection


def test_revoked_while_headers_buffered_never_flushes_success_headers(setup):
    from jiuwenswarm.channels.web.app_web import _SpaStaticHandler

    s = setup
    permit = s.capture()
    handler = _SpaStaticHandler.__new__(_SpaStaticHandler)
    handler.headers = {}
    handler.command = "GET"
    handler.request_version = "HTTP/1.1"
    handler.requestline = "GET /file-api/download HTTP/1.1"
    handler.wfile = BytesIO()
    handler.close_connection = False
    handler.log_message = lambda *_: None
    send_header = handler.send_header
    revoked = False

    def buffered(key, value):
        nonlocal revoked
        send_header(key, value)
        if key == "Content-Disposition" and not revoked:
            revoked = True
            s.auth.revoke(s.alice)

    handler.send_header = buffered

    class Response:
        status = 200

        def getheader(self, key):
            return {
                "Content-Length": str(permit.size),
                "Content-Disposition": 'attachment; filename="private-report.txt"',
            }.get(key)

        def read(self, _):
            raise AssertionError("no body should be read after revoke")

    handler._proxy_workspace_response(Response(), permit)
    output = handler.wfile.getvalue()
    assert b"200 OK" not in output, output
    assert b"private-report.txt" not in output, output
    assert b"403 Forbidden" in output


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change", ["unknown", "remote_uri", "remote_peer", "not_ready", "wrong_port"]
)
async def test_nonlocal_route_never_reads(installed, change):
    i = installed
    if change == "unknown":
        i.channel.agent_client = object()
    elif change == "remote_uri":
        i.route._uri = "ws://192.0.2.1:12345"
    elif change == "remote_peer":
        i.route._ws.remote_address = ("192.0.2.1", 12345)
    elif change == "wrong_port":
        i.route._ws.remote_address = ("127.0.0.1", 44444)
    else:
        i.route._server_ready = False
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=i.app), base_url="http://local"
    ) as client:
        reply = await client.get(
            "/file-api/download",
            params={"token": i.s.token(), "session_id": "alice-session"},
            headers={"Authorization": "Bearer " + i.s.tokens["alice"]},
        )
    assert reply.status_code == 403
    assert not i.calls


@pytest.mark.asyncio
async def test_changed_route_discards_returned_chunk(installed, monkeypatch):
    i = installed

    async def changed(**kwargs):
        response = await i.fetch(**kwargs)
        i.route._ws = SimpleNamespace(remote_address=("127.0.0.1", 12345))
        return response

    monkeypatch.setattr(
        "jiuwenswarm.gateway.routing.e2a_proxy.fetch_agent_unary", changed
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=i.app), base_url="http://local"
    ) as client:
        reply = await client.get(
            "/file-api/download",
            params={"token": i.s.token(), "session_id": "alice-session"},
            headers={"Authorization": "Bearer " + i.s.tokens["alice"]},
        )
    assert reply.status_code == 403
    assert b"fixture content" not in reply.content
    assert len(i.calls) == 1
