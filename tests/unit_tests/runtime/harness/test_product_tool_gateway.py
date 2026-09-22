# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""R1-03B1 product ToolGateway and managed MCP transport tests."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from openjiuwen.core.foundation.tool.schema import ToolOutput
from openjiuwen.harness.engine import HarnessEngine
from openjiuwen.harness_protocol import (
    AgentExecutionSpec,
    HarnessCapability,
    HarnessContext,
    HarnessState,
    HostCapability,
    McpServerConfig,
    McpTransport,
    ToolInvocation,
)

from jiuwenswarm.common.runtime_workspace import RuntimeWorkspacePaths
from jiuwenswarm.runtime.harness.binding_store import ExecutionBindingStore
from jiuwenswarm.runtime.harness.config_source import ExecutionConfigSource
from jiuwenswarm.runtime.harness.execution_session import ExecutionSession
from jiuwenswarm.runtime.harness.tool_gateway import (
    ProductToolGateway,
    ProductToolScope,
)
from jiuwenswarm.runtime.harness.tool_transport import (
    ManagedProductToolTransport,
    PRODUCT_MCP_SERVER_NAME,
)


class _Tool:
    def __init__(self, name: str = "echo", *, parallel_safe: bool = True) -> None:
        self.card = SimpleNamespace(
            name=name,
            description="Echo a value",
            input_params={
                "type": "object",
                "properties": {"value": {"type": "string"}},
                "required": ["value"],
                "additionalProperties": False,
            },
            parallel_safe=parallel_safe,
        )
        self.calls: list[tuple[dict, dict]] = []

    async def invoke(self, inputs, **kwargs):
        self.calls.append((dict(inputs), dict(kwargs)))
        return ToolOutput(success=True, data={"content": inputs["value"]})

    def render_for_llm(self, output) -> str:
        return str(output.data["content"])


def _scope(tmp_path: Path, *, session_id: str = "parent-1") -> ProductToolScope:
    return ProductToolScope(
        subject_id="alice",
        host_session_id=session_id,
        workspace=str(tmp_path.resolve()),
    )


@pytest.mark.asyncio
async def test_gateway_delegates_original_tool_under_bound_scope(tmp_path: Path) -> None:
    tool = _Tool()
    admitted = []

    async def admit(scope, invocation) -> bool:
        admitted.append((scope, invocation.name))
        return True

    session = object()
    gateway = ProductToolGateway(
        [tool],
        scope=_scope(tmp_path),
        invoke_kwargs={"session": session},
        admit=admit,
    )

    definitions = await gateway.definitions()
    result = await gateway.invoke(
        ToolInvocation("call-1", "echo", {"value": "hello"})
    )

    assert [item.name for item in definitions] == ["echo"]
    assert definitions[0].input_schema["required"] == ("value",)
    assert result.content == "hello"
    assert result.is_error is False
    assert tool.calls == [({"value": "hello"}, {"session": session})]
    assert admitted == [(gateway.scope, "echo")]


@pytest.mark.asyncio
async def test_gateway_rejects_unknown_or_unadmitted_tools(tmp_path: Path) -> None:
    tool = _Tool()
    gateway = ProductToolGateway(
        [tool],
        scope=_scope(tmp_path),
        admit=lambda _scope, _invocation: False,
    )

    denied = await gateway.invoke(ToolInvocation("call-1", "echo", {"value": "x"}))
    unknown = await gateway.invoke(ToolInvocation("call-2", "missing", {}))

    assert denied.is_error is True
    assert unknown.is_error is True
    assert tool.calls == []


def test_gateway_rejects_duplicate_catalog_and_relative_scope(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="absolute"):
        ProductToolScope("alice", "session", "relative")
    with pytest.raises(ValueError, match="at least one"):
        ProductToolGateway([], scope=_scope(tmp_path))
    with pytest.raises(ValueError, match="duplicate"):
        ProductToolGateway([_Tool(), _Tool()], scope=_scope(tmp_path))


@pytest.mark.asyncio
async def test_managed_transport_requires_token_and_delegates_mcp(
    tmp_path: Path,
) -> None:
    tool = _Tool()
    gateway = ProductToolGateway([tool], scope=_scope(tmp_path))
    transport = ManagedProductToolTransport(
        gateway,
        host_session_id=gateway.scope.host_session_id,
    )
    await transport.start()
    config = transport.server_config()
    try:
        assert config.name == PRODUCT_MCP_SERVER_NAME
        assert config.url is not None and config.url.startswith("http://127.0.0.1:")
        assert config.headers["Authorization"].startswith("Bearer ")

        async with httpx.AsyncClient() as unauthorized:
            response = await unauthorized.post(config.url, json={})
        assert response.status_code == 401

        async with httpx.AsyncClient(headers=dict(config.headers)) as client:
            async with streamable_http_client(
                config.url,
                http_client=client,
            ) as (read_stream, write_stream, _):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    listed = await session.list_tools()
                    called = await session.call_tool("echo", {"value": "through-mcp"})

        assert [item.name for item in listed.tools] == ["echo"]
        assert called.isError is False
        assert called.content[0].text == "through-mcp"
    finally:
        await transport.stop()
    assert transport.started is False


@pytest.mark.asyncio
async def test_managed_transport_closes_listener_when_startup_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = ManagedProductToolTransport(
        ProductToolGateway([_Tool()], scope=_scope(tmp_path)),
        host_session_id="parent-1",
    )

    def fail_before_server(_port: int):
        raise RuntimeError("MCP app construction failed")

    monkeypatch.setattr(transport, "_build_app", fail_before_server)
    with pytest.raises(RuntimeError, match="construction failed"):
        await transport.start()

    assert transport.started is False
    await transport.stop()


@pytest.mark.asyncio
async def test_unsafe_product_tool_calls_are_serialized(tmp_path: Path) -> None:
    active = 0
    peak = 0

    class UnsafeTool(_Tool):
        async def invoke(self, inputs, **kwargs):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0)
            active -= 1
            return await super().invoke(inputs, **kwargs)

    gateway = ProductToolGateway(
        [UnsafeTool(parallel_safe=False)],
        scope=_scope(tmp_path),
    )
    await asyncio.gather(
        gateway.invoke(ToolInvocation("one", "echo", {"value": "1"})),
        gateway.invoke(ToolInvocation("two", "echo", {"value": "2"})),
    )

    assert peak == 1


@pytest.mark.asyncio
async def test_execution_session_mounts_and_releases_bound_product_transport(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path.resolve()
    source = ExecutionConfigSource(
        explicit=AgentExecutionSpec("codex", "r1")
    )
    bindings = ExecutionBindingStore()
    bound = bindings.bind(
        source,
        subject_id="alice",
        host_session_id="parent-1",
        workspace=str(root),
    )
    paths = RuntimeWorkspacePaths(root, root, root, root)
    captured = SimpleNamespace(context=None, transport=None)

    class Cursor:
        def __init__(self) -> None:
            self.closed = asyncio.Event()

        def __aiter__(self):
            return self

        async def __anext__(self):
            await self.closed.wait()
            raise StopAsyncIteration

        async def aclose(self) -> None:
            self.closed.set()

    class Harness:
        card = SimpleNamespace(
            name="mcp-only",
            supports=lambda capability: capability is HarnessCapability.MCP_TOOLS,
        )
        state = HarnessState.TERMINATED
        provider_session_id = None

        def __init__(self) -> None:
            self.cursor = Cursor()

        async def start(self, context) -> None:
            captured.context = context
            self.state = HarnessState.IDLE

        async def stop(self) -> None:
            self.state = HarnessState.TERMINATED
            self.cursor.closed.set()

        def events(self):
            return self.cursor

    class Transport:
        def __init__(self, gateway, *, host_session_id) -> None:
            assert gateway.scope.host_session_id == host_session_id == "parent-1"
            self.started = False
            self.stopped = False
            captured.transport = self

        async def start(self) -> None:
            self.started = True

        def server_config(self) -> McpServerConfig:
            assert self.started
            return McpServerConfig(
                name=PRODUCT_MCP_SERVER_NAME,
                transport=McpTransport.HTTP,
                url="http://127.0.0.1:43111/mcp",
                headers={"Authorization": "Bearer " + "x" * 43},
            )

        async def stop(self) -> None:
            self.stopped = True
            self.started = False

    monkeypatch.setattr(
        "jiuwenswarm.runtime.harness.execution_session.ManagedProductToolTransport",
        Transport,
    )
    gateway = ProductToolGateway([_Tool()], scope=_scope(tmp_path))
    session = ExecutionSession(
        HarnessEngine(bound.binding, Harness()),
        paths,
        tool_gateway=gateway,
    )
    context = HarnessContext(
        agent_name="external",
        agent_id="external-1",
        host_session_id="parent-1",
        system_prompt="",
        cwd=str(root),
    )

    await session.start(context)

    assert captured.context.mcp_servers[0].name == PRODUCT_MCP_SERVER_NAME
    assert HostCapability.MCP_SERVERS in captured.context.host_capabilities
    assert captured.context.tools is None
    await session.stop()
    assert captured.transport.stopped is True


@pytest.mark.asyncio
async def test_execution_session_rejects_gateway_from_another_parent(
    tmp_path: Path,
) -> None:
    root = tmp_path.resolve()
    source = ExecutionConfigSource(
        explicit=AgentExecutionSpec("codex", "r1")
    )
    bindings = ExecutionBindingStore()
    bound = bindings.bind(
        source,
        subject_id="alice",
        host_session_id="parent-1",
        workspace=str(root),
    )
    paths = RuntimeWorkspacePaths(root, root, root, root)
    gateway = ProductToolGateway(
        [_Tool()],
        scope=_scope(tmp_path, session_id="other-parent"),
    )
    harness = SimpleNamespace(
        card=SimpleNamespace(
            name="mcp-only",
            supports=lambda capability: capability is HarnessCapability.MCP_TOOLS,
        ),
        state=HarnessState.TERMINATED,
        provider_session_id=None,
    )
    session = ExecutionSession(
        HarnessEngine(bound.binding, harness),
        paths,
        tool_gateway=gateway,
    )
    context = HarnessContext(
        agent_name="external",
        agent_id="external-1",
        host_session_id="parent-1",
        system_prompt="",
        cwd=str(root),
    )

    with pytest.raises(ValueError, match="scope does not match"):
        await session.start(context)


@pytest.mark.asyncio
async def test_execution_session_releases_product_transport_when_provider_start_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path.resolve()
    bound = ExecutionBindingStore().bind(
        ExecutionConfigSource(explicit=AgentExecutionSpec("codex", "r1")),
        subject_id="alice",
        host_session_id="parent-1",
        workspace=str(root),
    )
    captured = SimpleNamespace(transport=None)

    class Harness:
        card = SimpleNamespace(
            name="mcp-only",
            supports=lambda capability: capability is HarnessCapability.MCP_TOOLS,
        )
        state = HarnessState.TERMINATED
        provider_session_id = None

        async def start(self, _context) -> None:
            raise RuntimeError("provider failed after MCP readiness")

        async def stop(self) -> None:
            self.state = HarnessState.TERMINATED

    class Transport:
        def __init__(self, _gateway, *, host_session_id) -> None:
            assert host_session_id == "parent-1"
            self.stopped = False
            captured.transport = self

        async def start(self) -> None:
            return None

        def server_config(self) -> McpServerConfig:
            return McpServerConfig(
                name=PRODUCT_MCP_SERVER_NAME,
                transport=McpTransport.HTTP,
                url="http://127.0.0.1:43111/mcp",
                headers={"Authorization": "Bearer " + "x" * 43},
            )

        async def stop(self) -> None:
            self.stopped = True

    monkeypatch.setattr(
        "jiuwenswarm.runtime.harness.execution_session.ManagedProductToolTransport",
        Transport,
    )
    session = ExecutionSession(
        HarnessEngine(bound.binding, Harness()),
        RuntimeWorkspacePaths(root, root, root, root),
        tool_gateway=ProductToolGateway([_Tool()], scope=_scope(tmp_path)),
    )

    with pytest.raises(RuntimeError, match="provider failed"):
        await session.start(
            HarnessContext(
                agent_name="external",
                agent_id="external-1",
                host_session_id="parent-1",
                system_prompt="",
                cwd=str(root),
            )
        )

    assert captured.transport.stopped is True
