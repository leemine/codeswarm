# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from openjiuwen.harness_protocol import RuntimeSurface

from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponse
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.runtime.harness.ui_capability_manifest import (
    compile_native_ui_capability_manifest,
)
from jiuwenswarm.server import agent_ws_server as agent_ws_server_module
from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer


def _request(session_id: str | None) -> AgentRequest:
    return AgentRequest(
        request_id="req-surface-capabilities",
        channel_id="web",
        session_id=session_id,
        req_method=ReqMethod.SURFACE_CAPABILITIES_GET,
        params={"session_id": session_id} if session_id else {},
        agent_ref={"mode": "single", "id": "agent"},
    )


async def _call(request: AgentRequest, manager=None) -> AgentResponse:
    server = AgentWebSocketServer.__new__(AgentWebSocketServer)
    server._agent_manager = manager or SimpleNamespace()
    captured: dict[str, AgentResponse] = {}

    def _encode(response, response_id=None):  # noqa: ARG001
        captured["response"] = response
        return {"encoded": True}

    send_lock = AsyncMock()
    send_lock.__aenter__ = AsyncMock(return_value=None)
    send_lock.__aexit__ = AsyncMock(return_value=None)
    with (
        patch(
            "jiuwenswarm.server.agent_ws_server.encode_agent_response_for_wire",
            side_effect=_encode,
        ),
        patch(
            "jiuwenswarm.server.agent_ws_server.send_wire_payload",
            new=AsyncMock(),
        ),
    ):
        await server._handle_surface_capabilities_get(object(), request, send_lock)
    return captured["response"]


@pytest.mark.asyncio
async def test_native_code_manifest_uses_persisted_surface():
    with patch.object(
        agent_ws_server_module,
        "get_session_metadata",
        return_value={"mode": "agent.code.normal", "work_mode": "code"},
    ):
        response = await _call(_request("native-code"))

    assert response.ok is True
    assert response.agent_ref == {"mode": "single", "id": "agent"}
    manifest = response.payload["surface_capabilities"]
    assert manifest["provider_id"] == "native"
    assert manifest["surface"] == "code"
    assert response.payload["surface_capabilities_fingerprint"]


@pytest.mark.asyncio
async def test_external_manifest_comes_from_existing_adapter():
    manifest = replace(
        compile_native_ui_capability_manifest(RuntimeSurface.WORK),
        provider_id="codex",
    )
    agent = SimpleNamespace(_adapter=SimpleNamespace(ui_capability_manifest=manifest))
    with (
        patch.object(
            agent_ws_server_module,
            "get_session_metadata",
            return_value={
                "mode": "agent.work.normal",
                "work_mode": "work",
                "execution_profile_id": "codex-work",
            },
        ),
        patch("jiuwenswarm.runtime.request.prepare_chat_turn", new=AsyncMock(
            side_effect=AssertionError("read must not construct"))) as prepare,
    ):
        response = await _call(_request("external-work"), SimpleNamespace(
            get_agent_for_session_nowait=lambda channel, sid: agent))

    assert response.ok is True
    assert response.payload["surface_capabilities"] == manifest.record()
    prepare.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_and_unbound_external_team_do_not_construct_runtime():
    missing = await _call(_request(None))
    assert missing.ok is False
    assert missing.payload["code"] == "BAD_REQUEST"

    with patch.object(
        agent_ws_server_module,
        "get_session_metadata",
        return_value={"mode": "team.work.normal", "work_mode": "work", "execution_profile_id": "codex"},
    ):
        team = await _call(_request("team"))
    assert team.ok is False
    assert team.payload["code"] == "NOT_READY"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["team", "team.code.normal"])
async def test_bound_external_team_manifest_comes_from_existing_adapter(mode):
    manifest = replace(compile_native_ui_capability_manifest(RuntimeSurface.CODE), provider_id="codex")
    agent = SimpleNamespace(_adapter=SimpleNamespace(ui_capability_manifest=manifest))
    with (
        patch.object(agent_ws_server_module, "get_session_metadata", return_value={
            "mode": mode, "work_mode": "code",
            "team_name": "team", "execution_profile_id": "codex",
        }),
        patch("jiuwenswarm.runtime.request.prepare_chat_turn", new=AsyncMock(
            side_effect=AssertionError("read must not construct"))) as prepare,
    ):
        response = await _call(_request("external-team"), SimpleNamespace(
            get_agent_for_session_nowait=lambda channel, sid: agent))
    assert response.ok and response.payload["surface_capabilities"] == manifest.record()
    prepare.assert_not_awaited()


@pytest.mark.asyncio
async def test_native_team_manifest_does_not_require_external_binding():
    with patch.object(agent_ws_server_module, "get_session_metadata", return_value={
        "mode": "team.work.normal", "work_mode": "work",
    }):
        response = await _call(_request("native-team"))
    assert response.ok and response.payload["surface_capabilities"]["provider_id"] == "native"


def test_cold_catalog_projection_has_no_runtime_or_workspace_side_effects(tmp_path):
    from openjiuwen.harness.engine.config import config_fingerprint
    from jiuwenswarm.runtime.harness.cold_surface_manifest import cold_surface_manifest
    from jiuwenswarm.runtime.harness.config_source import load_execution_catalog
    from jiuwenswarm.runtime.harness.surface import creation_surface

    config = {'permissions': {'enabled': True}, 'execution': {
        'default_profile_id': 'oc', 'profiles': {'oc': {
            'provider_id': 'opencode', 'config_revision': 'frozen',
            'provider_config': {'model': {'model': 'synthetic', 'api_base': 'https://model.invalid/v1'}},
        }}}}
    spec = load_execution_catalog(config).source().resolve()
    metadata = {'session_id': 'cold', 'channel_id': 'web', 'user_id': 'bob',
        'project_dir': str(tmp_path / 'not-allocated'), 'mode': 'agent.work.normal',
        'work_mode': 'work', 'execution_profile_id': 'oc',
        'execution_config_revision': spec.config_revision,
        'execution_config_fingerprint': config_fingerprint(spec)}
    metadata['surface_creation'] = creation_surface(metadata)
    manifest = cold_surface_manifest(metadata, config=config, channel_id='web',
        session_id='cold', browser_available=False)
    assert manifest.provider_id == 'opencode'
    assert not (tmp_path / 'not-allocated').exists()
    for key in ('execution_config_revision', 'execution_config_fingerprint'):
        with pytest.raises(ValueError, match='execution configuration changed'):
            cold_surface_manifest({**metadata, key: 'drift'}, config=config,
                channel_id='web', session_id='cold', browser_available=False)
    with pytest.raises(ValueError, match='Surface'):
        cold_surface_manifest({**metadata, 'work_mode': 'code'}, config=config,
            channel_id='web', session_id='cold', browser_available=False)
