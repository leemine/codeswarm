"""Opt-in original Web UI recovery of durable Browser Artifact events.

Uses isolated AgentServer/Gateway/app_web and real Chrome, without a model
request or browser store injection. This tests history-to-UI recovery, not
end-to-end live Gateway acknowledgement or Browser action authorization.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.system, pytest.mark.skipif(
    os.environ.get("RUN_BROWSER_ARTIFACT_UI") != "1", reason="original Artifact Web UI is opt-in",
)]


@pytest.mark.asyncio
async def test_durable_artifact_history_recovers_once_and_downloads(tmp_path: Path, monkeypatch):
    scope = tmp_path / "artifact-ui"
    scope.mkdir()
    monkeypatch.setenv("JIUWENSWARM_DATA_DIR", str(scope / "data"))
    monkeypatch.setenv("JIUWENSWARM_CONFIG_DIR", str(scope / "data/config"))
    monkeypatch.setenv("HEARTBEAT_REMOTE_API_BASE", "http://127.0.0.1:9/v1")
    monkeypatch.setenv("HEARTBEAT_REMOTE_API_KEY", "artifact-ui-unused-fixture-key")
    monkeypatch.setenv("HEARTBEAT_REMOTE_MODEL", "unused-local-fixture")
    from playwright.async_api import async_playwright, expect
    import websockets
    from .goal_browser_remote_support import browser_services, digest, preflight
    from .test_heartbeat_channels_remote import rpc
    from .test_external_codex_web_channel_local import _wait_for_log

    dist = preflight("native")
    chrome = shutil.which("google-chrome") or shutil.which("chromium")
    assert chrome, "Explicit opt-in requires Chrome/Chromium"
    evidence = {"frontend_index_sha256": digest(dist / "index.html"), "checks": []}
    try:
        async with asyncio.timeout(180):
            async with browser_services(scope, "native") as (url, data):
                await _wait_for_log(scope / "gateway.log", "startup stage=ready", timeout=30)
                async with websockets.connect(url.replace("http://", "ws://") + "/ws") as ws:
                    created = await rpc(ws, "session.create", {
                        "persist_session": True, "mode": "agent.code", "work_mode": "code",
                    }, "artifact-ui-create")
                session_id = created["session_id"]
                # An independent host with no push transport commits history
                # before delivery fails. Recreating it cannot duplicate history.
                seed = '''
import asyncio, sys
from pathlib import Path
from jiuwenswarm.agents.harness.common.tools.send_file_to_user import SendFileToolkit
from jiuwenswarm.server.runtime.session.session_history import load_history_records, flush_pending_writes
async def main():
    path = Path(sys.argv[2]) / "agent/workspace/artifact-ui.txt"
    path.write_text("R1-10D_ARTIFACT_UI_OK", encoding="utf-8")
    for request in ("first-owner", "recreated-owner"):
        toolkit = SendFileToolkit(request_id=request, session_id=sys.argv[1], channel_id="web")
        try:
            await toolkit.deliver_projected_artifact(path, {"artifactId": "artifact-ui-stable", "metadata": {}})
        except RuntimeError as error:
            assert "not accepted" in str(error), str(error)
        else:
            raise AssertionError("isolated host unexpectedly accepted live delivery")
    rows = load_history_records(sys.argv[1])
    assert len([r for r in rows if r.get("event_type") == "chat.file"]) == 1
    assert flush_pending_writes()
asyncio.run(main())
'''
                process = await asyncio.create_subprocess_exec(
                    sys.executable, "-c", seed, session_id, str(data), cwd=scope,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                )
                try:
                    output, _ = await asyncio.wait_for(process.communicate(), 45)
                    assert process.returncode == 0, output.decode()[-3000:]
                finally:
                    if process.returncode is None:
                        process.kill()
                        await process.wait()
                evidence["checks"].append("durable_history_once_after_owner_recreation")
                async with async_playwright() as playwright:
                    browser = await playwright.chromium.launch(executable_path=chrome, headless=True)
                    try:
                        page = await browser.new_page(viewport={"width": 1440, "height": 1000})
                        page.set_default_timeout(30_000)
                        await page.goto(url + "/chat/" + session_id, wait_until="domcontentloaded")
                        await page.get_by_test_id("model-setup-guide-skip").click()
                        card = page.get_by_test_id("chat-panel-file-download-item")
                        await expect(card).to_have_count(1)
                        await expect(card).to_contain_text("artifact-ui.txt")
                        await page.reload(wait_until="domcontentloaded")
                        await expect(card).to_have_count(1)
                        await expect(card).to_contain_text("artifact-ui.txt")
                        evidence["checks"].append("original_web_history_and_refresh_single_card")
                        async with page.expect_download() as download_info:
                            await card.get_by_test_id("chat-panel-file-download-btn").click()
                        downloaded = await download_info.value
                        assert await downloaded.failure() is None
                        assert Path(await downloaded.path()).read_text() == "R1-10D_ARTIFACT_UI_OK"
                        evidence["checks"].append("original_file_api_download_bytes_verified")
                        await page.screenshot(path=str(scope / "artifact-ui.png"))
                    finally:
                        await browser.close()
        evidence["passed"] = True
    finally:
        (scope / "artifact-ui-result.json").write_text(json.dumps(evidence, indent=2))
