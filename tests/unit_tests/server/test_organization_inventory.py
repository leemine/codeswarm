"""Real credential/sidecar inventory isolation, including threaded projections."""

from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.governance import organization_auth, session_boundary
from jiuwenswarm.server.runtime.gateway_adapter.project_adapter import ProjectAdapter
from jiuwenswarm.server.runtime.gateway_adapter.session_adapter import SessionAdapter
from jiuwenswarm.server.runtime.session import (
    lifecycle,
    project_store,
    session_history,
    session_metadata,
)
from jiuwenswarm.server.runtime.session.project_access import ProjectAccessStore
from jiuwenswarm.server.ws_send import send_wire_payload


@pytest.fixture
def inventory(tmp_path, monkeypatch):
    tokens = {actor: secrets.token_urlsafe(32) for actor in ("alice", "bob")}
    config = tmp_path / "organization.json"
    config.write_text(
        json.dumps(
            {
                "authority": "organization:inventory",
                "signing_key": secrets.token_hex(32),
                "credentials": [
                    {
                        "actor_id": actor,
                        "sha256": hashlib.sha256(token.encode()).hexdigest(),
                        "expires_at": time.time() + 3600,
                        "revoked": False,
                    }
                    for actor, token in tokens.items()
                ],
            }
        )
    )
    config.chmod(0o600)
    monkeypatch.setenv(organization_auth.CONFIG_ENV, str(config))
    auth = organization_auth.configured_authenticator()
    principals = {
        actor: auth.principal({"Authorization": "Bearer " + token})
        for actor, token in tokens.items()
    }
    root = tmp_path / "agent"
    root.mkdir()
    sessions = root / "sessions"
    sessions.mkdir()
    monkeypatch.setattr(project_store, "get_agent_root_dir", lambda: root)
    for module in (session_metadata, session_history, lifecycle):
        monkeypatch.setattr(module, "get_agent_sessions_dir", lambda: sessions)
    project_store.invalidate_cache()
    session_metadata._METADATA_CACHE.clear()
    session_metadata._METADATA_CACHE_GENERATIONS.clear()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    project = project_store.create_project("shared-project", str(workspace))
    access = ProjectAccessStore()
    access.initialize(project.project_id, "admin")
    access.replace_acl(
        project.project_id,
        "admin",
        acl={"alice": ["read", "admin"], "bob": ["read", "admin"]},
        expected_revision=1,
    )
    host = session_boundary.organization_sharing_host()
    for sid, owner, pinned in [
        ("alice-one", "alice", False),
        ("alice-pinned", "alice", True),
        ("bob-one", "bob", False),
        ("legacy-unknown", None, False),
    ]:
        folder = sessions / sid
        folder.mkdir()
        (folder / "metadata.json").write_text(
            json.dumps(
                {
                    "session_id": sid,
                    "project_id": project.project_id,
                    "project_dir": str(workspace),
                    "title": f"{sid} private title",
                    "user_id": "bob" if owner == "alice" else "alice",
                    "channel_id": "web",
                    "work_mode": "work",
                    "mode": "agent",
                    "pinned": pinned,
                    "created_at": 1,
                    "last_message_at": 4 if owner == "alice" else 2,
                    "message_count": 1,
                }
            )
        )
        (folder / "history.jsonl").write_text(
            json.dumps({"role": "user", "content": f"{sid} private preview"}) + "\n"
        )
        if owner:
            host.register_owner_and_source(
                sid, principals[owner].identity(), project.project_id
            )
    yield SimpleNamespace(
        auth=auth,
        tokens=tokens,
        principals=principals,
        host=host,
        access=access,
        project_id=project.project_id,
        sessions=sessions,
    )
    project_store.invalidate_cache()
    session_metadata._METADATA_CACHE.clear()
    session_metadata._METADATA_CACHE_GENERATIONS.clear()


def request(method, params=None):
    return AgentRequest(
        request_id="inventory-request",
        channel_id="web",
        req_method=ReqMethod(method),
        params=params or {},
        user_id="forged-routing-user",
    )


async def dispatch(method, params, inventory):
    session_boundary.admit_session_request(
        method,
        params,
        identity_resolver=organization_auth.current_identity,
        host=inventory.host,
    )
    adapter = (
        ProjectAdapter(identity_resolver=lambda _: organization_auth.current_identity())
        if method.startswith("project.")
        else SessionAdapter()
    )
    return await adapter.handle(request(method, params))


@pytest.mark.asyncio
async def test_two_http_credential_sessions_adapter_inventory_counts_and_previews(
    inventory,
):
    # Production auth middleware and adapters composed in an ASGI test harness;
    # this is not a claim that a test-only RPC endpoint is a production API.
    from jiuwenswarm.gateway.channel_manager.web.organization_auth_http import (
        register_organization_auth,
    )

    app = FastAPI()
    register_organization_auth(app)

    @app.post("/inventory-test")
    async def endpoint(req: Request):
        body = await req.json()
        try:
            result = await dispatch(body["method"], body.get("params", {}), inventory)
            return {"ok": result.ok, "payload": result.payload}
        except PermissionError:
            return JSONResponse({"code": "FORBIDDEN"}, status_code=403)

    async with (
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as alice,
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as bob,
    ):
        for client, actor in ((alice, "alice"), (bob, "bob")):
            login = await client.post(
                "/api/v1/auth/organization/login",
                json={"token": inventory.tokens[actor]},
                headers={"X-Jiuwen-Auth": "1"},
            )
            assert login.status_code == 200

        async def call(client, method, params=None):
            return await client.post(
                "/inventory-test",
                json={"method": method, "params": params or {}},
                headers={"X-User-Id": "alice"},
            )

        for client, actor, count in ((alice, "alice", 2), (bob, "bob", 1)):
            listing = (
                await call(client, "session.list", {"limit": 1, "user_id": "alice"})
            ).json()["payload"]
            assert listing["total"] == count and len(listing["sessions"]) == 1
            assert all(
                row["session_id"].startswith(actor) for row in listing["sessions"]
            )
            projects = (await call(client, "project.list")).json()["payload"][
                "projects"
            ]
            shared = next(
                row for row in projects if row["project_id"] == inventory.project_id
            )
            assert (
                shared["session_count"] == 1
            )  # pinned excluded by existing projection
            sessions = (
                await call(
                    client, "project.get_sessions", {"project_id": inventory.project_id}
                )
            ).json()["payload"]
            assert (
                sessions["total"] == 1
                and sessions["sessions"][0]["session_id"] == actor + "-one"
            )
            pinned = (await call(client, "project.pinned_sessions")).json()["payload"][
                "sessions"
            ]
            assert [row["session_id"] for row in pinned] == (
                ["alice-pinned"] if actor == "alice" else []
            )
            own = (
                await call(client, "session.preview", {"session_id": actor + "-one"})
            ).json()["payload"]
            assert (
                own["preview_messages"][0]["content"] == actor + "-one private preview"
            )
            foreign = "bob-one" if actor == "alice" else "alice-one"
            assert (
                await call(
                    client, "session.preview", {"session_id": foreign, "user_id": actor}
                )
            ).status_code == 403
            assert (
                await call(client, "session.preview", {"session_id": "legacy-unknown"})
            ).status_code == 403


def test_unowned_metadata_never_enters_projection_and_os_directory_override_rejected(
    inventory, monkeypatch
):
    original = session_metadata._read_metadata
    reads = []

    def read(sid, *args, **kwargs):
        reads.append(sid)
        return original(sid, *args, **kwargs)

    monkeypatch.setattr(session_metadata, "_read_metadata", read)
    with organization_auth.authenticated_scope(inventory.principals["bob"]):
        rows, total = session_metadata.get_all_sessions_metadata()
        assert total == 1 and [r["session_id"] for r in rows] == ["bob-one"]
        assert [
            r["session_id"] for r in session_metadata.collect_all_sessions_metadata()
        ] == ["bob-one"]
        assert session_metadata.collect_all_sessions_metadata(user_id="alice") == []
        assert (
            session_metadata.collect_all_sessions_metadata(user_id="../../alice") == []
        )
    assert set(reads) == {"bob-one"}


@pytest.mark.asyncio
async def test_threaded_overlapping_scans_preserve_distinct_authenticated_identity(
    inventory, monkeypatch
):
    original = session_metadata._collect_all_sessions_metadata
    barrier = threading.Barrier(2, timeout=3)
    observed = []

    def scan(user_id=None):
        observed.append(organization_auth.current_identity().actor_id)
        barrier.wait()
        return original(user_id)

    monkeypatch.setattr(session_metadata, "_collect_all_sessions_metadata", scan)

    async def run(actor):
        with organization_auth.authenticated_scope(inventory.principals[actor]):
            return await asyncio.to_thread(
                session_metadata.collect_all_sessions_metadata
            )

    alice, bob = await asyncio.gather(run("alice"), run("bob"))
    assert set(observed) == {"alice", "bob"}
    assert {r["session_id"] for r in alice} == {"alice-one", "alice-pinned"}
    assert {r["session_id"] for r in bob} == {"bob-one"}
    assert not session_metadata._COLLECT_INFLIGHT


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["session.list", "project.list"])
@pytest.mark.parametrize("revocation", ["acl", "source", "credential"])
async def test_buffered_inventory_is_denied_after_current_authority_changes(
    inventory, method, revocation
):
    with organization_auth.authenticated_scope(inventory.principals["alice"]):
        permit = session_boundary.admit_session_request(
            method,
            {},
            identity_resolver=organization_auth.current_identity,
            host=inventory.host,
        )
        result = await dispatch(method, {}, inventory)
        assert result.ok
        if revocation == "acl":
            inventory.access.replace_acl(
                inventory.project_id,
                "admin",
                acl={"bob": ["read", "admin"]},
                expected_revision=2,
            )
        elif revocation == "source":
            inventory.host.invalidate_source(
                "alice-one", expected_epoch=inventory.host.source_epoch("alice-one")
            )
        else:
            inventory.auth.revoke(inventory.principals["alice"])
        ws = SimpleNamespace(send=AsyncMock())
        with session_boundary.delivery_scope():
            session_boundary.set_delivery_permit(permit)
            await send_wire_payload(
                ws,
                {
                    "request_id": "inventory-request",
                    "channel": "web",
                    "payload": result.payload,
                },
            )
        wire = json.loads(ws.send.call_args.args[0])
        assert "FORBIDDEN" in json.dumps(wire) and "private title" not in json.dumps(
            wire
        )
        assert '"projects"' not in json.dumps(wire) and '"sessions"' not in json.dumps(
            wire
        )


@pytest.mark.asyncio
async def test_new_inventory_request_cannot_join_pre_revocation_scan(
    inventory, monkeypatch
):
    """A new permit must not bless results of an older same-identity Future."""
    ready = threading.Event()
    release = threading.Event()
    joined = threading.Event()
    original = session_metadata._collect_all_sessions_metadata
    first = True

    def scan(user_id=None):
        nonlocal first
        result = original(user_id)
        if first:
            first = False
            ready.set()
            assert release.wait(3)
        return result

    monkeypatch.setattr(session_metadata, "_collect_all_sessions_metadata", scan)
    with organization_auth.authenticated_scope(inventory.principals["alice"]):
        old_task = asyncio.create_task(
            asyncio.to_thread(session_metadata.collect_all_sessions_metadata)
        )
        try:
            assert await asyncio.to_thread(ready.wait, 3)
            pending = next(iter(session_metadata._COLLECT_INFLIGHT.values()))
            original_result = pending.result

            def result(*args, **kwargs):
                joined.set()
                return original_result(*args, **kwargs)

            monkeypatch.setattr(pending, "result", result)
            inventory.host.invalidate_source(
                "alice-one", expected_epoch=inventory.host.source_epoch("alice-one")
            )
            permit = session_boundary.admit_session_request(
                "project.list",
                {},
                identity_resolver=organization_auth.current_identity,
                host=inventory.host,
            )
            fresh_task = asyncio.create_task(dispatch("project.list", {}, inventory))
            # An implementation may avoid joining by keying by authority version.
            for _ in range(100):
                if joined.is_set() or fresh_task.done():
                    break
                await asyncio.sleep(0.005)
            release.set()
            fresh = await fresh_task
            await old_task
            ws = SimpleNamespace(send=AsyncMock())
            with session_boundary.delivery_scope():
                session_boundary.set_delivery_permit(permit)
                await send_wire_payload(
                    ws,
                    {"request_id": "fresh", "channel": "web", "payload": fresh.payload},
                )
            sent = json.loads(ws.send.call_args.args[0])
            if "FORBIDDEN" in json.dumps(sent):
                return
            shared = next(
                row
                for row in sent["payload"]["projects"]
                if row["project_id"] == inventory.project_id
            )
            # The newly authorized scan may retain the owner's cleanup target,
            # but must replace the old readable row with the minimal DTO.
            assert shared["session_count"] == 1
            assert "private title" not in json.dumps(sent)
            rows = session_boundary.filter_current_inventory(pending.result())
            cleanup = next(row for row in rows if row["session_id"] == "alice-one")
            assert cleanup["cleanup_only"] is True and cleanup["title"] == ""
            assert cleanup["project_dir"] == "" and cleanup["message_count"] == 0
        finally:
            release.set()
            await old_task


@pytest.mark.asyncio
async def test_revoked_source_retains_only_original_owner_cleanup_inventory(inventory):
    from jiuwenswarm.governance.session_sharing import SessionSharingDenied

    inventory.host.invalidate_source(
        "alice-one", expected_epoch=inventory.host.source_epoch("alice-one")
    )
    with organization_auth.authenticated_scope(inventory.principals["alice"]):
        assert not inventory.host.owner_current("alice-one", organization_auth.current_identity())
        result = await dispatch("project.get_sessions", {"project_id": inventory.project_id}, inventory)
        assert result.ok and result.payload["total"] == 1
        row = result.payload["sessions"][0]
        assert row == {
            "session_id": "alice-one", "project_id": inventory.project_id,
            "mode": "agent", "work_mode": "work", "cleanup_only": True,
            "title": "", "project_dir": "", "message_count": 0,
            "created_at": 0, "last_message_at": 0, "pinned": False,
        }
        for method in ("history.get", "session.switch", "session.get_metadata", "chat.send"):
            with pytest.raises(SessionSharingDenied):
                session_boundary.admit_session_request(
                    method, {"session_id": "alice-one"},
                    identity_resolver=organization_auth.current_identity, host=inventory.host,
                )
        permit = session_boundary.admit_session_request(
            "session.delete", {"session_id": "alice-one"},
            identity_resolver=organization_auth.current_identity, host=inventory.host,
        )
        assert permit.revalidate()
    with organization_auth.authenticated_scope(inventory.principals["bob"]):
        result = await dispatch("project.get_sessions", {"project_id": inventory.project_id}, inventory)
        assert [row["session_id"] for row in result.payload["sessions"]] == ["bob-one"]


def test_stale_full_inventory_is_rebuilt_as_minimal_cleanup_after_revoke(inventory):
    with organization_auth.authenticated_scope(inventory.principals["alice"]):
        rows = session_metadata.collect_all_sessions_metadata()
        old = next(row for row in rows if row["session_id"] == "alice-one")
        assert old["title"] == "alice-one private title"
        inventory.host.invalidate_source(
            "alice-one", expected_epoch=inventory.host.source_epoch("alice-one")
        )
        filtered = session_boundary.filter_current_inventory([old])
        assert len(filtered) == 1 and filtered[0]["cleanup_only"] is True
        assert "private title" not in json.dumps(filtered)
        assert "project_dir" in filtered[0] and filtered[0]["project_dir"] == ""
        inventory.auth.revoke(inventory.principals["alice"])
        with pytest.raises(PermissionError):
            session_boundary.filter_current_inventory(filtered)


@pytest.mark.parametrize("change", ["team", "ephemeral", "deleted", "unknown"])
def test_cleanup_inventory_never_invents_unsupported_or_foreign_ownership(inventory, change):
    inventory.host.invalidate_source(
        "alice-one", expected_epoch=inventory.host.source_epoch("alice-one")
    )
    metadata = inventory.sessions / "alice-one" / "metadata.json"
    value = json.loads(metadata.read_text())
    if change == "team":
        value["mode"] = "team.work.normal"
    elif change == "ephemeral":
        value["ephemeral"] = True
    elif change == "deleted":
        metadata.unlink()
    else:
        metadata = inventory.sessions / "legacy-unknown" / "metadata.json"
    if change in {"team", "ephemeral"}:
        metadata.write_text(json.dumps(value))
    sid = "legacy-unknown" if change == "unknown" else "alice-one"
    with organization_auth.authenticated_scope(inventory.principals["alice"]):
        assert session_boundary.cleanup_inventory_entry(
            inventory.host, organization_auth.current_identity(), sid
        ) is None
