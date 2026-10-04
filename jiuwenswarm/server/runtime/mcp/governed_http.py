"""Private stateless MCP consumer; not registration or organization enablement.

One logical operation owns one SDK session and one HTTP pool. All SDK child
requests retain that operation's explicit authority. No ambient credential,
legacy client registry, reconnect decorator, or latest-Turn selector is used.
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import anyio
import httpx
from mcp import ClientSession, types
from mcp.client.streamable_http import streamable_http_client

from jiuwenswarm.governance.credential_resources import BoundCredentialAuthority, CredentialUse
from jiuwenswarm.governance.resources import ResourceAccessDenied


class McpConsumptionDenied(ResourceAccessDenied):
    """Safe public failure, including unsupported protected connection shapes."""


@dataclass(frozen=True, slots=True)
class McpHttpBinding:
    connection_id: str
    endpoint: str
    catalog_revision: str
    credential_use: CredentialUse = field(repr=False)

    def __post_init__(self):
        if (type(self.connection_id) is not str or not self.connection_id
                or type(self.catalog_revision) is not str or not self.catalog_revision
                or type(self.credential_use) is not CredentialUse
                or self.credential_use.purpose != 'mcp'
                or self.credential_use.destination != self.endpoint
                or not self.endpoint.startswith(('http://', 'https://'))
                or str(httpx.URL(self.endpoint)) != self.endpoint):
            raise ValueError('explicit canonical MCP HTTP binding required')


@dataclass(frozen=True, slots=True)
class McpRequestTarget:
    method: str
    url: str
    rpc_method: str
    tool_name: str
    arguments_json: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class McpRequestReceipt:
    """Local observations only; never an authorization token or remote receipt."""
    request_count: int
    closed: bool


@dataclass(frozen=True, slots=True)
class McpOperationResult:
    result: types.CallToolResult = field(repr=False)
    receipt: McpRequestReceipt


def _json(value: Any) -> str:
    # Reject Python coercions (tuple, non-string dict keys, NaN) before encoding.
    if value is not None and type(value) not in (dict, list, str, int, float, bool):
        raise McpConsumptionDenied('MCP input is unavailable')
    if type(value) is dict:
        for key, item in value.items():
            if type(key) is not str:
                raise McpConsumptionDenied('MCP input is unavailable')
            _json(item)
    elif type(value) is list:
        for item in value:
            _json(item)
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


class _OperationTransport(httpx.AsyncBaseTransport):
    def __init__(self, binding, authority, tool_name, arguments_json, admit, current,
                 *, max_response_bytes):
        self.binding = binding
        self.authority = authority
        self.tool_name = tool_name
        self.arguments_json = arguments_json
        self.admit = admit
        self.current = current
        self.max_response_bytes = max_response_bytes
        self.active = True
        self.closed = False
        self.count = 0
        self.cancelled = False
        self.initial_decision = self.check()
        self.inner = httpx.AsyncHTTPTransport(retries=0, trust_env=False)

    def check(self):
        if not self.active or self.current() is not True:
            raise McpConsumptionDenied('MCP execution is unavailable')
        decision = self.authority.check_for_request(
            self.binding.credential_use, destination=self.binding.endpoint)
        if self.current() is not True:
            raise McpConsumptionDenied('MCP execution is unavailable')
        return decision

    def target(self, request):
        if (request.method != 'POST' or str(request.url) != self.binding.endpoint
                or 'authorization' in request.headers or 'cookie' in request.headers):
            raise McpConsumptionDenied('MCP request is unavailable')
        message = json.loads(request.content)
        if type(message) is not dict or message.get('jsonrpc') != '2.0':
            raise McpConsumptionDenied('MCP request is unavailable')
        method = message.get('method')
        if method == 'notifications/initialized':
            valid = set(message) == {'jsonrpc', 'method'}
        else:
            valid = (set(message) <= {'jsonrpc', 'id', 'method', 'params'}
                     and type(message.get('id')) is int)
            params = message.get('params', {})
            if method == 'initialize':
                valid = valid and params == {
                    'protocolVersion': types.LATEST_PROTOCOL_VERSION,
                    'capabilities': {}, 'clientInfo': {'name': 'governed-mcp', 'version': '1'},
                }
            elif method == 'tools/call':
                valid = (valid and type(params) is dict and set(params) == {'name', 'arguments'}
                         and params['name'] == self.tool_name
                         and _json(params['arguments']) == self.arguments_json)
            elif method == 'tools/list':
                valid = valid and params == {}
            else:
                valid = False
        if not valid:
            raise McpConsumptionDenied('MCP request is unavailable')
        target = McpRequestTarget('POST', str(request.url), method,
                                  self.tool_name, self.arguments_json)
        if self.admit(target) is not True:
            raise McpConsumptionDenied('MCP operation is unavailable')
        return target

    async def handle_async_request(self, request):
        cancelled = False
        try:
            return await self._send(request)
        except asyncio.CancelledError:
            cancelled = True
            self.cancelled = True
        except Exception:
            pass
        # Do not let SDK logging/reconnect see resolver/transport exceptions.
        if cancelled:
            raise asyncio.CancelledError()
        raise McpConsumptionDenied('MCP consumption denied')

    async def _send(self, request):
        target = self.target(request)
        snapshot = (request.method, str(request.url), request.content, tuple(request.headers.raw))
        before = self.check()
        if before != self.initial_decision:
            raise McpConsumptionDenied('MCP authorization changed')
        secret = await self.authority.resolve_for_request(
            self.binding.credential_use, destination=str(request.url))
        if (self.target(request) != target or self.check() != before
                or snapshot != (request.method, str(request.url), request.content, tuple(request.headers.raw))):
            raise McpConsumptionDenied('MCP authorization changed')
        response = None
        try:
            request.headers['Authorization'] = 'Bearer ' + secret
            secret = None
            self.count += 1
            response = await self.inner.handle_async_request(request)
            if ('mcp-session-id' in response.headers
                    or (target.rpc_method == 'notifications/initialized' and response.status_code != 202)
                    or (target.rpc_method != 'notifications/initialized'
                        and (response.status_code != 200
                             or response.headers.get('content-type', '').split(';')[0].strip().lower()
                             != 'application/json'))):
                raise McpConsumptionDenied('MCP response shape is unsupported')
            parts = []
            length = 0
            async for chunk in response.aiter_bytes():
                length += len(chunk)
                if length > self.max_response_bytes:
                    raise McpConsumptionDenied('MCP response exceeds limit')
                parts.append(chunk)
            body = b''.join(parts)
            if target.rpc_method == 'notifications/initialized' and body:
                raise McpConsumptionDenied('MCP notification response is unsupported')
            if self.admit(target) is not True or self.check() != before:
                raise McpConsumptionDenied('MCP authorization changed')
            # No response cookies/headers can affect the private client's next send.
            return httpx.Response(response.status_code, content=body,
                                  headers={'content-type': 'application/json'})
        finally:
            request.headers.pop('Authorization', None)
            secret = None
            if response is not None:
                await response.aclose()

    async def aclose(self):
        self.active = False
        with anyio.move_on_after(5, shield=True) as scope:
            await self.inner.aclose()
        if scope.cancel_called:
            raise McpConsumptionDenied('MCP cleanup incomplete')
        self.closed = True


async def invoke_mcp_tool(
    binding: McpHttpBinding, *, tool_name: str, arguments: dict,
    credential_authority: BoundCredentialAuthority,
    admit_actual_request: Callable[[McpRequestTarget], bool],
    is_current: Callable[[], bool], timeout: float = 20,
    max_response_bytes: int = 1024 * 1024,
) -> McpOperationResult:
    """Execute one explicitly bound stateless MCP tool operation.

    The host must pass the original execution's fixed authority and exact tool
    proof. This does not select a catalog entry, register a tool, grant access,
    or make unknown MCP executors supported by the Native resource mapper.
    """
    transport = None
    result = None
    cancelled = False
    failed = False
    try:
        if (type(binding) is not McpHttpBinding
                or type(credential_authority) is not BoundCredentialAuthority
                or type(tool_name) is not str or not tool_name or type(arguments) is not dict
                or not callable(admit_actual_request) or not callable(is_current)
                or type(timeout) not in (int, float) or not 0 < timeout <= 120
                or type(max_response_bytes) is not int or not 0 < max_response_bytes <= 16 * 1024 * 1024):
            raise McpConsumptionDenied('MCP operation binding is unavailable')
        # Snapshot before the first await; SDK sessions/child tasks are operation-private.
        arguments_json = _json(arguments)
        transport = _OperationTransport(
            binding, credential_authority, tool_name, arguments_json,
            admit_actual_request, is_current, max_response_bytes=max_response_bytes)
        async with httpx.AsyncClient(transport=transport, trust_env=False,
                                    follow_redirects=False, timeout=timeout) as client:
            with anyio.fail_after(timeout):
                async with streamable_http_client(binding.endpoint, http_client=client,
                                                  terminate_on_close=False) as streams:
                    async with ClientSession(
                        streams[0], streams[1], read_timeout_seconds=timedelta(seconds=timeout),
                        client_info=types.Implementation(name='governed-mcp', version='1'),
                    ) as session:
                        await session.initialize()
                        result = await session.call_tool(tool_name, json.loads(arguments_json))
                        if transport.check() != transport.initial_decision:
                            raise McpConsumptionDenied('MCP authorization changed')
        # Recheck after SDK/client cleanup, without requiring the now-closed lease.
        if (admit_actual_request(McpRequestTarget(
                'POST', binding.endpoint, 'tools/call', tool_name, arguments_json)) is not True
                or is_current() is not True or credential_authority.check_for_request(
                binding.credential_use, destination=binding.endpoint) != transport.initial_decision):
            raise McpConsumptionDenied('MCP authorization changed')
    except asyncio.CancelledError:
        cancelled = True
    except Exception:
        failed = True
    finally:
        if transport is not None:
            transport.active = False
            cancelled = cancelled or transport.cancelled
            if not transport.closed:
                try:
                    await transport.aclose()
                except asyncio.CancelledError:
                    cancelled = True
                except Exception:
                    failed = True
    if cancelled:
        raise asyncio.CancelledError()
    if failed or result is None or transport is None or not transport.closed:
        raise McpConsumptionDenied('MCP consumption denied')
    return McpOperationResult(result, McpRequestReceipt(transport.count, True))
