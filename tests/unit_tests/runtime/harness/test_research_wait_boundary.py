# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""External research wait snapshots retain the existing subagent lifecycle."""

import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from jsonschema import validate

from openjiuwen.harness_protocol import json_value_to_builtin
from openjiuwen.harness.subagent_runtime.control import SubagentControl
from openjiuwen.harness.tools.subagent import build_subagent_tools
from jiuwenswarm.runtime.harness.external_subagents import (
    ExternalSubagentRuntime,
    _ResearchWaitTool,
)
from tests.unit_tests.runtime.harness.test_external_subagent_execution import _surface_route
from tests.unit_tests.runtime.harness.test_external_subagent_runtime import (
    _Factory,
    _install_factory,
    _invoke,
    _route,
)


async def _write_output(_chunk):
    pass


@pytest.mark.asyncio
@pytest.mark.parametrize("arguments,expected", [
    ({}, 45000), ({"timeout_ms": 300000}, 45000), ({"timeout_ms": 45000}, 45000),
    ({"timeout_ms": 12000}, 12000), ({"timeout_ms": 0}, 0), ({"timeout_ms": -1}, -1),
    ({"timeout_ms": True}, True), ({"timeout_ms": False}, False),
])
async def test_gateway_caps_wait_without_mutating_arguments_or_replacing_control(
    tmp_path, monkeypatch, arguments, expected,
):
    _install_factory(monkeypatch, _Factory())
    runtime = ExternalSubagentRuntime(
        _route(tmp_path), write_output=_write_output, work_research_enabled=True,
    )
    original_wait = SubagentControl.wait
    calls = []

    async def record_wait(control, subagent_ids, *, timeout_ms):
        calls.append((subagent_ids, timeout_ms))
        return await original_wait(control, subagent_ids, timeout_ms=timeout_ms)

    monkeypatch.setattr(SubagentControl, "wait", record_wait)
    payload = {"subagent_ids": ["missing-child"], **arguments}
    before = deepcopy(payload)
    try:
        if type(expected) is int:
            definition = next(item for item in await runtime.gateway.definitions() if item.name == "subagent_wait")
            # The real MCP server performs this validation before gateway.invoke.
            validate(payload, json_value_to_builtin(definition.input_schema))
        result = await _invoke(runtime, "subagent_wait", payload)
        assert not result.is_error
        assert "not_found" in result.content
        assert calls == [(["missing-child"], expected)]
        assert type(calls[0][1]) is type(expected)  # bool is delegated, never normalized as int.
        assert payload == before
    finally:
        await runtime.close("test_done")


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [None, "300000", 300000.0, [], {}])
async def test_invalid_timeout_is_still_rejected_by_core(tmp_path, monkeypatch, bad):
    _install_factory(monkeypatch, _Factory())
    runtime = ExternalSubagentRuntime(
        _route(tmp_path), write_output=_write_output, work_research_enabled=True,
    )
    wait = AsyncMock(side_effect=AssertionError("Invalid timeout must not reach control.wait"))
    monkeypatch.setattr(SubagentControl, "wait", wait)
    try:
        result = await _invoke(runtime, "subagent_wait", {"subagent_ids": ["child"], "timeout_ms": bad})
        assert result.is_error
        wait.assert_not_awaited()
    finally:
        await runtime.close("test_done")


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,enabled,capped", [
    ("work", False, True), ("work", True, True),
    ("code", False, False), ("code", True, False),
    (None, True, True), (None, False, False),
])
async def test_frozen_surface_controls_wait_boundary(tmp_path, monkeypatch, mode, enabled, capped):
    _install_factory(monkeypatch, _Factory())
    route = _surface_route(tmp_path, work_mode=mode) if mode else _route(tmp_path)
    runtime = ExternalSubagentRuntime(route, write_output=_write_output, work_research_enabled=enabled)
    try:
        definitions = await runtime.gateway.definitions()
        wait = next(item for item in definitions if item.name == "subagent_wait")
        timeout = wait.input_schema["properties"]["timeout_ms"]
        assert (timeout.get("default") == 45000) is capped
        if capped:
            assert "maximum" not in timeout
            assert "earlier snapshot" in timeout["description"]
            assert "1800000" not in wait.description
            assert "3600000" not in wait.description
        assert len(definitions) == 6
    finally:
        await runtime.close("test_done")


def test_wrapper_clones_card_and_reuses_original_renderer():
    original = next(tool for tool in build_subagent_tools(SimpleNamespace()) if tool.card.name == "subagent_wait")
    before = original.card.model_dump()
    wrapper = _ResearchWaitTool(original)
    assert original.card.model_dump() == before
    assert wrapper.card is not original.card
    output = SimpleNamespace(data={"statuses": {"child": "running"}, "results": {}, "output_files": {}, "timed_out": True})
    assert wrapper.render_for_llm(output) == original.render_for_llm(output)


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_id", ["codex", "opencode"])
async def test_running_snapshot_then_final_wait_keeps_same_child_and_turn(
    tmp_path, monkeypatch, provider_id,
):
    from openjiuwen.harness.subagent_runtime import control as control_module

    gate = asyncio.Event()
    factory = _Factory(gate=gate)
    _install_factory(monkeypatch, factory)
    runtime = ExternalSubagentRuntime(
        _route(tmp_path, provider_id=provider_id),
        write_output=_write_output, work_research_enabled=True,
    )
    durations = []
    original_wait = asyncio.wait

    async def fast_deadline(tasks, *, timeout, return_when):
        durations.append(timeout)
        # Only the control's timer is accelerated; real status subscriptions,
        # running child, timeout snapshot, and subsequent completion are intact.
        return await original_wait(tasks, timeout=min(timeout, 0.01), return_when=return_when)

    monkeypatch.setattr(control_module, "asyncio", SimpleNamespace(
        **{name: getattr(asyncio, name) for name in dir(asyncio) if not name.startswith("__")},
    ))
    monkeypatch.setattr(control_module.asyncio, "wait", fast_deadline)
    try:
        spawned = await _invoke(runtime, "subagent_spawn", {
            "subagent_type": "research_agent", "task_description": "inspect sources",
            "display_name": "Researcher", "role": "Research",
        })
        assert not spawned.is_error
        control = runtime._parent_host._subagent_controls["parent-a"]
        child_id = control.list_live()[0].subagent_id
        first = await _invoke(runtime, "subagent_wait", {"subagent_ids": [child_id], "timeout_ms": 300000})
        assert not first.is_error and "status: running" in first.content
        assert "Timed out" in first.content and factory.active == 1
        assert 44 < durations[0] <= 45
        gate.set()
        second = await _invoke(runtime, "subagent_wait", {"subagent_ids": [child_id]})
        assert not second.is_error and "result:inspect sources" in second.content
        assert "Timed out" not in second.content
        assert len(factory.created) == 1 and factory.created[0][0].subagent_id == child_id
        assert runtime.has_control()
    finally:
        gate.set()
        await runtime.close("test_done")


@pytest.mark.asyncio
async def test_real_loopback_mcp_accepts_large_wait_and_delegates_capped_snapshot(tmp_path, monkeypatch):
    import httpx
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    from jiuwenswarm.runtime.harness.tool_transport import ManagedProductToolTransport

    _install_factory(monkeypatch, _Factory())
    runtime = ExternalSubagentRuntime(
        _surface_route(tmp_path, work_mode="work"), write_output=_write_output,
    )
    transport = ManagedProductToolTransport(
        runtime.gateway, host_session_id=runtime.gateway.scope.host_session_id,
    )
    original_wait = SubagentControl.wait
    received = []

    async def record_wait(control, subagent_ids, *, timeout_ms):
        received.append(timeout_ms)
        return await original_wait(control, subagent_ids, timeout_ms=timeout_ms)

    monkeypatch.setattr(SubagentControl, "wait", record_wait)
    try:
        await transport.start()
        config = transport.server_config()
        async with httpx.AsyncClient(headers=dict(config.headers)) as client:
            async with streamable_http_client(config.url, http_client=client) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    await session.list_tools()
                    result = await session.call_tool(
                        "subagent_wait", {"subagent_ids": ["missing-child"], "timeout_ms": 300000},
                    )
        assert not result.isError
        assert "not_found" in result.content[0].text
        assert received == [45000]
    finally:
        await transport.stop()
        await runtime.close("test_done")
    assert not transport.started
