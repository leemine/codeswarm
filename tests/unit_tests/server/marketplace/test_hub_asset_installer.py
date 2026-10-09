# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

from pathlib import Path

import pytest

from jiuwenswarm.server.runtime.marketplace.hub_asset_installer import (
    _find_package_root,
    install_hub_asset_package,
)
from jiuwenswarm.server.runtime.marketplace.hub_asset_port import (
    HubAssetDetail,
    HubResolvedDownload,
)
from jiuwenswarm.server.runtime.marketplace.hub_install_state import (
    HubInstallRecord,
    HubInstallStateStore,
)


def test_group_archive_root_allows_nested_expert_manifests(tmp_path):
    root = tmp_path / "group"
    member = root / "agents" / "leader"
    member.mkdir(parents=True)
    (root / "manifest.json").write_text('{"package_type":"agent_group"}')
    (member / "manifest.json").write_text('{"package_type":"agent_template"}')
    assert _find_package_root(tmp_path, "agent_group") == root


class _HubPort:
    async def query_asset(self, request) -> HubAssetDetail:
        return HubAssetDetail(
            kind=request.kind,
            asset_id=request.asset_id,
            package_name="runtime-package",
            version="1.2.3",
            display_name="Runtime Package",
            short_description="A package",
            detail_description="A package from Hub",
            icon_uri="https://example.test/icon.png",
            tags=(),
        )

    async def resolve_download(self, request) -> HubResolvedDownload:
        return HubResolvedDownload(
            kind=request.kind,
            asset_id=request.asset_id,
            package_name="runtime-package",
            version=request.version,
            download_url="https://example.test/package.zip",
            checksum_sha256="a" * 64,
        )


class _Downloader:
    async def download_and_extract(
        self, artifact: HubResolvedDownload, destination: Path
    ) -> None:
        package = destination / artifact.package_name
        package.mkdir(parents=True)
        (package / "manifest.json").write_text("{}", encoding="utf-8")
        (package / "payload.txt").write_text("from-hub", encoding="utf-8")


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,method", [
    ("plugin", "plugin_packages.install"),
    ("agent_template", "agent_templates.install"),
    ("agent_group", "agent_groups.install"),
])
@pytest.mark.parametrize("revoke_at", ["query", "resolve", "download", "validate"])
async def test_revoked_install_never_publishes_package_or_provenance(
    tmp_path, monkeypatch, kind, method, revoke_at,
):
    from jiuwenswarm.governance.application_boundary import admit_application_request
    from jiuwenswarm.governance.contracts import TrustedIdentity
    from jiuwenswarm.governance import organization_auth, session_boundary
    from jiuwenswarm.governance.session_sharing import SessionSharingDenied

    grants = {"extensions": ["manage"]}
    identity = TrustedIdentity("operator", "operator", "test:hub")
    permit = admit_application_request(method, {}, identity_resolver=lambda: identity,
                                       policy_supplier=lambda _: grants)
    monkeypatch.setattr(organization_auth, "configured_authenticator", lambda: object())
    stages = []

    def checkpoint(stage):
        stages.append(stage)
        if stage == revoke_at:
            grants.clear()

    class Port(_HubPort):
        async def query_asset(self, request):
            result = await super().query_asset(request)
            checkpoint("query")
            return result

        async def resolve_download(self, request):
            result = await super().resolve_download(request)
            checkpoint("resolve")
            return result

    class Downloader(_Downloader):
        async def download_and_extract(self, artifact, destination):
            await super().download_and_extract(artifact, destination)
            (destination / artifact.package_name / "manifest.json").write_text(
                '{"package_type":"' + kind + '"}')
            checkpoint("download")

    state = HubInstallStateStore(tmp_path / "state")
    destination = tmp_path / "packages"
    with session_boundary.delivery_scope():
        session_boundary.set_delivery_permit(permit)
        with pytest.raises(SessionSharingDenied):
            await install_hub_asset_package(
                kind=kind, asset_id="asset-uuid", destination_root=destination,
                state_store=state, package_name_validator=lambda value: value,
                package_validator=lambda *_: checkpoint("validate"),
                hub_port=Port(), downloader=Downloader(),
            )
    assert stages[-1] == revoke_at
    assert not (destination / "runtime-package").exists()
    assert not list(destination.glob("*.staging-*"))
    assert state.get("asset-uuid") is None


@pytest.mark.asyncio
async def test_install_commits_validated_package_and_provenance(tmp_path: Path) -> None:
    destination_root = tmp_path / "packages"
    state = HubInstallStateStore(tmp_path / "state")
    committed: list[str] = []

    def validate_package(root: Path, expected: str) -> None:
        assert (root / "manifest.json").is_file()
        assert expected == "runtime-package"

    result = await install_hub_asset_package(
        kind="plugin",
        asset_id="asset-uuid",
        destination_root=destination_root,
        state_store=state,
        package_name_validator=lambda value: value,
        package_validator=validate_package,
        on_committed=lambda record: committed.append(record.package_id),
        hub_port=_HubPort(),
        downloader=_Downloader(),
    )

    assert result.package_id == "runtime-package"
    assert result.destination == destination_root / "runtime-package"
    assert (result.destination / "payload.txt").read_text(encoding="utf-8") == (
        "from-hub"
    )
    assert committed == ["runtime-package"]
    assert state.get("asset-uuid") == result.record
    assert result.record.kind == "plugin"
    assert result.record.version == "1.2.3"
    assert result.record.checksum_sha256 == "a" * 64


@pytest.mark.asyncio
async def test_install_rolls_back_package_and_provenance_when_commit_hook_fails(
    tmp_path: Path,
) -> None:
    destination_root = tmp_path / "packages"
    state = HubInstallStateStore(tmp_path / "state")
    rolled_back: list[str] = []

    def fail_commit(_record) -> None:
        raise OSError("marketplace unavailable")

    with pytest.raises(OSError, match="marketplace unavailable"):
        await install_hub_asset_package(
            kind="agent_template",
            asset_id="asset-uuid",
            destination_root=destination_root,
            state_store=state,
            package_name_validator=lambda value: value,
            package_validator=lambda _root, _expected: None,
            on_committed=fail_commit,
            on_rollback=rolled_back.append,
            hub_port=_HubPort(),
            downloader=_Downloader(),
        )

    assert not (destination_root / "runtime-package").exists()
    assert state.get("asset-uuid") is None
    assert rolled_back == ["runtime-package"]


@pytest.mark.asyncio
async def test_install_validates_package_after_copying_to_package_id_staging_dir(
    tmp_path: Path,
) -> None:
    class RootPackageDownloader:
        async def download_and_extract(self, _artifact, destination: Path) -> None:
            (destination / "manifest.json").write_text("{}", encoding="utf-8")

    validated_roots: list[Path] = []

    def validate_package(root: Path, expected: str) -> None:
        assert root.name == expected
        assert root.parent.parent == tmp_path / "packages"
        validated_roots.append(root)

    result = await install_hub_asset_package(
        kind="mcp",
        asset_id="asset-uuid",
        destination_root=tmp_path / "packages",
        state_store=HubInstallStateStore(tmp_path / "state"),
        package_name_validator=lambda value: value,
        package_validator=validate_package,
        hub_port=_HubPort(),
        downloader=RootPackageDownloader(),
    )

    assert len(validated_roots) == 1
    assert result.destination.name == "runtime-package"


@pytest.mark.asyncio
async def test_install_rejects_package_id_owned_by_another_asset(
    tmp_path: Path,
) -> None:
    state = HubInstallStateStore(tmp_path / "state")
    state.upsert(
        HubInstallRecord(
            asset_id="other-asset",
            package_id="runtime-package",
            kind="plugin",
            version="1.0.0",
            checksum_sha256="b" * 64,
            installed_at="2026-09-01T00:00:00Z",
        )
    )

    with pytest.raises(ValueError, match="owned by another Hub asset"):
        await install_hub_asset_package(
            kind="plugin",
            asset_id="asset-uuid",
            destination_root=tmp_path / "packages",
            state_store=state,
            package_name_validator=lambda value: value,
            package_validator=lambda _root, _expected: None,
            hub_port=_HubPort(),
            downloader=_Downloader(),
        )

    assert state.get("other-asset") is not None
    assert state.get("asset-uuid") is None


@pytest.mark.asyncio
async def test_install_rejects_existing_destination_without_overwriting_it(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "packages" / "runtime-package"
    destination.mkdir(parents=True)
    sentinel = destination / "user.txt"
    sentinel.write_text("keep", encoding="utf-8")

    with pytest.raises(ValueError, match="destination already exists"):
        await install_hub_asset_package(
            kind="plugin",
            asset_id="asset-uuid",
            destination_root=tmp_path / "packages",
            state_store=HubInstallStateStore(tmp_path / "state"),
            package_name_validator=lambda value: value,
            package_validator=lambda _root, _expected: None,
            hub_port=_HubPort(),
            downloader=_Downloader(),
        )

    assert sentinel.read_text(encoding="utf-8") == "keep"
