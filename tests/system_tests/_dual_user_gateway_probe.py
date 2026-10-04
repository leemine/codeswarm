"""Opt-in real local Gateway/AgentServer two-principal boundary probe.

Run with --root <new absolute private directory>. Uses provisioned fixture data,
real HTTP login and WS RPCs; no Provider/model or browser UI is exercised. The
transparent relay may hold an already produced response to test queued delivery.
"""

from __future__ import annotations

import argparse
import asyncio
from contextlib import AsyncExitStack
import hashlib
import json
import os
from pathlib import Path
import secrets
import signal
import subprocess
import importlib.metadata
import socket
import sys
import time


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def run(root, *, browser_probe=None):
    root.mkdir(mode=0o700)
    repo = Path(__file__).resolve().parents[2]
    data = root / "data"
    auth_path = root / "organization.json"
    tokens = {actor: secrets.token_urlsafe(40) for actor in ("alice", "bob")}
    auth_path.write_text(
        json.dumps(
            {
                "authority": "organization:real-local-fixture",
                "signing_key": secrets.token_hex(32),
                "credentials": [
                    {
                        "actor_id": actor,
                        "sha256": hashlib.sha256(token.encode()).hexdigest(),
                        "expires_at": time.time() + 1200,
                        "revoked": False,
                    }
                    for actor, token in tokens.items()
                ],
            }
        )
    )
    auth_path.chmod(0o600)
    os.environ.update(
        JIUWENSWARM_DATA_DIR=str(data),
        JIUWENSWARM_CONFIG_DIR=str(data / "config"),
        JIUWENSWARM_ORGANIZATION_AUTH_FILE=str(auth_path),
        JIUWENSWARM_CONFIG_URL="off",
    )
    # Provisioning only. All behavior under test uses the actual network boundary.
    from jiuwenswarm.common.utils import prepare_workspace, get_agent_sessions_dir
    from jiuwenswarm.server.runtime.session import project_store
    from jiuwenswarm.server.runtime.session.project_access import ProjectAccessStore
    from jiuwenswarm.governance.organization_auth import configured_authenticator
    from jiuwenswarm.governance.session_boundary import organization_sharing_host

    prepare_workspace(overwrite=False, workspace_dir=data)
    workspace = root / "workspace"
    workspace.mkdir()
    project = project_store.create_project("shared-project", str(workspace))
    access = ProjectAccessStore()
    access.initialize(project.project_id, "alice")
    access.replace_acl(
        project.project_id,
        "alice",
        acl={
            "alice": ["read", "execute", "admin"],
            "bob": ["read", "execute", "admin"],
        },
        expected_revision=1,
    )
    auth = configured_authenticator()
    host = organization_sharing_host()
    sessions = get_agent_sessions_dir()
    for actor in ("alice", "bob"):
        sid = actor + "-private"
        folder = sessions / sid
        folder.mkdir(parents=True)
        (folder / "metadata.json").write_text(
            json.dumps(
                {
                    "session_id": sid,
                    "project_id": project.project_id,
                    "project_dir": str(workspace),
                    "title": actor + "-PRIVATE-TITLE",
                    "user_id": "forged-other",
                    "channel_id": "web",
                    "work_mode": "work",
                    "mode": "agent",
                    "created_at": 1,
                    "last_message_at": 2,
                    "message_count": 2,
                }
            )
        )
        (folder / "history.jsonl").write_text(
            "".join(
                json.dumps(
                    {
                        "id": f"{actor}-{i}",
                        "role": "user",
                        "content": f"{actor}-PRIVATE-{i}",
                    }
                )
                + "\n"
                for i in range(2)
            )
        )
        principal = auth.principal({"Authorization": "Bearer " + tokens[actor]})
        host.register_owner_and_source(sid, principal.identity(), project.project_id)

    import httpx
    from websockets.legacy.client import connect
    from websockets.legacy.server import serve

    agent_port, web_port, gateway_port = free_port(), free_port(), free_port()
    env = os.environ.copy()
    env.update(
        HOME=str(root / "home"),
        JIUWENSWARM_RUNTIME_WORKSPACE_READY="1",
        JIUWENSWARM_AGENT_PREWARM="0",
        HEALTH_CHECK_INTERVAL="3600",
        AGENT_SERVER_HOST="127.0.0.1",
        AGENT_SERVER_PORT=str(agent_port),
        GATEWAY_HOST="127.0.0.1",
        GATEWAY_PORT=str(gateway_port),
        WEB_HOST="127.0.0.1",
        WEB_PORT=str(web_port),
        NO_PROXY="127.0.0.1,localhost",
    )
    children, traces, observations = [], [], []
    hold_id = None
    held, release = asyncio.Event(), asyncio.Event()
    queued = {}

    async def relay(gateway, _path):
        async with connect(
            f"ws://127.0.0.1:{agent_port}",
            max_size=16 * 1024 * 1024,
            extra_headers={
                "X-Jiuwen-Gateway-Assertion": gateway.request_headers[
                    "X-Jiuwen-Gateway-Assertion"
                ]
            },
        ) as agent:
            deliveries = set()

            async def send(raw, destination):
                frame = json.loads(raw)
                if hold_id is not None and frame.get("request_id") == hold_id:
                    queued["frame"] = frame
                    held.set()
                    await release.wait()
                await destination.send(raw)
                if hold_id is not None and frame.get("request_id") == hold_id:
                    queued["forwarded"] = True

            async def up():
                async for raw in gateway:
                    frame = json.loads(raw)
                    traces.append(
                        {
                            "direction": "gateway-agent",
                            "id": frame.get("request_id"),
                            "method": frame.get("method"),
                            "assertion_actor": frame.get("_organization_assertion", {})
                            .get("claims", {})
                            .get("actor"),
                        }
                    )
                    await agent.send(raw)

            async def down():
                async for raw in agent:
                    frame = json.loads(raw)
                    traces.append(
                        {
                            "direction": "agent-gateway",
                            "id": frame.get("request_id"),
                            "ok": frame.get("ok"),
                        }
                    )
                    task = asyncio.create_task(send(raw, gateway))
                    deliveries.add(task)
                    task.add_done_callback(deliveries.discard)

            tasks = [asyncio.create_task(up()), asyncio.create_task(down())]
            try:
                await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for task in tasks + list(deliveries):
                    task.cancel()
                await asyncio.gather(*tasks, *deliveries, return_exceptions=True)

    async def start(module, port, values):
        log = (root / (module.rsplit(".", 1)[-1] + ".log")).open("wb")
        try:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                module,
                "--port",
                str(port),
                cwd=repo,
                env=values,
                stdout=log,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
            )
        finally:
            log.close()
        children.append(process)
        return process

    async def wait_log(name, marker):
        async with asyncio.timeout(75):
            while marker not in (root / name).read_text(errors="replace"):
                assert all(child.returncode is None for child in children), (
                    "service exited; inspect owned logs"
                )
                await asyncio.sleep(0.2)

    counter = 0
    forbidden_delivery_ids = set()

    async def rpc(ws, method, params, *, request_id=None, timeout=15):
        nonlocal counter
        counter += 1
        rid = request_id or f"probe-{counter}"
        await ws.send(
            json.dumps({"type": "req", "id": rid, "method": method, "params": params})
        )
        async with asyncio.timeout(timeout):
            while True:
                frame = json.loads(await ws.recv())
                if (
                    frame.get("id") in forbidden_delivery_ids
                    and frame.get("ok") is True
                ):
                    raise AssertionError("revoked queued response reached client")
                if frame.get("id") == rid and frame.get("type") == "res":
                    observations.append({"method": method, "id": rid, "frame": frame})
                    return frame

    result = {
        "scope": "real local HTTP + Gateway WS + AgentServer WS; fixture-provisioned history",
        "source_sha": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=repo, text=True
        ).strip(),
        "core_direct_url": json.loads(
            importlib.metadata.distribution("openjiuwen").read_text("direct_url.json")
        ),
        "provider": "not run",
        "ui": "not run",
    }
    try:
        await start("jiuwenswarm.server.app_agentserver", agent_port, env)
        await wait_log("app_agentserver.log", "ready:")
        async with AsyncExitStack() as stack:
            listener = await stack.enter_async_context(serve(relay, "127.0.0.1", 0))
            gateway_env = dict(
                env, AGENT_SERVER_PORT=str(listener.sockets[0].getsockname()[1])
            )
            await start("jiuwenswarm.gateway.app_gateway", web_port, gateway_env)
            await wait_log("app_gateway.log", "startup stage=web_channel_listening")
            if browser_probe is not None:
                result["ui"] = "real browser probe started"
                result.update(
                    await browser_probe(
                        root=root,
                        repo=repo,
                        web_port=web_port,
                        tokens=tokens,
                        sessions=sessions,
                        env=env,
                        children=children,
                    )
                )
                return
            clients = {
                actor: await stack.enter_async_context(
                    httpx.AsyncClient(
                        base_url=f"http://127.0.0.1:{web_port}",
                        timeout=10,
                        trust_env=False,
                    )
                )
                for actor in tokens
            }
            websockets = {}
            for actor, client in clients.items():
                response = await client.post(
                    "/api/v1/auth/organization/login",
                    json={"token": tokens[actor]},
                    headers={"x-jiuwen-auth": "1"},
                )
                assert (
                    response.status_code == 200 and response.json()["actor_id"] == actor
                ), response.text
                status = await client.get("/api/v1/auth/organization/status")
                assert status.json()["actor_id"] == actor
                cookie = "; ".join(f"{k}={v}" for k, v in client.cookies.items())
                websockets[actor] = await stack.enter_async_context(
                    connect(
                        f"ws://127.0.0.1:{web_port}/ws",
                        extra_headers={"Cookie": cookie},
                    )
                )
            alice, bob = websockets["alice"], websockets["bob"]
            for actor, ws in websockets.items():
                frame = await rpc(ws, "session.list", {})
                assert frame.get("ok") is True, frame
                encoded = json.dumps(frame)
                assert (
                    actor + "-PRIVATE-TITLE" in encoded
                    and ("bob" if actor == "alice" else "alice") + "-PRIVATE-TITLE"
                    not in encoded
                ), frame
            denied = await rpc(
                bob, "history.get", {"session_id": "alice-private", "user_id": "alice"}
            )
            assert (
                denied.get("ok") is False
                and denied.get("code") == "FORBIDDEN"
                and "alice-PRIVATE" not in json.dumps(denied)
            ), denied
            created = await rpc(
                alice,
                "session.share.create",
                {
                    "session_id": "alice-private",
                    "target_actor": "bob",
                    "actions": ["view"],
                    "history_scope": "current_snapshot",
                    "expires_at": time.time() + 600,
                },
            )
            assert created.get("ok") is True, created
            share = created["payload"]["share"]
            params = {
                "session_id": "alice-private",
                "share_id": share["share_id"],
                "limit": 1,
            }
            page = await rpc(bob, "session.share.history.get", params)
            assert page.get("ok") is True and "alice-PRIVATE-1" in json.dumps(page), (
                page
            )
            with (sessions / "alice-private/history.jsonl").open("a") as stream:
                stream.write(
                    json.dumps({"role": "user", "content": "AFTER-SHARE-PRIVATE"})
                    + "\n"
                )
            frozen = await rpc(
                bob, "session.share.history.get", dict(params, limit=100)
            )
            assert frozen.get("ok") is True and "AFTER-SHARE-PRIVATE" not in json.dumps(
                frozen
            ), frozen
            for method in ("chat.send", "session.switch", "history.get"):
                denied = await rpc(
                    bob,
                    method,
                    {"session_id": "alice-private", "share_id": share["share_id"]},
                )
                assert (
                    denied.get("ok") is False and denied.get("code") == "FORBIDDEN"
                ), denied
            revoked = await rpc(
                alice,
                "session.share.revoke",
                {
                    "session_id": "alice-private",
                    "share_id": share["share_id"],
                    "expected_revision": share["revision"],
                },
            )
            assert revoked.get("ok") is True, revoked
            denied = await rpc(bob, "session.share.history.get", params)
            assert (
                denied.get("ok") is False
                and denied.get("code") == "FORBIDDEN"
                and "alice-PRIVATE" not in json.dumps(denied)
            ), denied
            cookie = "; ".join(f"{k}={v}" for k, v in clients["bob"].cookies.items())
            async with connect(
                f"ws://127.0.0.1:{web_port}/ws", extra_headers={"Cookie": cookie}
            ) as reconnected:
                denied = await rpc(reconnected, "session.share.history.get", params)
                assert denied.get("code") == "FORBIDDEN", denied
                listed = await rpc(reconnected, "session.share.list", {})
                assert listed.get("ok") is True and listed["payload"]["shares"] == [], (
                    listed
                )
            for path in ("/file-api/future-alias", "/api/trajectory/alice-private"):
                response = await clients["bob"].get(
                    path,
                    params={
                        "session_id": "alice-private",
                        "share_id": share["share_id"],
                    },
                )
                assert response.status_code in (403, 404), (path, response.status_code)
                result.setdefault("http_surfaces", []).append(
                    {
                        "path": path,
                        "status": response.status_code,
                        "verified": "denied"
                        if response.status_code == 403
                        else "route not mounted",
                    }
                )
                if response.status_code == 403:
                    assert response.json()["code"] == "ORGANIZATION_AUTHORITY_REQUIRED"
            # Hold an actual successful AgentServer response in transit. Other
            # requests continue through the unmodified relay and signed RPCs.
            created = await rpc(
                alice,
                "session.share.create",
                {
                    "session_id": "alice-private",
                    "target_actor": "bob",
                    "actions": ["view"],
                    "history_scope": "current_snapshot",
                    "expires_at": time.time() + 600,
                },
            )
            assert created.get("ok") is True, created
            queued_share = created["payload"]["share"]
            hold_id = "queued-shared-history"
            queued_task = asyncio.create_task(
                rpc(
                    bob,
                    "session.share.history.get",
                    {
                        "session_id": "alice-private",
                        "share_id": queued_share["share_id"],
                    },
                    request_id=hold_id,
                    timeout=5,
                )
            )
            await asyncio.wait_for(held.wait(), 15)
            assert "alice-PRIVATE" in json.dumps(queued["frame"]), queued
            revoked = await rpc(
                alice,
                "session.share.revoke",
                {
                    "session_id": "alice-private",
                    "share_id": queued_share["share_id"],
                    "expected_revision": queued_share["revision"],
                },
            )
            assert revoked.get("ok") is True, revoked
            forbidden_delivery_ids.add(hold_id)
            release.set()
            try:
                queued_response = await queued_task
                result["queued_share_denied"] = queued_response.get(
                    "ok"
                ) is False and "alice-PRIVATE" not in json.dumps(queued_response)
            except TimeoutError:
                assert queued.get("forwarded") is True
                fresh = await rpc(
                    bob,
                    "session.share.history.get",
                    {
                        "session_id": "alice-private",
                        "share_id": queued_share["share_id"],
                    },
                )
                assert fresh.get("code") == "FORBIDDEN", fresh
                result["queued_share_denied"] = True
                result["queued_share_outcome"] = (
                    "silently dropped; subsequent live request FORBIDDEN"
                )
            # Complete independent credential revocation checks even if the
            # share queue reveals a product defect.
            hold_id = "queued-inventory"
            queued["forwarded"] = False
            held.clear()
            release.clear()
            queued_task = asyncio.create_task(
                rpc(bob, "session.list", {}, request_id=hold_id, timeout=5)
            )
            await asyncio.wait_for(held.wait(), 15)
            assert "bob-PRIVATE-TITLE" in json.dumps(queued["frame"]), queued
            logout = await clients["bob"].post(
                "/api/v1/auth/organization/logout", headers={"x-jiuwen-auth": "1"}
            )
            assert logout.status_code == 200, logout.text
            forbidden_delivery_ids.add(hold_id)
            release.set()
            try:
                queued_response = await queued_task
                result["queued_credential_denied"] = queued_response.get(
                    "ok"
                ) is False and "bob-PRIVATE" not in json.dumps(queued_response)
            except TimeoutError:
                assert queued.get("forwarded") is True
                await bob.send(
                    json.dumps(
                        {
                            "type": "req",
                            "id": "after-logout",
                            "method": "session.list",
                            "params": {},
                        }
                    )
                )
                from websockets.exceptions import ConnectionClosed

                try:
                    async with asyncio.timeout(5):
                        raw = await bob.recv()
                    raise AssertionError("revoked socket delivered: " + raw)
                except ConnectionClosed as exc:
                    result["queued_credential_denied"] = True
                    result["revoked_connection_close_code"] = exc.code
            except Exception as exc:
                from websockets.exceptions import ConnectionClosed

                if not isinstance(exc, ConnectionClosed):
                    raise
                result["queued_credential_denied"] = True
                result["revoked_connection_close_code"] = exc.code
            try:
                async with connect(
                    f"ws://127.0.0.1:{web_port}/ws", extra_headers={"Cookie": cookie}
                ):
                    raise AssertionError("revoked credential reconnected")
            except Exception as exc:
                from websockets.exceptions import InvalidStatusCode

                assert isinstance(exc, InvalidStatusCode) and exc.status_code in (
                    401,
                    403,
                ), repr(exc)
                result["revoked_reconnect_status"] = exc.status_code
            result["completed"] = [
                "two independent HTTP logins and simultaneous WebSockets",
                "private inventory isolation",
                "owner history denial",
                "fixed snapshot share",
                "execute and owner API denial",
                "share revoke denial and reconnect",
                "credential queued delivery denied and reconnect rejected",
            ]
            assert result["queued_share_denied"], (
                "revoked shared history was delivered from transport queue"
            )
            assert result["queued_credential_denied"]
            result["status"] = "passed"
    except Exception as exc:
        result.update(status="failed", error=type(exc).__name__, detail=str(exc))
        raise
    finally:
        release.set()
        cleanup = []
        for child in reversed(children):
            if child.returncode is None:
                child.terminate()
                try:
                    await asyncio.wait_for(child.wait(), 15)
                except TimeoutError:
                    os.killpg(child.pid, signal.SIGKILL)
                    await child.wait()
            cleanup.append({"pid": child.pid, "returncode": child.returncode})
        result.update(observations=observations, transport=traces, cleanup=cleanup)
        (root / "result.json").write_text(json.dumps(result, indent=2))
        print(
            json.dumps(
                {
                    k: v
                    for k, v in result.items()
                    if k not in {"observations", "transport"}
                },
                indent=2,
            )
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    if not args.root.is_absolute() or args.root.exists():
        parser.error("--root must be a new absolute directory")
    asyncio.run(asyncio.wait_for(run(args.root), timeout=180))
