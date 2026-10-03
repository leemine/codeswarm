"""Actual organization login/share/view/revoke UI using two browser contexts."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import shutil
import sys

from _dual_user_gateway_probe import free_port, run


async def browser_probe(*, root, repo, web_port, tokens, sessions, env, children, dist):
    import httpx
    from playwright.async_api import async_playwright, expect

    port = free_port()
    with (root / "frontend.log").open("wb") as log:
        frontend = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "jiuwenswarm.channels.web.app_web",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--dist",
            str(dist),
            "--proxy-target",
            f"http://127.0.0.1:{web_port}",
            cwd=repo,
            env=env,
            stdout=log,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
    children.append(frontend)
    base = f"http://127.0.0.1:{port}"
    async with httpx.AsyncClient(timeout=2, trust_env=False) as http:
        async with asyncio.timeout(25):
            while True:
                try:
                    if (await http.get(base)).status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                await asyncio.sleep(0.1)
    frames, errors, pages = [], [], {}
    methods = {}
    checks = []

    def capture(actor, direction, raw):
        try:
            frame = json.loads(raw)
        except (ValueError, TypeError):
            return
        if direction == "sent":
            methods[(actor, frame.get("id"))] = frame.get("method")
        frames.append(
            {
                "actor": actor,
                "direction": direction,
                "id": frame.get("id"),
                "method": frame.get("method") or methods.get((actor, frame.get("id"))),
                "ok": frame.get("ok"),
                "code": frame.get("code"),
                "event": frame.get("event"),
                "session_id": frame.get("params", {}).get("session_id")
                if isinstance(frame.get("params"), dict)
                else None,
            }
        )

    async def wait_response(actor, method, *, after, ok):
        async with asyncio.timeout(25):
            while True:
                for frame in frames[after:]:
                    if (
                        frame["actor"] == actor
                        and frame["direction"] == "received"
                        and frame["method"] == method
                    ):
                        assert frame["ok"] is ok, frame
                        return
                await asyncio.sleep(0.1)

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            executable_path=shutil.which("google-chrome"), headless=True
        )
        try:
            for actor in tokens:
                context = await browser.new_context(
                    viewport={"width": 1440, "height": 1000}
                )
                page = await context.new_page()
                page.set_default_timeout(20000)
                pages[actor] = page
                page.on(
                    "pageerror",
                    lambda error, who=actor: errors.append(
                        {"actor": who, "error": str(error)}
                    ),
                )

                def on_socket(socket, who=actor):
                    socket.on("framesent", lambda raw: capture(who, "sent", raw))
                    socket.on(
                        "framereceived", lambda raw: capture(who, "received", raw)
                    )

                page.on("websocket", on_socket)
                await page.goto(base)
                await expect(page.get_by_test_id("auth-login-page")).to_be_visible()
                await page.get_by_test_id("auth-login-password-input").fill(
                    tokens[actor]
                )
                await page.get_by_test_id("auth-login-submit-button").click()
                await expect(page.get_by_test_id("auth-login-page")).to_have_count(0)
                await expect(
                    page.get_by_test_id("multi-session-open-shared-sessions")
                ).to_be_visible()
                await page.screenshot(
                    path=str(root / f"{actor}-dashboard.png"), full_page=True
                )
                checks.append(actor + " authenticated using actual login form")
            alice, bob = pages["alice"], pages["bob"]
            attack = await bob.context.new_page()
            attack.set_default_timeout(20000)
            pages["bob-owner-route"] = attack

            def attack_socket(socket):
                socket.on(
                    "framesent", lambda raw: capture("bob-owner-route", "sent", raw)
                )
                socket.on(
                    "framereceived",
                    lambda raw: capture("bob-owner-route", "received", raw),
                )

            attack.on("websocket", attack_socket)
            await attack.goto(base + "/chat/alice-private?user_id=alice")
            await expect(
                attack.get_by_role("heading", name="对话不存在或已删除")
            ).to_be_visible()
            await expect(attack.locator("body")).not_to_contain_text("alice-PRIVATE")
            await attack.screenshot(
                path=str(root / "bob-owner-route-denied.png"), full_page=True
            )
            checks.append(
                "Bob browsing owner route with forged user_id sees neutral unavailable page and no private content"
            )
            await attack.close()
            await bob.bring_to_front()
            # Expand the genuine project row to reveal the provisioned Session.
            project = alice.get_by_test_id("multi-session-project-row-main").filter(
                has_text="shared-project"
            )
            await expect(project).to_be_visible()
            await project.click()
            row = alice.get_by_test_id(
                "multi-session-conversation-list-item-main"
            ).filter(has_text="alice-PRIVATE-TITLE")
            await expect(row).to_be_visible()
            before_switch = len(frames)
            await row.click()
            await wait_response("alice", "session.switch", after=before_switch, ok=True)
            checks.append(
                "Alice owner Session switch preserves authenticated principal"
            )
            await expect(
                alice.get_by_test_id("chat-panel-share-export")
            ).to_be_enabled()
            await alice.get_by_test_id("chat-panel-share-export").click()
            await expect(
                alice.get_by_test_id("multi-session-sharing-form")
            ).to_be_visible()
            await alice.get_by_test_id("multi-session-sharing-target").fill("bob")
            await alice.get_by_test_id("multi-session-sharing-save").click()
            await expect(
                alice.get_by_test_id("multi-session-sharing-managed-item")
            ).to_have_count(1)
            await alice.screenshot(
                path=str(root / "alice-created-share.png"), full_page=True
            )
            checks.append("Alice created persistent share through owner dialog")
            await bob.get_by_test_id("multi-session-open-shared-sessions").click()
            await expect(
                bob.get_by_test_id("multi-session-sharing-received-item")
            ).to_have_count(1)
            await bob.get_by_test_id("multi-session-sharing-open").click()
            viewer = bob.get_by_test_id("multi-session-shared-history")
            await expect(viewer).to_be_visible()
            await expect(
                bob.get_by_test_id("multi-session-shared-history-message")
            ).to_have_count(2)
            await expect(viewer).to_contain_text("alice-PRIVATE-1")
            assert (
                await viewer.locator("textarea,input,[contenteditable=true]").count()
                == 0
            )
            await bob.screenshot(
                path=str(root / "bob-shared-history.png"), full_page=True
            )
            checks.append(
                "Bob opened sidebar inbox and readonly fixed history without execute input"
            )
            with (sessions / "alice-private/history.jsonl").open("a") as stream:
                stream.write(
                    json.dumps({"role": "user", "content": "AFTER-SHARE-PRIVATE"})
                    + "\n"
                )
            before_refresh = len(frames)
            await bob.get_by_test_id("multi-session-shared-history-refresh").click()
            await wait_response(
                "bob", "session.share.history.get", after=before_refresh, ok=True
            )
            await expect(
                bob.get_by_test_id("multi-session-shared-history-loading")
            ).to_have_count(0)
            await expect(viewer).not_to_contain_text("AFTER-SHARE-PRIVATE")
            await expect(
                bob.get_by_test_id("multi-session-shared-history-message")
            ).to_have_count(2)
            checks.append("Refresh does not expand fixed history after host append")
            await alice.get_by_test_id("multi-session-sharing-revoke").click()
            await expect(
                alice.get_by_test_id("multi-session-sharing-managed-item")
            ).to_have_count(0)
            await bob.get_by_test_id("multi-session-shared-history-refresh").click()
            await expect(
                bob.get_by_test_id("multi-session-shared-history-error")
            ).to_be_visible()
            await expect(
                bob.get_by_test_id("multi-session-shared-history-message")
            ).to_have_count(0)
            await expect(viewer).not_to_contain_text("alice-PRIVATE")
            await bob.screenshot(
                path=str(root / "bob-revoked-history.png"), full_page=True
            )
            checks.append(
                "Alice revoked through UI; Bob refresh clears cached content and shows unavailable"
            )
            await bob.get_by_test_id("multi-session-shared-history-close").click()
            await bob.get_by_test_id("multi-session-open-shared-sessions").click()
            await expect(
                bob.get_by_test_id("multi-session-sharing-received-empty")
            ).to_be_visible()
            checks.append("Recipient inbox no longer contains revoked share")
            return {
                "status": "passed",
                "ui": "real Chrome full application; two isolated contexts",
                "browser_version": browser.version,
                "ui_checks": checks,
                "limitations": [
                    "No model request or real Provider/Team execution",
                    "Session lifecycle is preprovisioned by the host fixture",
                    "Unopened organization bootstrap RPCs remain explicitly denied",
                    "Project execute ACL does not grant tool/process resources",
                ],
                "dist_sha256": hashlib.sha256(
                    (dist / "index.html").read_bytes()
                ).hexdigest(),
            }
        finally:
            for actor, page in pages.items():
                try:
                    await page.screenshot(
                        path=str(root / f"{actor}-final.png"), full_page=True
                    )
                    (root / f"{actor}-visible-text.txt").write_text(
                        await page.locator("body").inner_text()
                    )
                except Exception:
                    pass
            (root / "browser-rpc.json").write_text(json.dumps(frames, indent=2))
            (root / "browser-errors.json").write_text(json.dumps(errors, indent=2))
            (root / "browser-checks.json").write_text(json.dumps(checks, indent=2))
            await browser.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--dist", required=True, type=Path)
    args = parser.parse_args()
    if (
        not args.root.is_absolute()
        or args.root.exists()
        or not (args.dist / "index.html").is_file()
    ):
        parser.error("new absolute root and built frontend dist are required")

    async def probe(**values):
        return await browser_probe(**values, dist=args.dist.resolve())

    asyncio.run(asyncio.wait_for(run(args.root, browser_probe=probe), timeout=240))
