"""Bounded owner Workspace downloads on the existing local HTTP surface."""

from __future__ import annotations

import base64
import mimetypes
import ipaddress
from urllib.parse import quote, urlsplit

from fastapi.responses import JSONResponse, Response, StreamingResponse

from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.governance.organization_auth import (
    authenticated_scope,
    configured_authenticator,
)
from jiuwenswarm.governance.session_boundary import organization_sharing_host
from jiuwenswarm.governance.workspace_download import (
    MAX_DOWNLOAD_CHUNK_BYTES,
    WorkspaceDownloadPermit,
    workspace_send_scope,
)
from jiuwenswarm.agents.harness.common.tools.web_file_download import (
    validate_file_download_token,
)

GUARD_KEY = "_jiuwen_workspace_download_guard"


def require_loopback_url(url, *, schemes):
    parsed = urlsplit(url)
    if (
        parsed.scheme not in schemes
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or not parsed.hostname
        or (
            parsed.hostname != "localhost"
            and not ipaddress.ip_address(parsed.hostname).is_loopback
        )
    ):
        raise PermissionError("local download route required")
    return parsed


def capture_local_agent_route(channel):
    from jiuwenswarm.gateway.routing.agent_client import WebSocketAgentServerClient

    client = getattr(channel, "agent_client", None)
    if type(client) is not WebSocketAgentServerClient:
        raise PermissionError("original local AgentServer route required")
    uri, ws = client._uri, client._ws
    parsed = require_loopback_url(uri, schemes={"ws", "wss"})
    peer = getattr(ws, "remote_address", None)
    if (
        not isinstance(peer, tuple)
        or len(peer) < 2
        or not ipaddress.ip_address(peer[0]).is_loopback
        or peer[1] != (parsed.port or (443 if parsed.scheme == "wss" else 80))
    ):
        raise PermissionError("connected local AgentServer peer required")

    def check():
        if (
            getattr(channel, "agent_client", None) is not client
            or client._ws is not ws
            or client._uri != uri
            or not client.server_ready
            or ws.remote_address != peer
        ):
            raise PermissionError("original download route changed")

    check()
    return client, check


def capture_http_download(headers, query):
    """Same-host source checks; the URL only selects an already issued artifact."""
    auth = configured_authenticator()
    if auth is None or set(query) - {"token", "session_id", "inline"}:
        raise PermissionError("owner download request required")
    principal = auth.principal(headers)
    permit = WorkspaceDownloadPermit.capture(
        organization_sharing_host(),
        principal.identity,
        query.get("session_id"),
        query.get("token"),
        token_validator=validate_file_download_token,
    )
    return principal, permit


async def owner_workspace_download(request, channel):
    from .container_file_http import _parse_single_byte_range
    from jiuwenswarm.gateway.routing.e2a_proxy import fetch_agent_unary

    try:
        if len(request.query_params.multi_items()) != len(request.query_params):
            raise PermissionError("duplicate download selector")
        agent_client, route_check = capture_local_agent_route(channel)
        principal, permit = capture_http_download(request.headers, request.query_params)
        if dict(permit._source.binding)["channel_id"] != channel.channel_id:
            raise PermissionError("original channel required")

        def check():
            route_check()
            permit.check()

        request.scope[GUARD_KEY] = check
        token = request.query_params["token"]
        total = permit.size
        mime = mimetypes.guess_type(permit.name)[0] or "application/octet-stream"
        range_header = request.headers.get("range", "")
        selected = (
            _parse_single_byte_range(range_header, total) if range_header else None
        )
        if range_header and selected is None:
            return Response(
                status_code=416,
                headers={
                    "Content-Range": f"bytes */{total}",
                    "Content-Length": "0",
                    "Accept-Ranges": "bytes",
                    "Cache-Control": "no-store",
                },
            )
        start, end = selected or (0, max(0, total - 1))
        length = end - start + 1 if total else 0
        inline = request.query_params.get("inline", "") in {"1", "true"} and mime in {
            "image/png",
            "image/jpeg",
            "image/gif",
            "image/webp",
            "audio/mpeg",
            "audio/wav",
            "video/mp4",
            "application/pdf",
        }
        disposition = "inline" if inline else "attachment"
        headers = {
            "Content-Length": str(length),
            "Cache-Control": "no-store",
            "Accept-Ranges": "bytes",
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": f"{disposition}; filename*=UTF-8''{quote(permit.name, safe='')}",
        }
        status = 206 if selected else 200
        if selected:
            headers["Content-Range"] = f"bytes {start}-{end}/{total}"

        async def chunk(offset, limit):
            check()
            params = {"token": token, "offset": offset, "limit": limit}

            def before_send(actual_client, wire):
                check()
                if (
                    actual_client is not agent_client
                    or wire.get("method")
                    != ReqMethod.FILE_DOWNLOAD_WORKSPACE_CHUNK.value
                    or wire.get("session_id") != permit.session_id
                    or wire.get("channel") != channel.channel_id
                    or wire.get("params") != params
                ):
                    raise PermissionError("original download request changed")

            with authenticated_scope(principal), workspace_send_scope(before_send):
                ok, result = await fetch_agent_unary(
                    agent_client=agent_client,
                    req_method=ReqMethod.FILE_DOWNLOAD_WORKSPACE_CHUNK,
                    params=params,
                    session_id=permit.session_id,
                    user_id=principal.identity().actor_id,
                    channel_id=channel.channel_id,
                    label="file.download_workspace_chunk",
                )
            check()
            if (
                not ok
                or type(result) is not dict
                or set(result) != {"data", "offset", "size", "name", "mime_type", "eof"}
            ):
                raise PermissionError("Workspace chunk rejected")
            data = base64.b64decode(result["data"], validate=True)
            expected = min(limit, total - offset)
            if (
                type(result["offset"]) is not int
                or result["offset"] != offset
                or type(result["size"]) is not int
                or result["size"] != total
                or result["name"] != permit.name
                or result["mime_type"] != mime
                or len(data) != expected
                or type(result["eof"]) is not bool
                or result["eof"] != (offset + len(data) == total)
            ):
                raise PermissionError("Workspace chunk changed")
            return data

        # Confirm the real AgentServer consumer before disclosing success headers,
        # even for HEAD/empty files. Buffered bytes still need final send checks.
        first = await chunk(
            start,
            1
            if request.method == "HEAD"
            else min(MAX_DOWNLOAD_CHUNK_BYTES, max(length, 1)),
        )
        if request.method == "HEAD" or total == 0:
            return Response(status_code=status, headers=headers, media_type=mime)

        async def stream():
            offset, data = start, first
            while offset <= end:
                check()
                yield data
                offset += len(data)
                if offset <= end:
                    data = await chunk(
                        offset, min(MAX_DOWNLOAD_CHUNK_BYTES, end - offset + 1)
                    )

        return StreamingResponse(
            stream(), status_code=status, headers=headers, media_type=mime
        )
    except Exception:
        request.scope.pop(GUARD_KEY, None)
        return JSONResponse(
            {"error": "Workspace download denied", "code": "FORBIDDEN"},
            status_code=403,
            headers={"Cache-Control": "no-store"},
        )
