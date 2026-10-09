# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Optional organization credential login on the existing Web HTTP surface."""

from __future__ import annotations

from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.datastructures import Headers

from jiuwenswarm.governance.organization_auth import (
    COOKIE,
    authenticated_scope,
    configured_authenticator,
)

PREFIX = "/api/v1/auth/organization"


class OrganizationAuthenticationMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        auth = configured_authenticator()
        if auth is None or scope["type"] not in {"http", "websocket"}:
            return await self.app(scope, receive, send)
        if scope["type"] == "http" and scope.get("path") in {
            PREFIX + "/status",
            PREFIX + "/login",
        }:
            return await self.app(scope, receive, send)
        try:
            principal = auth.principal(Headers(scope=scope))
        except Exception:
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            else:
                await JSONResponse(
                    {"error": "organization authentication required"}, status_code=401
                )(scope, receive, send)
            return
        ended = False
        started = False

        async def checked_send(message):
            nonlocal ended, started
            if ended:
                return
            denial_status = 401
            denial_message = "organization credential expired or revoked"
            try:
                principal.identity()
                denial_status = 403
                denial_message = "Workspace download authorization changed"
                from .workspace_download_http import GUARD_KEY
                guard = scope.get(GUARD_KEY)
                if guard is not None:
                    guard()
            except Exception:
                ended = True
                if scope["type"] == "websocket":
                    await send({"type": "websocket.close", "code": 1008})
                elif started:
                    await send(
                        {"type": "http.response.body", "body": b"", "more_body": False}
                    )
                else:
                    await JSONResponse(
                        {"error": denial_message},
                        status_code=denial_status,
                        headers={"Cache-Control": "no-store"},
                    )(scope, receive, send)
                return
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        with authenticated_scope(principal):
            await self.app(
                scope,
                receive,
                send if scope.get("path") == PREFIX + "/logout" else checked_send,
            )


def _allowed_login_request(request: Request) -> bool:
    if request.headers.get("x-jiuwen-auth") != "1":
        return False
    origin = request.headers.get("origin")
    if origin is None:
        return True  # Non-browser clients still need the secret credential.
    from jiuwenswarm.common.security.ws_origin import get_allowed_origin_hosts

    try:
        parsed = urlsplit(origin)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return False
        local = {"localhost", "127.0.0.1", "::1"}
        return (
            parsed.hostname == request.url.hostname
            or parsed.hostname in get_allowed_origin_hosts()
            or (parsed.hostname in local and request.url.hostname in local)
        )
    except ValueError:
        return False


class LoginBody(BaseModel):
    token: str = Field(min_length=32, max_length=4096)


def register_organization_auth(app: FastAPI) -> None:
    app.add_middleware(OrganizationAuthenticationMiddleware)

    @app.get(PREFIX + "/status")
    async def status(request: Request):
        auth = configured_authenticator()
        if auth is None:
            return {"enabled": False, "authenticated": False}
        try:
            identity = auth.principal(request.headers).identity()
            return {
                "enabled": True,
                "authenticated": True,
                "actor_id": identity.actor_id,
                "sharing_enabled": auth._config().get("sharing_enabled", True) is True,
            }
        except Exception:
            return {"enabled": True, "authenticated": False}

    @app.post(PREFIX + "/login")
    async def login(request: Request, body: LoginBody):
        auth = configured_authenticator()
        if auth is None:
            return JSONResponse(
                {"error": "organization authentication disabled"}, status_code=404
            )
        if not _allowed_login_request(request):
            return JSONResponse({"error": "auth header required"}, status_code=403)
        try:
            principal = auth.principal({"Authorization": "Bearer " + body.token})
            identity = principal.identity()
        except Exception:
            return JSONResponse({"error": "invalid credential"}, status_code=401)
        response = JSONResponse({"authenticated": True, "actor_id": identity.actor_id})
        response.set_cookie(
            COOKIE,
            body.token,
            httponly=True,
            secure=request.url.scheme == "https",
            samesite="strict",
            path="/",
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.post(PREFIX + "/logout")
    async def logout(request: Request):
        auth = configured_authenticator()
        if auth is None:
            return JSONResponse(
                {"error": "organization authentication disabled"}, status_code=404
            )
        if not _allowed_login_request(request):
            return JSONResponse({"error": "auth header required"}, status_code=403)
        principal = auth.principal(request.headers)
        try:
            auth.revoke(principal)
        except Exception:
            return JSONResponse(
                {"error": "credential revocation failed; still signed in"},
                status_code=503,
            )
        response = JSONResponse({"authenticated": False})
        response.delete_cookie(COOKIE, path="/")
        response.headers["Cache-Control"] = "no-store"
        return response
