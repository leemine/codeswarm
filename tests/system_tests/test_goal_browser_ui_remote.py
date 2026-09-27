"""Real Native/Codex Goal through the original built Web UI and Chromium.

RUN_GOAL_BROWSER_REMOTE=1 explicitly opts in to sending synthetic objectives,
system prompts, and tool output to HEARTBEAT_REMOTE_API_BASE using the explicit
HEARTBEAT_REMOTE_API_KEY/MODEL. No personal configuration is read. Install the
locked test/codex dependencies, Playwright Chromium, and build the frontend.
Optional GOAL_BROWSER_ARTIFACT_DIR retains isolated scopes for CI artifacts.
Disabled cases remain individually collected skips; an opted-in missing
dependency, browser, frontend, or configuration is a failure, never a skip.
"""
from __future__ import annotations

import asyncio
import json
import os
import shlex
import tempfile
from pathlib import Path

import pytest

from .goal_browser_remote_support import (
    browser_services, digest, history_evidence, observe_browser_channel, preflight, wait_code_mode_complete,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.system,
    pytest.mark.skipif(os.environ.get("RUN_GOAL_BROWSER_REMOTE") != "1", reason="real Goal browser UI is opt-in"),
]


async def _send(page, text: str) -> None:
    await page.get_by_test_id("chat-panel-input").fill(text)
    await page.get_by_test_id("chat-panel-input-send").click()


async def _history_visible(page, testid: str, *, top: bool = False) -> None:
    """Scroll the original virtual timeline; no store or browser state injection."""
    target = page.get_by_test_id(testid)
    scroll = page.get_by_test_id("chat-panel-scroll")
    for _ in range(80):
        if await target.count() and await target.first.is_visible():
            return
        await scroll.hover()
        await page.mouse.wheel(0, -900 if top else 900)
        await asyncio.sleep(0.25)
    raise AssertionError(f"Original timeline did not display {testid}")


@pytest.mark.asyncio
@pytest.mark.timeout(600)
@pytest.mark.parametrize("provider", ["native", "codex"])
async def test_real_goal_browser_create_pause_resume_complete_refresh_clear(tmp_path: Path, monkeypatch, provider: str):
    dist = preflight(provider)
    from playwright.async_api import async_playwright, expect

    output = Path(os.environ.get("GOAL_BROWSER_ARTIFACT_DIR", str(tmp_path)))
    output.mkdir(parents=True, exist_ok=True)
    scope = Path(tempfile.mkdtemp(prefix=f"goal-browser-{provider}-", dir=output))
    monkeypatch.setenv("JIUWENSWARM_DATA_DIR", str(scope / "data"))
    monkeypatch.setenv("JIUWENSWARM_CONFIG_DIR", str(scope / "data/config"))
    evidence = {"provider": provider, "frontend_index_sha256": digest(dist / "index.html"), "checks": []}
    secret = os.environ["HEARTBEAT_REMOTE_API_KEY"]
    try:
        async with asyncio.timeout(540):
            async with browser_services(scope, provider) as (url, data):
                # Close browser and Playwright's driver before auditing owned
                # service processes: the driver inherits this isolated DATA path.
                async with async_playwright() as playwright:
                    browser_env = {key: value for key, value in os.environ.items()
                                   if key not in {"HEARTBEAT_REMOTE_API_KEY", "HEARTBEAT_REMOTE_API_BASE"}}
                    browser = await playwright.chromium.launch(
                        headless=True, args=["--disable-dev-shm-usage"], env=browser_env)
                    page = None
                    try:
                        context = await browser.new_context(viewport={"width": 1440, "height": 1000})
                        page = await context.new_page()
                        observe_browser_channel(page, evidence)
                        evidence["page_errors"] = []
                        page.on("pageerror", lambda error: evidence["page_errors"].append(str(error).replace(secret, "[REDACTED]")[:1000]))
                        page.set_default_timeout(45_000)
                        await page.goto(url, wait_until="domcontentloaded")
                        await expect(page.get_by_test_id("app-shell")).to_be_visible()
                        # A fresh generated workspace shows the original model
                        # onboarding overlay even though its model is configured.
                        # Dismiss through its real UI, never by editing stores.
                        await page.get_by_test_id("model-setup-guide-skip").click()
                        await expect(page.get_by_test_id("model-setup-guide")).to_have_count(0)
                        evidence["checks"].append("original_onboarding_dismissed")
                        mode = page.get_by_test_id("multi-session-work-mode-label")
                        if await mode.get_attribute("data-variant") != "code":
                            mode_start = len(evidence["browser_channel"])
                            await page.get_by_test_id("multi-session-work-mode-trigger").click()
                            await page.get_by_test_id("multi-session-work-mode-menu-code").click()
                            await wait_code_mode_complete(evidence, mode_start)
                            await expect(page).to_have_url(url + "/chat/new")
                            await expect(page.get_by_test_id("chat-panel-input")).to_be_editable()
                        await expect(mode).to_have_attribute("data-variant", "code")
                        evidence["checks"].append("code_mode_switch_completed")
                        marker = f"GOAL_BROWSER_{provider.upper()}_OK"
                        artifact = data / "agent/workspace" / f"goal-browser-{provider}.txt"
                        command = f"sleep 15; printf '%s' {shlex.quote(marker)} > {shlex.quote(str(artifact))}; cat {shlex.quote(str(artifact))}"
                        objective = (
                            f"Create {artifact} containing exactly {marker} and verify it with the shell. "
                            "First call get_current_goal. Execute this exact command with the shell tool: "
                            f"{command}\nThen submit_goal_report status complete with the verified path and contents as evidence. "
                            "Use the actual current goal identity from get_current_goal; never invent one. "
                            "Do not delegate or ask questions. On resume continue the same objective."
                        )
                        # This is the original menu entry for /goal, not an RPC set.
                        await page.get_by_test_id("chat-panel-input-attach-trigger").click()
                        await page.get_by_test_id("chat-panel-input-attach-menu-goal").click()
                        await expect(page.get_by_test_id("chat-panel-goal-tag")).to_be_visible()
                        await _send(page, objective)
                        # Require the original welcome-submit navigation to
                        # select the new conversation; do not repair it in tests.
                        conversation = page.get_by_test_id("multi-session-conversation-list-item")
                        await expect(conversation).to_have_count(1)
                        created_session = await conversation.get_attribute("data-variant")
                        await expect(page.get_by_test_id("app-shell")).to_have_attribute("data-session-id", created_session)
                        evidence["selected_session"] = created_session
                        evidence["selected_url"] = page.url
                        status = page.get_by_test_id("goal-bar-status")
                        await expect(status).to_have_attribute("data-variant", "active", timeout=90_000)
                        session_id = await page.get_by_test_id("app-shell").get_attribute("data-session-id")
                        assert session_id and session_id != "new"
                        button = page.get_by_test_id("goal-bar-pause-resume-button")
                        await button.click()
                        # Unlike the optimistically toggled button variant, status
                        # follows the actual Goal snapshot returned by the service.
                        await expect(status).to_have_attribute("data-variant", "paused", timeout=90_000)
                        evidence["checks"].append("create_then_pause_confirmed")
                        await button.click()
                        await expect(status).to_have_attribute("data-variant", "active", timeout=90_000)
                        evidence["checks"].append("resume_confirmed")
                        card = page.get_by_test_id("goal-bar-completed-card")
                        await expect(card).to_have_count(1, timeout=240_000)
                        await expect(card).to_be_visible()
                        evidence.update(history_evidence(data, session_id, marker, artifact, provider))
                        evidence["checks"].append("real_artifact_and_unique_completed_history")
                        await page.reload(wait_until="domcontentloaded")
                        await expect(page.get_by_test_id("app-shell")).to_have_attribute("data-session-id", session_id)
                        await _history_visible(page, "goal-bar-completed-card")
                        await expect(card).to_have_count(1)
                        await _history_visible(page, "chat-panel-message-goal-badge", top=True)
                        objective_row = page.get_by_test_id("chat-panel-message-row").filter(
                            has=page.get_by_test_id("chat-panel-message-goal-badge"))
                        await expect(objective_row).to_contain_text(marker)
                        evidence["checks"].append("reload_preserves_session_objective_and_card")
                        await _send(page, "/goal clear")
                        await expect(page.get_by_test_id("goal-bar")).to_have_count(0)
                        await _send(page, "/goal")
                        await expect(page.get_by_text("当前会话没有持续目标。", exact=False)).to_be_visible()
                        history_evidence(data, session_id, marker, artifact, provider)
                        evidence["checks"].append("clear_confirmed_history_retained")
                        await context.close()
                    finally:
                        try:
                            if page is not None and not page.is_closed():
                                evidence["last_url"] = page.url
                                evidence["last_session"] = await page.get_by_test_id("app-shell").get_attribute("data-session-id", timeout=3000)
                        except Exception as error:
                            evidence["page_diagnostic_error"] = type(error).__name__
                        finally:
                            await browser.close()
        evidence["passed"] = True
    except Exception as error:
        evidence["passed"] = False
        evidence["error_type"] = type(error).__name__
        evidence["error"] = str(error).replace(secret, "[REDACTED]")[:4000]
        # Shared service readiness helpers can include log tails in exceptions.
        # Never export a credential through a pytest failure, even on startup.
        raise AssertionError(str(error).replace(secret, "[REDACTED]")) from None
    finally:
        (scope / "browser-result.json").write_text(json.dumps(evidence, indent=2))
