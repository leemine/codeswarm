"""Real Gateway process and Chrome authorization over AgentServer's E2A handler.

Root admission/model/registry remain the existing Team fixture. The browser mounts
original authorization components in a fixture shell, not the full application.
"""

import asyncio
import json
import os
from pathlib import Path
import shutil
import sys
from contextlib import AsyncExitStack

import pytest
import websockets
from playwright.async_api import async_playwright, expect
from jiuwenswarm.common.e2a import E2AEnvelope
from jiuwenswarm.common.e2a.agent_compat import e2a_to_agent_request
from jiuwenswarm.runtime.session import SessionWorkKind
from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer
from tests.system_tests.test_external_team_runtime_interactions_local import (
    RuntimeProbe,
    _run_goal,
    recovery_env,
    pytestmark,
)
from tests.system_tests.test_external_codex_web_channel_local import (
    _pick_free_port,
    _wait_for_log,
)


class GatewayProbe(RuntimeProbe):
    channel_id = "web"

    def __init__(self, scope):
        super().__init__("allow")
        self.scope = scope
        self.outputs = asyncio.Queue()
        self.frames = []

    def observe(self, chunk):
        self.outputs.put_nowait(chunk)

    async def run(self, coordinator, produce, adapter, request, scenario):
        from jiuwenswarm.common.utils import prepare_workspace

        self.scope.mkdir(parents=True, exist_ok=True)
        data = self.scope / "data"
        prepare_workspace(overwrite=False, workspace_dir=data)
        frontend = Path(__file__).parents[2] / "jiuwenswarm/channels/web/frontend"
        dist = self.scope / "dist"
        dist.mkdir()
        css = next((frontend / "dist/assets").glob("index-*.css"))
        shutil.copy2(css, dist / "app.css")
        build = await asyncio.create_subprocess_exec(
            "node",
            "tests/buildTeamGatewayBrowser.mjs",
            str(dist / "fixture.js"),
            cwd=frontend,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        build_output, _ = await build.communicate()
        assert build.returncode == 0, build_output.decode()
        (dist / "index.html").write_text(
            '<!doctype html><html data-theme="default" data-color-mode="light"><head><title>WorkSwarm</title><link rel="stylesheet" href="/app.css"></head><body><main style="max-width:900px;margin:60px auto"><div id="root"></div></main><script type="module" src="/fixture.js"></script></body></html>'
        )
        server = object.__new__(AgentWebSocketServer)
        server._runtime = self.runtime
        server._agent_manager = self.runtime.agent_manager
        server._session_stream_tasks = {}
        server._current_ws = None
        server._current_send_lock = asyncio.Lock()
        ready = asyncio.Event()
        controls = asyncio.Queue()
        release = asyncio.Event()
        requests = set()
        processes = []
        logs = []
        producer = None

        async def connection(ws):
            server._current_ws = ws
            await ws.send(json.dumps({"type": "event", "event": "connection.ack"}))

            async def dispatch(raw):
                envelope = E2AEnvelope.from_dict(json.loads(raw))
                incoming = e2a_to_agent_request(envelope)
                if incoming.req_method.value == "team.history.get":
                    ready.set()
                if incoming.req_method.value == "chat.send":
                    self.frames.append(
                        {
                            "agent_request": incoming.request_id,
                            "interaction_id": incoming.params.get("request_id"),
                        }
                    )
                    await controls.put(incoming.request_id)
                    await release.wait()
                    release.clear()
                await server._handle_message(ws, raw, server._current_send_lock)

            try:
                async for raw in ws:
                    task = asyncio.create_task(dispatch(raw))
                    requests.add(task)
                    task.add_done_callback(requests.discard)
            finally:
                await asyncio.gather(*requests, return_exceptions=True)
                server._release_current_connection(ws)

        async def forward():
            while True:
                chunk = await self.outputs.get()
                try:
                    await server.send_push(
                        {
                            "channel_id": "web",
                            "session_id": "team-local",
                            "payload": {**chunk.payload, "session_id": "team-local"},
                        }
                    )
                finally:
                    self.outputs.task_done()

        async def stop(process):
            if process.returncode is None:
                process.terminate()
            try:
                await asyncio.wait_for(process.wait(), 10)
            except TimeoutError:
                process.kill()
                await process.wait()

        async def start(command, env, name):
            log = (self.scope / (name + ".log")).open("wb")
            logs.append(log)
            child = await asyncio.create_subprocess_exec(
                *command,
                cwd=self.scope,
                env=env,
                stdout=log,
                stderr=asyncio.subprocess.STDOUT,
            )
            processes.append(child)
            stack.push_async_callback(stop, child)
            return child

        try:
            async with AsyncExitStack() as stack:
                listener = await stack.enter_async_context(
                    websockets.serve(connection, "127.0.0.1", 0)
                )
                env = os.environ.copy()
                web_port = _pick_free_port()
                http_port = _pick_free_port()
                env.update(
                    JIUWENSWARM_DATA_DIR=str(data),
                    JIUWENSWARM_CONFIG_DIR=str(data / "config"),
                    JIUWENSWARM_CONFIG_URL="off",
                    JIUWENSWARM_AGENT_PREWARM="0",
                    HEALTH_CHECK_INTERVAL="3600",
                    AGENT_SERVER_HOST="127.0.0.1",
                    AGENT_SERVER_PORT=str(listener.sockets[0].getsockname()[1]),
                    GATEWAY_HOST="127.0.0.1",
                    GATEWAY_PORT=str(_pick_free_port()),
                    WEB_HOST="127.0.0.1",
                    WEB_PORT=str(web_port),
                    HOME=str(self.scope / "home"),
                )
                await start(
                    [
                        sys.executable,
                        "-m",
                        "jiuwenswarm.gateway.app_gateway",
                        "--port",
                        str(web_port),
                    ],
                    env,
                    "gateway",
                )
                await _wait_for_log(
                    self.scope / "gateway.log",
                    "startup stage=web_channel_listening",
                    timeout=40,
                )
                await start(
                    [
                        sys.executable,
                        "-m",
                        "jiuwenswarm.channels.web.app_web",
                        "--host",
                        "127.0.0.1",
                        "--port",
                        str(http_port),
                        "--dist",
                        str(dist),
                        "--proxy-target",
                        f"http://127.0.0.1:{web_port}",
                    ],
                    env,
                    "frontend",
                )
                from aiohttp import ClientSession

                async with ClientSession() as http:
                    for _ in range(100):
                        try:
                            async with http.get(
                                f"http://127.0.0.1:{http_port}"
                            ) as response:
                                if response.status == 200:
                                    break
                        except OSError:
                            pass
                        await asyncio.sleep(0.2)
                    else:
                        raise AssertionError("app_web HTTP readiness failed")
                forwarding = asyncio.create_task(forward())
                try:
                    async with async_playwright() as playwright:
                        browser = await playwright.chromium.launch(
                            executable_path=shutil.which("google-chrome"), headless=True
                        )
                        try:
                            page = await browser.new_page(
                                viewport={"width": 1200, "height": 800}
                            )
                            page.on(
                                "websocket",
                                lambda ws: ws.on(
                                    "framereceived",
                                    lambda raw: self.frames.append(json.loads(raw)),
                                ),
                            )
                            page.on(
                                "pageerror",
                                lambda e: self.frames.append({"page_error": str(e)}),
                            )
                            await page.goto(
                                f"http://127.0.0.1:{http_port}",
                                wait_until="domcontentloaded",
                            )
                            await asyncio.wait_for(ready.wait(), 20)
                            producer = asyncio.create_task(
                                coordinator.run_unary(
                                    "team-local",
                                    "root-goal",
                                    SessionWorkKind.GOAL_STREAM,
                                    produce,
                                )
                            )
                            answers = 0
                            while not producer.done():
                                prompt = page.get_by_test_id(
                                    "interaction-slot-auth-prompt"
                                )
                                await expect(prompt).to_be_visible(timeout=40000)
                                await prompt.locator(
                                    '[data-variant="allow-once"]'
                                ).click()
                                req_id = await asyncio.wait_for(controls.get(), 10)
                                for _ in range(100):
                                    if any(
                                        f.get("id") == req_id
                                        and f.get("type") == "res"
                                        and f.get("ok")
                                        for f in self.frames
                                    ):
                                        break
                                    await asyncio.sleep(0.02)
                                else:
                                    raise AssertionError(
                                        "Gateway early response not observed"
                                    )
                                await expect(prompt).to_be_visible()
                                await expect(
                                    prompt.locator('[data-variant="allow-once"]')
                                ).to_be_disabled()
                                if answers == 2:
                                    await page.screenshot(
                                        path=str(
                                            self.scope / "approval-waits-runtime.png"
                                        )
                                    )
                                release.set()
                                answers += 1
                                for _ in range(200):
                                    settled = [
                                        f
                                        for f in self.frames
                                        if f.get("payload", {}).get("request_id")
                                        == req_id
                                        and f.get("event")
                                        in {"runtime.accepted", "chat.error"}
                                    ]
                                    if settled:
                                        break
                                    await asyncio.sleep(0.02)
                                assert (
                                    settled
                                    and settled[-1].get("event") == "runtime.accepted"
                                ), self.frames[-12:]
                                old_id = next(
                                    f["interaction_id"]
                                    for f in self.frames
                                    if f.get("agent_request") == req_id
                                )
                                await expect(
                                    page.locator(
                                        f'[data-testid="interaction-slot-auth-prompt"][data-request-id="{old_id}"]'
                                    )
                                ).to_have_count(0)

                                # The fixture has exactly build/complete/verify/two view approvals.
                                if answers == 5:
                                    break
                            await producer
                            await self.outputs.join()
                            review_titles = page.get_by_test_id(
                                "team-area-process-item-title"
                            ).filter(has_text="reviewer")
                            await expect(review_titles).to_have_count(2)
                            await page.screenshot(
                                path=str(self.scope / "reviewer-live.png")
                            )
                            await server.send_push(
                                {
                                    "channel_id": "web",
                                    "session_id": "team-local",
                                    "payload": {
                                        "event_type": "test.done",
                                        "session_id": "team-local",
                                    },
                                }
                            )
                            output = page.get_by_test_id("fixture-report")
                            await expect(output).to_be_visible(timeout=20000)
                            report = json.loads(await output.inner_text())
                            report["browser_version"] = browser.version
                            assert (
                                report["answers"] == [True] * 5
                                and report["errors"] == []
                            ), report
                            rows = report["history"]["raw"]["records"]
                            assert any(
                                r.get("execution_kind") == "scheduled_review"
                                and r.get("event_type") == "chat.final"
                                for r in rows
                            )
                            assert (
                                report["history"]["restored"]["tasks"][0]["status"]
                                == "completed"
                            )
                            assert (
                                len(
                                    [
                                        e
                                        for e in report["live"][
                                            "teamMemberExecutionEvents"
                                        ]
                                        if e.get("review")
                                    ]
                                )
                                == 3
                            )
                            assert not any(
                                m["member_id"] == "reviewer"
                                for m in report["live"]["teamMembers"]
                            )
                            assert {
                                e["id"]
                                for e in report["live"]["teamMemberExecutionEvents"]
                                if e.get("review")
                            } == {
                                e["id"]
                                for e in report["history"]["restored"][
                                    "executionEvents"
                                ]
                                if e.get("review")
                            }
                            await expect(review_titles).to_have_count(2)
                            review_row = (
                                page.get_by_test_id("team-area-process-item")
                                .filter(
                                    has=page.get_by_test_id(
                                        "team-area-process-item-title"
                                    ).filter(has_text="reviewer")
                                )
                                .first
                            )
                            await review_row.get_by_test_id(
                                "team-area-process-item-toggle"
                            ).click()
                            await expect(
                                review_row.get_by_test_id("team-area-process-detail")
                            ).to_contain_text("审查标识")
                            await expect(
                                review_row.get_by_test_id("team-area-process-detail")
                            ).to_contain_text(
                                next(
                                    r["provider_id"]
                                    for r in rows
                                    if r.get("execution_kind") == "scheduled_review"
                                )
                            )
                            await expect(
                                review_row.get_by_test_id("team-area-process-detail")
                            ).to_contain_text("Vote recorded")
                            await page.screenshot(
                                path=str(self.scope / "reviewer-history.png")
                            )
                            await page.goto(
                                f"http://127.0.0.1:{http_port}/?restore=1",
                                wait_until="domcontentloaded",
                            )
                            await expect(
                                page.get_by_test_id("fixture-report")
                            ).to_be_visible(timeout=20000)
                            restored_report = json.loads(
                                await page.get_by_test_id("fixture-report").inner_text()
                            )
                            restored_events = restored_report["history"]["restored"][
                                "executionEvents"
                            ]
                            assert [
                                e["id"] for e in restored_events if e.get("review")
                            ] == [
                                e["id"]
                                for e in report["history"]["restored"][
                                    "executionEvents"
                                ]
                                if e.get("review")
                            ]
                            await expect(review_titles).to_have_count(2)
                            await review_row.get_by_test_id(
                                "team-area-process-item-toggle"
                            ).click()
                            await expect(
                                review_row.get_by_test_id("team-area-process-detail")
                            ).to_contain_text("审查标识")
                            await page.screenshot(
                                path=str(self.scope / "reviewer-reloaded.png")
                            )
                            report["reload"] = restored_report
                            assert not any("page_error" in f for f in self.frames)
                            (self.scope / "browser.json").write_text(
                                json.dumps(report, ensure_ascii=False, indent=2)
                            )
                        finally:
                            await browser.close()
                finally:
                    release.set()
                    if producer is not None and not producer.done():
                        producer.cancel()
                        await asyncio.gather(producer, return_exceptions=True)
                    forwarding.cancel()
                    await asyncio.gather(forwarding, return_exceptions=True)
                    for process in reversed(processes):
                        if process.returncode is None:
                            process.terminate()
                        try:
                            await asyncio.wait_for(process.wait(), 10)
                        except TimeoutError:
                            process.kill()
                            await process.wait()
        finally:
            for log in logs:
                log.close()
            (self.scope / "frames.json").write_text(
                json.dumps(self.frames, ensure_ascii=False, indent=2)
            )
            (self.scope / "cleanup.json").write_text(
                json.dumps({"processes_exited": [p.returncode for p in processes]})
            )
        return False


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["codex", "opencode"])
async def test_real_gateway_browser_reviewer_approval(
    tmp_path, monkeypatch, recovery_env, provider
):
    scope = Path(os.environ.get("TEAM_REVIEW_EVIDENCE_DIR", str(tmp_path))) / provider
    from openjiuwen.core.runner.callback.framework import AsyncCallbackFramework
    from jiuwenswarm.extensions.registry import ExtensionRegistry

    monkeypatch.setattr(ExtensionRegistry, "_instance", None)
    ExtensionRegistry.create_instance(AsyncCallbackFramework(), {}, None)
    probe = GatewayProbe(scope)
    try:
        await _run_goal(
            tmp_path, monkeypatch, provider, recovery_env, "scheduled", probe=probe
        )
    finally:
        if probe.runtime is not None:
            await probe.runtime.close()
