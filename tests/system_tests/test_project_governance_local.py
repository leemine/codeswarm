"""Real loopback WS / disk canary; host identity is injected, not remote auth.

Uses the production AgentServer dispatcher and ProjectAdapter without starting
unrelated model/prewarm services. This is not a browser or multi-user deployment
authentication acceptance test.
"""
from __future__ import annotations

import asyncio
import json
import os
from contextlib import AsyncExitStack
from pathlib import Path

import pytest
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve

from jiuwenswarm.common.e2a.wire_codec import parse_agent_server_wire_unary
from jiuwenswarm.governance.contracts import TrustedIdentity
from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer
from jiuwenswarm.server.runtime.gateway_adapter import AdapterRegistry, ProjectAdapter
from jiuwenswarm.server.runtime.session import project_store

pytestmark = [pytest.mark.integration, pytest.mark.system]


@pytest.mark.asyncio
async def test_real_websocket_project_acl_create_read_revoke_and_reconnect(tmp_path, monkeypatch):
    monkeypatch.setattr(project_store, "get_agent_root_dir", lambda: tmp_path / "agent")
    project_store.invalidate_cache()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    observations = []

    def dispatcher(actor):
        server = AgentWebSocketServer.__new__(AgentWebSocketServer)
        identity = TrustedIdentity(actor, actor, "injected-test-host") if actor else None
        server._trusted_identity_resolver = lambda _request: identity
        server._adapter_registry = AdapterRegistry()
        server._adapter_registry.register(ProjectAdapter(identity_resolver=server._resolve_trusted_identity))

        async def handle(ws):
            lock = asyncio.Lock()
            async for raw in ws:
                await server._handle_message(ws, raw, lock)
        return handle

    async with AsyncExitStack() as stack:
        addresses = {}
        for actor in ("owner", "reader", ""):
            listener = await stack.enter_async_context(serve(dispatcher(actor), "127.0.0.1", 0))
            addresses[actor] = f"ws://127.0.0.1:{listener.sockets[0].getsockname()[1]}"

        async def rpc(actor, method, **params):
            # A fresh real connection for every operation verifies that a cached
            # grant or reconnect cannot defeat persisted revocation.
            async with connect(addresses[actor]) as ws:
                await ws.send(json.dumps({
                    "request_id": f"probe-{len(observations)}", "channel_id": "web",
                    "req_method": method, "user_id": "owner", "params": params,
                    "metadata": {"actor_id": "owner", "authority": "forged"},
                }))
                result = parse_agent_server_wire_unary(json.loads(await asyncio.wait_for(ws.recv(), 15)))
                observations.append({"actor": actor, "method": method, "ok": result.ok,
                                     "code": (result.payload or {}).get("code")})
                return result

        created = await rpc("owner", "project.create", name="Evidence", project_dir=str(workspace), work_mode="work")
        assert created.ok, created.payload
        project_id = created.payload["project_id"]
        granted = await rpc("owner", "project.acl.update", project_id=project_id,
                            acl={"reader": ["read"]}, expected_revision=1)
        assert granted.ok and granted.payload["acl_revision"] == 2
        assert (await rpc("reader", "project.info", project_id=project_id)).ok
        assert not (await rpc("reader", "project.rename", project_id=project_id, name="denied")).ok
        assert not (await rpc("reader", "project.delete", project_id=project_id)).ok
        assert not (await rpc("", "project.info", project_id=project_id)).ok
        assert (await rpc("owner", "project.extensions.update", project_id=project_id,
                          goal="Compare two cited sources", extensions={"research": {"version": 1}})).ok
        revoked = await rpc("owner", "project.acl.update", project_id=project_id, acl={}, expected_revision=2)
        assert revoked.ok and revoked.payload["acl_revision"] == 3
        assert not (await rpc("reader", "project.info", project_id=project_id)).ok
        assert not (await rpc("reader", "project.extensions.get", project_id=project_id)).ok
        assert not (await rpc("reader", "session.list")).ok
        final = await rpc("owner", "project.extensions.get", project_id=project_id)
        assert final.ok and final.payload["goal"] == "Compare two cited sources"

    project_store.invalidate_cache()
    evidence = os.getenv("R1_THREE_EVIDENCE_DIR")
    if evidence:
        target = Path(evidence)
        target.mkdir(parents=True, exist_ok=True)
        (target / "project-ws-canary.json").write_text(json.dumps({
            "kind": "real-loopback-websocket-and-disk", "remote_auth": "not_tested",
            "operations": observations, "listeners_closed": True,
        }, indent=2) + "\n")
