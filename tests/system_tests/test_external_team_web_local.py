"""Real WebChannel socket + React DOM consumer + original Team Runtime controls.

Gateway-to-AgentServer hop and admitted root setup are an explicit in-process
fixture; WebChannel, wire codecs, Runtime, Runner and history handler are real.
"""

import asyncio
from dataclasses import asdict
import json
import os
from pathlib import Path
import socket
from types import SimpleNamespace

import pytest
import uvicorn
from jiuwenswarm.common.schema.message import Message
from jiuwenswarm.common.e2a.wire_codec import parse_agent_server_wire_unary
from jiuwenswarm.gateway.channel_manager.base import RobotMessageRouter
from jiuwenswarm.gateway.channel_manager.web.web_connect import (
    WebChannel,
    WebChannelConfig,
)
from jiuwenswarm.gateway.channel_manager.web.web_channel_app import (
    build_web_channel_app,
)
from jiuwenswarm.gateway.app_gateway import _normalize_gateway_message
from jiuwenswarm.server.agent_ws_server import _payload_to_request, AgentWebSocketServer
from jiuwenswarm.runtime.session import SessionWorkKind
from tests.system_tests.test_external_team_runtime_interactions_local import (
    RuntimeProbe,
    _run_goal,
    recovery_env,
    pytestmark,
)


class WebProbe(RuntimeProbe):
    channel_id = "web"

    def __init__(self, evidence):
        super().__init__("allow")
        self.evidence = evidence
        self.outbound = asyncio.Queue()
        self.inbound = []

    def observe(self, chunk):
        self.outbound.put_nowait(chunk)

    async def run(self, coordinator, produce, adapter, request, scenario):
        channel = WebChannel(WebChannelConfig(enabled=True), RobotMessageRouter())
        ready = asyncio.Event()

        async def on_message(message):
            message = _normalize_gateway_message(message)
            self.inbound.append(
                {"method": message.req_method.value, "params": message.params}
            )
            wire = asdict(message)
            wire["request_id"] = message.id
            wire["req_method"] = message.req_method.value
            req = _payload_to_request(wire)
            clients = list(channel.clients)
            assert len(clients) == 1
            ws = clients[0]
            if message.req_method.value == "test.ready":
                raise AssertionError("ready should be a local method")
            if message.req_method.value == "team.history.get":

                async def send(encoded):
                    response = parse_agent_server_wire_unary(json.loads(encoded))
                    await channel.send_response(
                        ws, message.id, ok=response.ok, payload=response.payload
                    )

                await AgentWebSocketServer._handle_team_history_get(
                    None, SimpleNamespace(send=send), req, asyncio.Lock()
                )
                return True
            try:
                async for event in self.runtime.stream(req):
                    await publish(event)
                await channel.send_response(
                    ws, message.id, ok=True, payload={"accepted": True}
                )
            except Exception as exc:
                await channel.send_response(ws, message.id, ok=False, error=str(exc))
            return True

        async def initialized(ws, req_id, params, session_id):
            await channel.send_response(ws, req_id, ok=True)
            ready.set()

        channel.register_method("test.ready", initialized, local_only=True)
        channel.on_message(on_message)

        async def publish(chunk):
            await channel.send(
                Message(
                    id=chunk.request_id,
                    type="event",
                    channel_id="web",
                    session_id="team-local",
                    params={},
                    timestamp=0,
                    ok=True,
                    payload=chunk.payload,
                )
            )

        async def relay():
            while True:
                item = await self.outbound.get()
                try:
                    if item is None:
                        return
                    await publish(item)
                finally:
                    self.outbound.task_done()

        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        server = uvicorn.Server(
            uvicorn.Config(
                build_web_channel_app(channel), log_level="warning", lifespan="off"
            )
        )
        serving = asyncio.create_task(server.serve(sockets=[sock]))
        forwarding = asyncio.create_task(relay())
        child = None
        output = None
        task = None
        waiting = None
        exited = None
        try:
            while not server.started:
                if serving.done():
                    await serving
                await asyncio.sleep(0.01)
            script = (
                Path(__file__).parents[2]
                / "jiuwenswarm/channels/web/frontend/tests/teamReviewerLocal.client.mjs"
            )
            self.evidence.mkdir(parents=True, exist_ok=True)
            output = (self.evidence / "ui.log").open("wb")
            child = await asyncio.create_subprocess_exec(
                "node",
                str(script),
                f"http://127.0.0.1:{port}",
                str(self.evidence / "ui.json"),
                stdout=output,
                stderr=asyncio.subprocess.STDOUT,
            )
            waiting = asyncio.create_task(ready.wait())
            exited = asyncio.create_task(child.wait())
            done, _ = await asyncio.wait(
                {waiting, exited}, timeout=30, return_when=asyncio.FIRST_COMPLETED
            )
            if exited in done:
                raise AssertionError((self.evidence / "ui.log").read_text())
            assert ready.is_set(), "UI did not connect"
            waiting.cancel()
            task = asyncio.create_task(
                coordinator.run_unary(
                    "team-local", "root-goal", SessionWorkKind.GOAL_STREAM, produce
                )
            )
            done, _ = await asyncio.wait(
                {task, exited}, return_when=asyncio.FIRST_COMPLETED
            )
            if exited in done and child.returncode:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                raise AssertionError((self.evidence / "ui.log").read_text())
            await task
            await self.outbound.join()
            for ws in channel.clients:
                await channel.send_event(ws, "test.done", {"session_id": "team-local"})
            await asyncio.wait_for(exited, 20)
            assert child.returncode == 0, (self.evidence / "ui.log").read_text()
            report = json.loads((self.evidence / "ui.json").read_text())
            assert any(
                p.get("execution_kind") == "scheduled_review" for p in report["frames"]
            )
            answers = [x for x in self.inbound if x["method"] == "chat.send"]
            assert len(answers) == len(report["seen"])
            assert all(x["params"]["session_generation"] > 0 for x in answers)
            assert all(x["params"]["mode"] == "team.code.normal" for x in answers), (
                answers
            )
            (self.evidence / "wire.json").write_text(
                json.dumps(self.inbound, ensure_ascii=False, indent=2) + "\n"
            )
            return False
        finally:
            (self.evidence / "wire.json").write_text(
                json.dumps(self.inbound, ensure_ascii=False, indent=2) + "\n"
            )
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            if waiting is not None:
                waiting.cancel()
                await asyncio.gather(waiting, return_exceptions=True)
            if child is not None and child.returncode is None:
                child.terminate()
                await asyncio.wait_for(child.wait(), 10)
            if exited is not None:
                await asyncio.gather(exited, return_exceptions=True)
            if output is not None:
                output.close()
            forwarding.cancel()
            await asyncio.gather(forwarding, return_exceptions=True)
            server.should_exit = True
            await asyncio.wait_for(serving, 10)


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["codex", "opencode"])
async def test_real_team_web_approval_and_history(
    tmp_path, monkeypatch, recovery_env, provider
):
    evidence = (
        Path(os.environ.get("TEAM_REVIEW_EVIDENCE_DIR", str(tmp_path))) / provider
    )
    probe = WebProbe(evidence)
    try:
        await _run_goal(
            tmp_path, monkeypatch, provider, recovery_env, "scheduled", probe=probe
        )
    finally:
        if probe.runtime is not None:
            await probe.runtime.close()
