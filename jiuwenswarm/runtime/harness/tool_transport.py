# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Session-owned authenticated loopback MCP transport for product tools."""

from __future__ import annotations

import asyncio
import hmac
import json
import secrets
import socket
from typing import Any
from uuid import uuid4

import mcp.types as types
import uvicorn
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings

from openjiuwen.harness_protocol import (
    McpServerConfig,
    McpTransport,
    ToolGateway,
    ToolInvocation,
    json_value_to_builtin,
)

PRODUCT_MCP_SERVER_NAME = "jiuwenswarm_product_tools"
PRODUCT_MCP_PATH = "/mcp"
_MAX_REQUEST_BODY_BYTES = 1024 * 1024
_START_TIMEOUT_S = 10.0
_STOP_TIMEOUT_S = 10.0


def _tool_text(content: Any) -> str:
    value = json_value_to_builtin(content)
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class ManagedProductToolTransport:
    """Expose exactly one bound ToolGateway over authenticated loopback HTTP."""

    def __init__(
        self,
        gateway: ToolGateway,
        *,
        host_session_id: str,
        server_name: str = PRODUCT_MCP_SERVER_NAME,
    ) -> None:
        if not host_session_id:
            raise ValueError("product MCP parent session is required")
        if not server_name:
            raise ValueError("product MCP server name is required")
        self._gateway = gateway
        self._host_session_id = host_session_id
        self._server_name = server_name
        self._token = secrets.token_urlsafe(32)
        self._port: int | None = None
        self._socket: socket.socket | None = None
        self._uvicorn: uvicorn.Server | None = None
        self._serve_task: asyncio.Task[None] | None = None
        self._lifecycle_lock = asyncio.Lock()

    @property
    def started(self) -> bool:
        task = self._serve_task
        return self._port is not None and task is not None and not task.done()

    @property
    def exit_confirmed(self) -> bool:
        """Return true only when the owned server task has actually exited."""

        task = self._serve_task
        return task is None or task.done()

    async def start(self) -> None:
        async with self._lifecycle_lock:
            if self.started:
                raise RuntimeError("product MCP transport is already started")
            if self._serve_task is not None:
                raise RuntimeError("product MCP transport cannot be restarted")
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("127.0.0.1", 0))
            listener.listen(128)
            listener.setblocking(False)
            self._socket = listener
            self._port = int(listener.getsockname()[1])
            try:
                app, manager = self._build_app(self._port)
                server = uvicorn.Server(
                    uvicorn.Config(
                        app,
                        host="127.0.0.1",
                        port=self._port,
                        log_level="warning",
                        access_log=False,
                        lifespan="off",
                    )
                )
                self._uvicorn = server
                task = asyncio.create_task(
                    self._serve(server, listener, manager),
                    name=f"product_mcp[{self._host_session_id}]",
                )
                self._serve_task = task
                async with asyncio.timeout(_START_TIMEOUT_S):
                    while not server.started:
                        if task.done():
                            await task
                            raise RuntimeError("product MCP transport exited before readiness")
                        await asyncio.sleep(0)
            except BaseException:
                await self._stop_locked()
                raise

    def server_config(self) -> McpServerConfig:
        if not self.started or self._port is None:
            raise RuntimeError("product MCP transport is not running")
        return McpServerConfig(
            name=self._server_name,
            transport=McpTransport.HTTP,
            url=f"http://127.0.0.1:{self._port}{PRODUCT_MCP_PATH}",
            headers={"Authorization": f"Bearer {self._token}"},
        )

    async def stop(self) -> None:
        async with self._lifecycle_lock:
            await self._stop_locked()

    async def _stop_locked(self) -> None:
        server = self._uvicorn
        task = self._serve_task
        listener = self._socket
        if server is not None:
            server.should_exit = True
        if task is not None:
            try:
                async with asyncio.timeout(_STOP_TIMEOUT_S):
                    await asyncio.shield(task)
            except TimeoutError:
                task.cancel()
                try:
                    async with asyncio.timeout(_STOP_TIMEOUT_S):
                        await asyncio.shield(task)
                except TimeoutError as exc:
                    if listener is not None:
                        listener.close()
                        self._socket = None
                    raise RuntimeError(
                        "product MCP transport exit could not be confirmed"
                    ) from exc
                except asyncio.CancelledError:
                    pass
            except asyncio.CancelledError:
                pass
            except Exception:
                # The owned server task has exited abnormally, but its exit is
                # still confirmed.  Startup/readiness already reports failures
                # that happen before the transport is published.
                pass
        if listener is not None:
            listener.close()
        self._uvicorn = None
        self._socket = None
        self._port = None

    @staticmethod
    async def _serve(
        server: uvicorn.Server,
        listener: socket.socket,
        manager: StreamableHTTPSessionManager,
    ) -> None:
        async with manager.run():
            await server.serve(sockets=[listener])

    def _build_app(self, port: int):
        mcp_server: Server = Server(self._server_name)

        @mcp_server.list_tools()
        async def list_tools() -> list[types.Tool]:
            definitions = await self._gateway.definitions()
            return [
                types.Tool(
                    name=item.name,
                    description=item.description,
                    inputSchema=json_value_to_builtin(item.input_schema),
                )
                for item in definitions
            ]

        @mcp_server.call_tool()
        async def call_tool(
            name: str,
            arguments: dict[str, Any],
        ) -> types.CallToolResult:
            result = await self._gateway.invoke(
                ToolInvocation(
                    call_id=f"mcp-{uuid4().hex}",
                    name=name,
                    arguments=arguments,
                )
            )
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=_tool_text(result.content))],
                isError=result.is_error,
            )

        manager = StreamableHTTPSessionManager(
            mcp_server,
            json_response=True,
            stateless=True,
            security_settings=TransportSecuritySettings(
                enable_dns_rebinding_protection=True,
                allowed_hosts=[f"127.0.0.1:{port}"],
                allowed_origins=[],
            ),
            max_request_body_size=_MAX_REQUEST_BODY_BYTES,
        )

        async def app(scope, receive, send) -> None:
            if scope["type"] != "http":
                return
            headers = {
                key.decode("latin-1").lower(): value.decode("latin-1")
                for key, value in scope.get("headers", ())
            }
            supplied = headers.get("authorization", "")
            if scope.get("path") != PRODUCT_MCP_PATH or not hmac.compare_digest(
                supplied,
                f"Bearer {self._token}",
            ):
                body = b'{"error":"unauthorized"}'
                await send(
                    {
                        "type": "http.response.start",
                        "status": 401,
                        "headers": [
                            (b"content-type", b"application/json"),
                            (b"content-length", str(len(body)).encode("ascii")),
                        ],
                    }
                )
                await send({"type": "http.response.body", "body": body})
                return
            await manager.handle_request(scope, receive, send)

        return app, manager


__all__ = [
    "ManagedProductToolTransport",
    "PRODUCT_MCP_PATH",
    "PRODUCT_MCP_SERVER_NAME",
]
