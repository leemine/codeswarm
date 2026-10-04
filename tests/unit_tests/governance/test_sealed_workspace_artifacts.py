"""Real Native tool certificate + sidecar/asset/HMAC; synthetic model/push only."""


# Imported fixtures intentionally become this module's pytest fixture providers.
# ruff: noqa: F401, F811

import asyncio
import json
import threading
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from jiuwenswarm.agents.harness.common.tools import send_file_to_user as sends
from jiuwenswarm.agents.harness.common.tools.verified_download_assets import (
    VerifiedDownloadAssetOwner,
)
from jiuwenswarm.agents.harness.common.rails.permissions._auto_permission.artifact_authorization import (
    AutoPermissionArtifactAuthorizationMixin,
)
from jiuwenswarm.agents.harness.common.rails.permissions.tool_decision_facts import (
    build_tool_decision_facts,
)
from jiuwenswarm.agents.harness.common.rails.permissions.generated_artifact_delivery import (
    clear_send_file_execution_grant,
)
from jiuwenswarm.governance.resources import ResourceDefinition
from jiuwenswarm.governance.workspace_download import (
    WorkspaceDownloadPermit,
    WorkspaceDownloadDenied,
)
from tests.unit_tests.governance.test_artifact_authority_review import (
    credentials,
    setup,
    chain,
)  # noqa: F401

_original_consume = sends.SendFileToolkit._send_file_with_envelope


@pytest.fixture
async def sealed(chain, monkeypatch):  # noqa: F811
    c = chain
    sends._SENT_FILE_PATHS_BY_SESSION.clear()
    monkeypatch.setattr(
        sends.SendFileToolkit, "_send_file_with_envelope", _original_consume
    )
    owner = VerifiedDownloadAssetOwner(
        root=c.s.tmp_path / "sealed", start_sweeper=False
    )
    c.toolkit._require_execution_authorization = True
    c.toolkit._asset_owner = owner
    c.s.manager._asset_owner = owner
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.tools.verified_download_assets._SHARED_VERIFIED_DOWNLOAD_ASSET_OWNER",
        owner,
    )
    push = AsyncMock(return_value=True)
    monkeypatch.setattr(sends, "send_runtime_push", push)
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.session_history.append_history_record",
        MagicMock(),
    )

    def approve():
        facts = build_tool_decision_facts(
            "send_file_to_user",
            {"abs_file_path_list": str(c.s.file), "target_channels": ["web"]},
            workspace_root=c.s.root,
            original_args_were_valid_object=True,
            send_paths=(str(c.s.file),),
        )
        assert (
            AutoPermissionArtifactAuthorizationMixin._issue_send_file_authorization(
                facts
            )
            == ""
        )

    def capture(token=None):
        return WorkspaceDownloadPermit.capture(
            c.s.host,
            lambda: c.s.current[0](),
            "alice-session",
            token or push.await_args.args[0]["payload"]["files"][0]["download_token"],
            token_validator=c.s.manager.validate_token,
            asset_owner=owner,
        )

    c.asset_owner, c.push, c.approve, c.capture = owner, push, approve, capture
    try:
        yield c
    finally:
        clear_send_file_execution_grant()
        sends._SENT_FILE_PATHS_BY_SESSION.clear()
        owner.close()


@pytest.mark.asyncio
async def test_actual_native_approved_delivery_survives_completed_tool_and_original_change(
    sealed,
):
    c = sealed
    c.toolkit._user_id = "legacy-routing-not-an-authority"
    c.approve()
    c.s.file.write_bytes(b"delivery-time")
    result = await c.invoke()
    assert result[0][0] == "成功发送 1 个文件"
    item = c.push.await_args.args[0]["payload"]["files"][0]
    assert item["path"] != str(c.s.file)
    assert item["download_url"].startswith("/file-api/download?token=")
    from urllib.parse import parse_qs, urlsplit

    query = parse_qs(urlsplit(item["download_url"]).query)
    assert set(query) == {"token", "session_id"}
    assert query["session_id"] == ["alice-session"]
    c.s.file.write_bytes(b"changed-after-delivery")
    permit = c.capture()
    assert permit.read(0, 65536) == b"delivery-time"
    c.s.file.unlink()
    assert c.capture().read(0, 65536) == b"delivery-time"
    registration = json.loads(next(c.asset_owner.root.glob("*.json")).read_text())
    assert registration["state"] == "committed"
    assert registration["workspace_origin"]["original_path"] == str(c.s.file)
    assert c.s.tokens["alice"] not in json.dumps(registration)


@pytest.mark.asyncio
async def test_missing_approval_does_not_allocate_asset(sealed):
    c = sealed
    assert "grant_missing" in (await c.invoke())[0][0]
    assert not c.asset_owner.root.exists()
    c.push.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        "identity",
        "credential",
        "resource",
        "source",
        "retire",
        "sealed",
        "sidecar",
        "expiry",
    ],
)
async def test_sealed_current_authority_and_original_registration_are_required(
    sealed, change
):
    c = sealed
    c.approve()
    assert (await c.invoke())[0][0] == "成功发送 1 个文件"
    permit = c.capture()
    sidecar = next(c.asset_owner.root.glob("*.json"))
    data = json.loads(sidecar.read_text())
    if change == "identity":
        c.s.current[0] = c.s.bob.identity
    elif change == "credential":
        c.s.auth.revoke(c.s.alice)
    elif change == "resource":
        c.s.access.revoke_resource(
            c.s.project.project_id,
            c.s.alice.identity(),
            "workspace",
            subject_id="alice",
            expected_revision=3,
        )
    elif change == "source":
        c.s.host.invalidate_source("alice-session", expected_epoch=1)
    elif change == "retire":
        c.s.host.compensate_owner_registration(
            "alice-session", c.s.alice.identity(), expected_revision=1, expected_epoch=1
        )
    elif change == "sealed":
        Path(data["sealed_path"]).chmod(0o600)
        Path(data["sealed_path"]).write_bytes(b"x" * data["size_bytes"])
    elif change == "sidecar":
        data["workspace_origin"]["source"]["identity"]["actor_id"] = "bob"
        sidecar.write_text(json.dumps(data))
    elif change == "expiry":
        c.asset_owner._now_fn = lambda: data["expires_at"]
    with pytest.raises(WorkspaceDownloadDenied):
        permit.read(0, 4)
    with pytest.raises(WorkspaceDownloadDenied):
        c.capture()


@pytest.mark.asyncio
@pytest.mark.parametrize("result", [False, "error", "commit_error"])
async def test_delivery_failure_cleanup_and_uncertain_exposure_keep_original_ttl(
    sealed, monkeypatch, result
):
    c = sealed
    c.approve()
    if result is False:
        c.push.return_value = False
    elif result == "error":
        c.push.side_effect = RuntimeError("synthetic uncertain transport")
    else:
        monkeypatch.setattr(
            c.asset_owner,
            "commit",
            MagicMock(side_effect=OSError("synthetic commit failure")),
        )
    await c.invoke()
    assets = list(c.asset_owner.root.glob("*.json"))
    if result is False:
        assert not assets
        assert not list(c.asset_owner.root.iterdir())
    else:
        assert len(assets) == 1
        assert json.loads(assets[0].read_text())["state"] == "staged"
        assert c.capture().read(0, 7) == b"fixture"


@pytest.mark.asyncio
async def test_cancelled_stage_worker_is_drained_and_only_its_unexposed_asset_revoked(
    tmp_path,
):
    owner = VerifiedDownloadAssetOwner(root=tmp_path / "assets", start_sweeper=False)
    source = tmp_path / "source"
    source.write_bytes(b"synthetic")
    other = owner.stage(source, file_name="other", expires_at=9999999999)
    began, release = threading.Event(), threading.Event()

    def stage():
        began.set()
        assert release.wait(5)
        return owner.stage(source, file_name="own", expires_at=9999999999)

    task = asyncio.create_task(sends.SendFileToolkit._stage_owned_asset(owner, stage))
    await asyncio.wait_for(asyncio.to_thread(began.wait, 5), 6)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 6)
    assert list(owner.root.glob("*.json")) == [owner.root / f"{other.asset_id}.json"]
    owner.close()


@pytest.mark.asyncio
async def test_changed_source_during_same_fd_copy_has_no_registration_or_push(
    sealed, monkeypatch
):
    c = sealed
    c.approve()
    import os

    original = os.pread
    changed = False

    def pread(fd, limit, offset):
        nonlocal changed
        result = original(fd, limit, offset)
        if not changed:
            changed = True
            c.s.file.write_bytes(b"x" * c.s.file.stat().st_size)
        return result

    monkeypatch.setattr(os, "pread", pread)
    assert "失败" in (await c.invoke())[0][0]
    assert changed
    assert list(c.asset_owner.root.iterdir()) == []
    c.push.assert_not_awaited()


@pytest.mark.asyncio
async def test_original_workspace_symlink_escape_cannot_be_sealed(sealed):
    c = sealed
    c.approve()
    outside = c.s.tmp_path / "outside"
    outside.write_bytes(b"not authorized")
    c.s.file.unlink()
    c.s.file.symlink_to(outside)
    await c.invoke()
    c.push.assert_not_awaited()
    assert not c.asset_owner.root.exists()


@pytest.mark.asyncio
async def test_restart_same_asset_owner_keeps_binding_and_registration_snapshot_is_immutable(
    sealed,
):
    c = sealed
    c.approve()
    await c.invoke()
    token = c.push.await_args.args[0]["payload"]["files"][0]["download_token"]
    payload = c.s.manager.validate_token(token, session_id="alice-session")
    new = VerifiedDownloadAssetOwner(root=c.asset_owner.root, start_sweeper=False)
    new.prune()
    fingerprint = payload["workspace_artifact_v1"]["registration_digest"]
    snapshot = new.workspace_registration(payload["asset_id"], fingerprint)
    detached = snapshot.to_dict()
    detached["workspace_origin"]["source"]["identity"]["actor_id"] = "forged"
    assert (
        snapshot.to_dict()["workspace_origin"]["source"]["identity"]["actor_id"]
        == "alice"
    )
    from dataclasses import FrozenInstanceError

    with pytest.raises(FrozenInstanceError):
        snapshot._canonical_json = "{}"
    permit = WorkspaceDownloadPermit.capture(
        c.s.host,
        c.s.alice.identity,
        "alice-session",
        token,
        token_validator=c.s.manager.validate_token,
        asset_owner=new,
    )
    assert permit.read(0, 7) == b"fixture"
    new.close()


@pytest.mark.asyncio
async def test_legacy_asset_without_origin_does_not_enter_organization_permit(sealed):
    c = sealed
    import time

    asset = c.asset_owner.stage(
        c.s.file, file_name="legacy", expires_at=time.time() + 600
    )
    with pytest.raises(WorkspaceDownloadDenied):
        c.s.manager.generate_verified_asset_token(
            asset, file_name="legacy", session_id="alice-session"
        )
    token = c.s.manager._sign_payload(
        {
            "kind": "verified_asset_v1",
            "asset_id": asset.asset_id,
            "path": str(asset.sealed_path),
            "exp": asset.expires_at,
            "size": asset.size_bytes,
            "digest": asset.content_digest,
            "name": "legacy",
            "sid": "alice-session",
        }
    )
    with pytest.raises(WorkspaceDownloadDenied):
        c.capture(token)


from tests.unit_tests.governance.test_workspace_download_delivery import installed  # noqa: E402,F401


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method,range_header", [("GET", None), ("HEAD", None), ("GET", "bytes=2-8")]
)
async def test_sealed_actual_adapter_http_headers_and_bytes(
    sealed, installed, method, range_header
):  # noqa: F811
    import httpx

    c = sealed
    c.approve()
    assert (await c.invoke())[0][0] == "成功发送 1 个文件"
    item = c.push.await_args.args[0]["payload"]["files"][0]
    data = c.s.file.read_bytes()
    c.s.file.write_bytes(b"changed-after-publication")
    headers = {"Authorization": "Bearer " + c.s.tokens["alice"]}
    if range_header:
        headers["Range"] = range_header
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=installed.app), base_url="http://local"
    ) as client:
        response = await client.request(method, item["download_url"], headers=headers)
    assert response.status_code == (206 if range_header else 200)
    assert response.content == (
        b"" if method == "HEAD" else data[2:9] if range_header else data
    )
    assert installed.calls


@pytest.mark.asyncio
async def test_sealed_zip_skill_hint_uses_snapshot_fd(sealed):
    import zipfile

    c = sealed
    c.s.file = c.s.root / "package.zip"
    with zipfile.ZipFile(c.s.file, "w") as package:
        package.writestr("SKILL.md", "synthetic skill")
    c.approve()
    assert (await c.invoke())[0][0] == "成功发送 1 个文件"
    assert c.push.await_args.args[0]["payload"]["files"][0]["is_skill_package"] is True


@pytest.mark.asyncio
async def test_registration_save_failure_leaves_no_asset_or_temporary_file(
    sealed, monkeypatch
):
    import os

    c = sealed
    c.approve()
    original = os.replace

    def replace(source, target, *args, **kwargs):
        if str(target).endswith(".json"):
            raise OSError("synthetic registration persistence failure")
        return original(source, target, *args, **kwargs)

    monkeypatch.setattr(os, "replace", replace)
    assert "失败" in (await c.invoke())[0][0]
    assert list(c.asset_owner.root.iterdir()) == []
    c.push.assert_not_awaited()


@pytest.mark.asyncio
async def test_resource_restore_cannot_revive_original_token(sealed):
    c = sealed
    c.approve()
    await c.invoke()
    permit = c.capture()
    c.s.access.revoke_resource(
        c.s.project.project_id,
        c.s.alice.identity(),
        "workspace",
        subject_id="alice",
        expected_revision=3,
    )
    c.s.access.register_resource(
        c.s.project.project_id,
        ResourceDefinition("workspace", "workspace", str(c.s.root)),
        owner_subject_id="alice",
        actions=("read",),
        expected_revision=4,
        delegable=True,
    )
    with pytest.raises(WorkspaceDownloadDenied):
        permit.check()
    with pytest.raises(WorkspaceDownloadDenied):
        c.capture()


@pytest.mark.asyncio
async def test_source_authority_change_in_worker_cleans_unexposed_snapshot(
    sealed, monkeypatch
):
    c = sealed
    c.approve()
    original = c.asset_owner._stage_from_verified_fd

    def stage(fd, **kwargs):
        c.s.current[0] = c.s.bob.identity
        return original(fd, **kwargs)

    monkeypatch.setattr(c.asset_owner, "_stage_from_verified_fd", stage)
    assert "失败" in (await c.invoke())[0][0]
    c.push.assert_not_awaited()
    assert not c.asset_owner.root.exists() or list(c.asset_owner.root.iterdir()) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("ttl", [0, 3600])
async def test_governed_snapshot_rejects_invalid_lifetime_before_allocation(
    sealed, monkeypatch, ttl
):
    c = sealed
    c.approve()
    monkeypatch.setattr(sends, "_VERIFIED_ASSET_TTL_SECONDS", ttl)
    assert "失败" in (await c.invoke())[0][0]
    assert not c.asset_owner.root.exists()
    c.push.assert_not_awaited()


@pytest.mark.asyncio
async def test_actual_final_asgi_guard_drops_buffer_after_sealed_asset_change(
    sealed, installed
):  # noqa: F811
    import httpx
    from fastapi import FastAPI
    from jiuwenswarm.gateway.channel_manager.web.container_file_http import (
        attach_container_file_routes,
    )
    from jiuwenswarm.gateway.channel_manager.web.organization_auth_http import (
        register_organization_auth,
    )

    c = sealed
    c.approve()
    await c.invoke()
    item = c.push.await_args.args[0]["payload"]["files"][0]
    changed = []

    class ChangeBeforeSend:
        def __init__(self, app):
            self.app = app

        async def __call__(self, scope, receive, send):
            async def mutate(message):
                if (
                    message["type"] == "http.response.body"
                    and message.get("body")
                    and not changed
                ):
                    changed.append(True)
                    path = Path(item["path"])
                    path.chmod(0o600)
                    path.write_bytes(b"x" * path.stat().st_size)
                await send(message)

            await self.app(scope, receive, mutate)

    app = FastAPI()
    attach_container_file_routes(app, installed.channel)
    app.add_middleware(ChangeBeforeSend)
    register_organization_auth(app)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://local",
    ) as client:
        reply = await client.get(
            item["download_url"],
            headers={"Authorization": "Bearer " + c.s.tokens["alice"]},
        )
    assert changed == [True]
    assert reply.content == b""
    assert len(installed.calls) <= 1


async def _delivered_asset(c):
    from jiuwenswarm.agents.harness.common.tools.verified_download_assets import (
        VerifiedDownloadAsset,
        _registration_digest,
    )

    c.approve()
    assert "成功发送" in (await c.invoke())[0][0]
    payload = json.loads(next(c.asset_owner.root.glob("*.json")).read_text())
    return VerifiedDownloadAsset(
        payload["asset_id"],
        Path(payload["sealed_path"]),
        payload["expires_at"],
        payload["size_bytes"],
        payload["content_digest"],
        _registration_digest(payload),
        tuple(payload["asset_root"]),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["commit", "revoke", "prune", "restart_prune"])
async def test_governed_mutation_never_touches_replacement_root(sealed, operation):
    import shutil

    c = sealed
    asset = await _delivered_asset(c)
    root = c.asset_owner.root
    original = root.with_name("original-sealed")
    root.rename(original)
    shutil.copytree(original, root)
    before = {p.name: p.read_bytes() for p in root.iterdir()}
    if operation == "restart_prune":
        owner = VerifiedDownloadAssetOwner(root=root, start_sweeper=False)
        try:
            owner.prune(now=asset.expires_at + 1)
        finally:
            owner.close()
    elif operation == "prune":
        c.asset_owner.prune(now=asset.expires_at + 1)
    else:
        with pytest.raises(ValueError, match="root_changed"):
            getattr(c.asset_owner, operation)(asset)
    assert {p.name: p.read_bytes() for p in root.iterdir()} == before
    assert {p.name: p.read_bytes() for p in original.iterdir()} == before


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["commit", "revoke", "prune"])
async def test_governed_mutation_rejects_symlinked_ancestor(sealed, operation):
    c = sealed
    asset = await _delivered_asset(c)
    root = c.asset_owner.root
    parent = root.parent
    moved = parent.with_name(parent.name + "-moved")
    parent.rename(moved)
    parent.symlink_to(moved, target_is_directory=True)
    try:
        before = {p.name: p.read_bytes() for p in (moved / root.name).iterdir()}
        if operation == "prune":
            c.asset_owner.prune(now=asset.expires_at + 1)
        else:
            with pytest.raises(OSError):
                getattr(c.asset_owner, operation)(asset)
        assert {p.name: p.read_bytes() for p in (moved / root.name).iterdir()} == before
    finally:
        parent.unlink()
        moved.rename(parent)


@pytest.mark.asyncio
async def test_governed_commit_is_idempotent_and_expiry_prunes_owned_files(sealed):
    c = sealed
    asset = await _delivered_asset(c)
    c.asset_owner.commit(asset)
    assert c.capture().read(0, 65536)
    c.asset_owner.prune(now=asset.expires_at + 1)
    assert list(c.asset_owner.root.iterdir()) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["sealed_path", "expires_at", "asset_id"])
async def test_governed_prune_retains_unknown_registration_and_files(sealed, field):
    c = sealed
    asset = await _delivered_asset(c)
    sidecar = next(c.asset_owner.root.glob("*.json"))
    payload = json.loads(sidecar.read_text())
    payload[field] = None
    sidecar.write_text(json.dumps(payload))
    before = {p.name: p.read_bytes() for p in c.asset_owner.root.iterdir()}
    c.asset_owner.prune(now=asset.expires_at + 10000)
    assert {p.name: p.read_bytes() for p in c.asset_owner.root.iterdir()} == before
