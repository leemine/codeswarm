# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

from types import MethodType, SimpleNamespace

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponseChunk
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.runtime.agent_adapter.interface import JiuWenSwarm


@pytest.mark.asyncio
async def test_native_runtime_acceptance_carries_code_surface_manifest():
    swarm = JiuWenSwarm.__new__(JiuWenSwarm)
    swarm._adapter = SimpleNamespace()

    async def _stream(_self, request):
        yield AgentResponseChunk(
            request_id=request.request_id,
            channel_id=request.channel_id,
            payload={"event_type": "runtime.accepted", "accepted": True},
        )

    swarm._process_message_stream = MethodType(_stream, swarm)
    request = AgentRequest(
        request_id="native-code",
        channel_id="web",
        session_id="native-code-session",
        req_method=ReqMethod.CHAT_SEND,
        params={"mode": "agent.code.normal", "query": "hello"},
    )

    chunks = [chunk async for chunk in swarm.process_message_stream(request)]

    assert len(chunks) == 1
    payload = chunks[0].payload
    assert payload["session_id"] == "native-code-session"
    assert payload["surface_capabilities"]["provider_id"] == "native"
    assert payload["surface_capabilities"]["surface"] == "code"
    assert payload["surface_capabilities_fingerprint"]
