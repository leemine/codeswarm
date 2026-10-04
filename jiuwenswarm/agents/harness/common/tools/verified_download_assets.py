# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""TTL-owned immutable download assets for authorized file delivery."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import logging
import os
import stat
import tempfile
import threading
import time
import uuid
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_ASSET_STATE_STAGED = "staged"
_ASSET_STATE_COMMITTED = "committed"
_ACTIVE_ASSET_STATES = frozenset({_ASSET_STATE_STAGED, _ASSET_STATE_COMMITTED})
_DEFAULT_SWEEP_INTERVAL_SECONDS = 30.0
_DEFAULT_ORPHAN_GRACE_SECONDS = 120.0
_DIGEST_CHUNK_SIZE = 1024 * 1024


def _default_asset_root() -> Path:
    configured = str(os.getenv("JIUWENSWARM_DOWNLOAD_ASSET_ROOT") or "").strip()
    if configured:
        return Path(configured).expanduser()
    owner_id = os.getuid() if hasattr(os, "getuid") else os.getpid()
    return Path(tempfile.gettempdir()) / f"jiuwenswarm-download-assets-{owner_id}"


@dataclass(frozen=True)
class VerifiedDownloadAsset:
    """One immutable sealed file staged before its token becomes visible."""

    asset_id: str
    sealed_path: Path
    expires_at: float
    size_bytes: int
    content_digest: str
    workspace_origin_digest: str | None = None
    asset_root: tuple[int, int] | None = None


@dataclass(frozen=True, slots=True)
class VerifiedWorkspaceRegistration:
    """Immutable bytes of a validated owner record; callers receive detached data."""
    _canonical_json: str

    def to_dict(self) -> dict:
        return json.loads(self._canonical_json)


class VerifiedDownloadAssetOwner:
    """Own staged and committed sealed files until their token TTL expires."""

    def __init__(
        self,
        *,
        root: Path | str | None = None,
        now_fn: Callable[[], float] | None = None,
        sweep_interval_seconds: float = _DEFAULT_SWEEP_INTERVAL_SECONDS,
        orphan_grace_seconds: float = _DEFAULT_ORPHAN_GRACE_SECONDS,
        start_sweeper: bool = True,
    ) -> None:
        requested_root = Path(root or _default_asset_root()).expanduser()
        if requested_root.is_symlink():
            raise ValueError("download_asset_root_invalid")
        self.root = requested_root.absolute().resolve(strict=False)
        self._now_fn = now_fn or time.time
        self._sweep_interval_seconds = max(float(sweep_interval_seconds), 0.1)
        self._orphan_grace_seconds = max(float(orphan_grace_seconds), 1.0)
        self._start_sweeper = start_sweeper
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._sweeper: threading.Thread | None = None
        self._governed_root: tuple[int, int] | None = None

    def stage(
        self,
        source_path: Path | str,
        *,
        file_name: str,
        expires_at: float,
    ) -> VerifiedDownloadAsset:
        """Atomically capture one stable delivery-time snapshot."""

        if expires_at <= self._now_fn():
            raise ValueError("download_asset_expired")
        source = Path(source_path).expanduser().absolute()
        self._ensure_root()
        self.prune()
        asset_id = uuid.uuid4().hex
        suffix = Path(file_name).suffix[:32]
        sealed_path = self.root / f"{asset_id}{suffix}"
        sidecar_path = self._sidecar_path(asset_id)
        try:
            actual_size, actual_digest = self._copy_verified_source(
                source,
                sealed_path,
            )
            payload = {
                "asset_id": asset_id,
                "sealed_path": sealed_path.as_posix(),
                "expires_at": float(expires_at),
                "size_bytes": actual_size,
                "content_digest": actual_digest,
                "state": _ASSET_STATE_STAGED,
            }
            self._write_sidecar_atomic(sidecar_path, payload)
        except (OSError, ValueError):
            self._safe_unlink(sidecar_path)
            self._safe_unlink(sealed_path)
            raise
        self._ensure_sweeper()
        return VerifiedDownloadAsset(
            asset_id=asset_id,
            sealed_path=sealed_path,
            expires_at=float(expires_at),
            size_bytes=actual_size,
            content_digest=actual_digest,
        )

    def _stage_from_verified_fd(
        self, source_fd: int, *, file_name: str, expires_at: float,
        workspace_origin: dict, source_check: Callable[[], None],
    ) -> VerifiedDownloadAsset:
        """Host-only same-FD snapshot; provenance and bytes share one registration.

        No owner lock is held while calling the Project/source checker. The
        caller owns the source FD for the complete synchronous operation.
        """
        if expires_at <= self._now_fn():
            raise ValueError("download_asset_expired")
        if self._governed_root is not None:
            self._check_governed_root()
        source_check()
        initial = _file_identity(os.fstat(source_fd))
        asset_id = uuid.uuid4().hex
        sealed = self.root / f"{asset_id}{Path(file_name).suffix[:32]}"
        pending = self.root / f".{asset_id}.stage"
        sidecar = self._sidecar_path(asset_id)
        output = None
        root_fd = _open_directory(self.root, create=True)
        root_stat = os.fstat(root_fd)
        try:
            self._bind_governed_root(root_stat)
            os.fchmod(root_fd, 0o700)
        except BaseException:
            os.close(root_fd)
            raise
        def check_root():
            current = _open_directory(self.root)
            try:
                observed = os.fstat(current)
                if (observed.st_dev, observed.st_ino) != (root_stat.st_dev, root_stat.st_ino):
                    raise ValueError("download_asset_root_changed")
            finally:
                os.close(current)
        try:
            output = os.open(pending.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=root_fd)
            digest, offset = hashlib.sha256(), 0
            while True:
                source_check()
                chunk = os.pread(source_fd, _DIGEST_CHUNK_SIZE, offset)
                if not chunk:
                    break
                _write_all(output, chunk)
                digest.update(chunk)
                offset += len(chunk)
            if _file_identity(os.fstat(source_fd)) != initial or offset != initial["size"]:
                raise ValueError("download_asset_source_changed")
            source_check()
            os.fsync(output)
            os.fchmod(output, 0o400)
            os.close(output)
            output = None
            os.replace(pending.name, sealed.name, src_dir_fd=root_fd, dst_dir_fd=root_fd)
            check_root()
            payload = {
                "asset_id": asset_id, "sealed_path": sealed.as_posix(),
                "expires_at": float(expires_at), "size_bytes": offset,
                "content_digest": f"sha256:{digest.hexdigest()}",
                "state": _ASSET_STATE_STAGED,
                "workspace_origin": copy.deepcopy(workspace_origin),
                "file_name": file_name,
                "asset_root": [root_stat.st_dev, root_stat.st_ino],
                "sealed_file": _file_identity(os.stat(sealed.name, dir_fd=root_fd, follow_symlinks=False)),
            }
            fingerprint = _registration_digest(payload)
            source_check()
            with self._lock:
                self._write_sidecar_atomic(sidecar, payload, directory_fd=root_fd)
            source_check()
            check_root()
            self._ensure_sweeper()
            return VerifiedDownloadAsset(asset_id, sealed, float(expires_at), offset,
                                         payload["content_digest"], fingerprint, (root_stat.st_dev, root_stat.st_ino))
        except BaseException:
            for owned in (sidecar, pending, sealed):
                try:
                    os.unlink(owned.name, dir_fd=root_fd)
                except FileNotFoundError:
                    pass
                except OSError:
                    logger.error("Unexposed sealed asset cleanup failed")
            raise
        finally:
            if output is not None:
                os.close(output)
            os.close(root_fd)

    def workspace_registration(self, asset_id: str, fingerprint: str) -> VerifiedWorkspaceRegistration:
        """Read an exact signed immutable registration; never infer old origins."""
        with self._lock:
            root_fd = _open_directory(self.root)
            try:
                fd = os.open(self._sidecar_path(asset_id).name,
                    os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=root_fd)
                try:
                    before = _file_identity(os.fstat(fd))
                    if before["size"] > 65536:
                        raise ValueError("download_asset_registration_invalid")
                    encoded = os.read(fd, 65537)
                    if len(encoded) != before["size"] or _file_identity(os.fstat(fd)) != before:
                        raise ValueError("download_asset_registration_changed")
                    payload = json.loads(encoded)
                finally:
                    os.close(fd)
                root = os.fstat(root_fd)
                if type(payload) is not dict or payload.get("asset_root") != [root.st_dev, root.st_ino]:
                    raise ValueError("download_asset_root_changed")
            finally:
                os.close(root_fd)
            if (payload.get("state") not in _ACTIVE_ASSET_STATES
                    or payload.get("asset_id") != asset_id
                    or _registration_digest(payload) != fingerprint
                    or self._now_fn() >= float(payload["expires_at"])):
                raise ValueError("download_asset_registration_mismatch")
            path = Path(payload["sealed_path"])
            if (path.parent != self.root or not _regular_file_without_symlink(path)
                    or _file_identity(path.stat(follow_symlinks=False)) != payload["sealed_file"]):
                raise ValueError("download_asset_changed")
            self._bind_governed_root(root)
            return VerifiedWorkspaceRegistration(json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False))

    def _bind_governed_root(self, root) -> None:
        identity = (root.st_dev, root.st_ino)
        with self._lock:
            if self._governed_root is not None and self._governed_root != identity:
                raise ValueError("download_asset_root_changed")
            self._governed_root = identity

    def _check_governed_root(self) -> None:
        fd = _open_directory(self.root)
        try:
            self._bind_governed_root(os.fstat(fd))
        finally:
            os.close(fd)

    @contextmanager
    def _owned_asset_registration(self, asset):
        """Mutation has the same immutable root/file proof as reading."""
        if asset.asset_root is None or asset.workspace_origin_digest is None:
            raise ValueError("download_asset_origin_missing")
        with self._lock:
            fd = _open_directory(self.root)
            try:
                root = os.fstat(fd)
                if (root.st_dev, root.st_ino) != asset.asset_root:
                    raise ValueError("download_asset_root_changed")
                self._bind_governed_root(root)
                payload = _read_sidecar_at(fd, self._sidecar_path(asset.asset_id).name)
                if (not self._payload_matches_asset(payload, asset)
                        or payload.get("asset_root") != list(asset.asset_root)
                        or payload.get("state") not in _ACTIVE_ASSET_STATES
                        or asset.sealed_path.parent != self.root
                        or _file_identity(os.stat(asset.sealed_path.name, dir_fd=fd, follow_symlinks=False)) != payload["sealed_file"]):
                    raise ValueError("download_asset_registration_mismatch")
                yield fd, payload
            finally:
                os.close(fd)

    def commit(self, asset: VerifiedDownloadAsset) -> None:
        """Mark a staged asset delivered while preserving the same TTL owner."""

        if asset.workspace_origin_digest is not None:
            with self._owned_asset_registration(asset) as (fd, payload):
                payload["state"] = _ASSET_STATE_COMMITTED
                self._write_sidecar_atomic(self._sidecar_path(asset.asset_id), payload, directory_fd=fd)
            return
        with self._lock:
            sidecar_path = self._sidecar_path(asset.asset_id)
            payload = self._read_sidecar(sidecar_path)
            if not self._payload_matches_asset(payload, asset):
                raise ValueError("download_asset_registration_mismatch")
            if payload.get("state") not in _ACTIVE_ASSET_STATES:
                raise ValueError("download_asset_state_invalid")
            payload["state"] = _ASSET_STATE_COMMITTED
            self._write_sidecar_atomic(sidecar_path, payload)

    def revoke(self, asset: VerifiedDownloadAsset) -> None:
        """Remove an asset whose token has not become externally visible."""

        if asset.workspace_origin_digest is not None:
            with self._owned_asset_registration(asset) as (fd, _):
                os.unlink(asset.sealed_path.name, dir_fd=fd)
                os.unlink(self._sidecar_path(asset.asset_id).name, dir_fd=fd)
                os.fsync(fd)
            return
        with self._lock:
            self._safe_unlink(self._sidecar_path(asset.asset_id))
            self._safe_unlink(asset.sealed_path)

    def is_active(
        self,
        *,
        asset_id: str,
        sealed_path: Path | str,
        expires_at: float,
        size_bytes: int,
        content_digest: str,
        now: float | None = None,
    ) -> bool:
        """Validate managed token claims against durable staged ownership."""

        current_time = self._now_fn() if now is None else float(now)
        if current_time > float(expires_at):
            return False
        normalized_id = _normalize_asset_id(asset_id)
        if not normalized_id:
            return False
        candidate = Path(sealed_path).expanduser().resolve(strict=False)
        if candidate.parent != self.root or candidate.name.startswith("."):
            return False
        try:
            payload = self._read_sidecar(self._sidecar_path(normalized_id))
        except (OSError, ValueError):
            return False
        try:
            return (
                payload.get("state") in _ACTIVE_ASSET_STATES
                and payload.get("asset_id") == normalized_id
                and payload.get("sealed_path") == candidate.as_posix()
                and float(payload.get("expires_at")) == float(expires_at)
                and int(payload.get("size_bytes")) == int(size_bytes)
                and payload.get("content_digest") == _normalize_digest(content_digest)
                and _regular_file_without_symlink(candidate)
            )
        except (TypeError, ValueError):
            return False

    def prune(self, *, now: float | None = None) -> None:
        # Pure legacy roots retain their existing platform/cleanup behavior.
        if self._governed_root is not None or self._has_governed_registration():
            self._prune_governed(now=now)
        else:
            self._prune_legacy(now=now)

    def _has_governed_registration(self) -> bool:
        if os.name != "posix" or not self.root.exists():
            return False
        fd = _open_directory(self.root)
        try:
            for name in os.listdir(fd):
                if not name.endswith(".json") or name.startswith("."):
                    continue
                try:
                    if "workspace_origin" in _read_sidecar_at(fd, name):
                        return True
                except (OSError, ValueError):
                    continue
            return False
        finally:
            os.close(fd)

    def _prune_governed(self, *, now=None) -> None:
        current_time = self._now_fn() if now is None else float(now)
        with self._lock:
            try:
                fd = _open_directory(self.root)
            except OSError:
                logger.error("Sealed asset prune root unavailable")
                return
            try:
                root = os.fstat(fd)
                identity = (root.st_dev, root.st_ino)
                if self._governed_root is not None and identity != self._governed_root:
                    logger.error("Sealed asset prune root changed")
                    return
                records = []
                for name in os.listdir(fd):
                    if name.startswith(".") or not name.endswith(".json"):
                        continue
                    try:
                        payload = _read_sidecar_at(fd, name)
                        if "workspace_origin" in payload and payload.get("asset_root") != list(identity):
                            logger.error("Sealed asset prune root changed")
                            return
                        if (Path(payload["sealed_path"]).parent != self.root
                                or payload["asset_id"] != Path(name).stem
                                or payload.get("state") not in _ACTIVE_ASSET_STATES
                                or not math.isfinite(float(payload["expires_at"]))):
                            raise ValueError("download_asset_registration_mismatch")
                        if "workspace_origin" in payload:
                            _registration_digest(payload)
                        records.append((name, payload))
                    except (KeyError, OSError, TypeError, ValueError):
                        # Unknown registration ownership is retained, never reset.
                        logger.error("Sealed asset prune registration unavailable")
                        return
                self._bind_governed_root(root)
                registered = set()
                for name, payload in records:
                    path = Path(str(payload.get("sealed_path", "")))
                    if path.parent != self.root:
                        continue
                    registered.add(path.name)
                    try:
                        expires_at = float(payload["expires_at"])
                        if not math.isfinite(expires_at):
                            raise ValueError("download_asset_expiry_invalid")
                    except (KeyError, TypeError, ValueError):
                        logger.error("Sealed asset prune registration unavailable")
                        continue
                    if current_time <= expires_at:
                        continue
                    if "workspace_origin" in payload:
                        try:
                            asset = VerifiedDownloadAsset(payload["asset_id"], path, float(payload["expires_at"]),
                                payload["size_bytes"], payload["content_digest"], _registration_digest(payload), identity)
                            # Uses its own same-root FD; no raw path mutation.
                            self.revoke(asset)
                        except (KeyError, OSError, TypeError, ValueError):
                            logger.error("Expired sealed asset cleanup remains unconfirmed")
                    elif payload.get("asset_id") == Path(name).stem:
                        for owned in (path.name, name):
                            try:
                                os.unlink(owned, dir_fd=fd)
                            except FileNotFoundError:
                                pass
                cutoff = current_time - self._orphan_grace_seconds
                for name in os.listdir(fd):
                    if name.startswith(".") or name.endswith(".json") or name in registered:
                        continue
                    try:
                        info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                        if stat.S_ISREG(info.st_mode) and info.st_mtime < cutoff:
                            os.unlink(name, dir_fd=fd)
                    except FileNotFoundError:
                        pass
            finally:
                os.close(fd)

    def _prune_legacy(self, *, now: float | None = None) -> None:
        """Recover registered assets and remove expired or old orphan files."""

        current_time = self._now_fn() if now is None else float(now)
        self._ensure_root()
        with self._lock:
            registered_names: set[str] = set()
            for sidecar_path in self.root.glob("*.json"):
                if sidecar_path.is_symlink():
                    self._safe_unlink(sidecar_path)
                    continue
                try:
                    payload = self._read_sidecar(sidecar_path)
                    sealed_path = Path(str(payload["sealed_path"])).resolve(
                        strict=False
                    )
                    valid_location = (
                        sealed_path.parent == self.root
                        and payload.get("asset_id") == sidecar_path.stem
                    )
                    expired = current_time > float(payload["expires_at"])
                    valid_state = payload.get("state") in _ACTIVE_ASSET_STATES
                except (KeyError, OSError, TypeError, ValueError):
                    valid_location = False
                    expired = True
                    valid_state = False
                    sealed_path = self.root / sidecar_path.stem
                if not valid_location or expired or not valid_state:
                    self._safe_unlink(sidecar_path)
                    if valid_location:
                        self._safe_unlink(sealed_path)
                    continue
                registered_names.add(sealed_path.name)

            orphan_cutoff = current_time - self._orphan_grace_seconds
            for candidate in self.root.iterdir():
                if candidate.name.startswith(".") or candidate.suffix == ".json":
                    continue
                if candidate.name in registered_names or candidate.is_symlink():
                    continue
                try:
                    if candidate.stat(follow_symlinks=False).st_mtime < orphan_cutoff:
                        self._safe_unlink(candidate)
                except OSError:
                    continue

    def close(self) -> None:
        """Stop the optional background sweeper."""

        self._stop_event.set()
        sweeper = self._sweeper
        if sweeper is not None and sweeper is not threading.current_thread():
            sweeper.join(timeout=1.0)

    def _ensure_root(self) -> None:
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.root.is_symlink() or not self.root.is_dir():
            raise ValueError("download_asset_root_invalid")
        os.chmod(self.root, 0o700)

    def _ensure_sweeper(self) -> None:
        if not self._start_sweeper or self._sweeper is not None:
            return
        with self._lock:
            if self._sweeper is not None:
                return
            self._sweeper = threading.Thread(
                target=self._sweep_loop,
                name="verified-download-asset-sweeper",
                daemon=True,
            )
            self._sweeper.start()

    def _sweep_loop(self) -> None:
        while not self._stop_event.wait(self._sweep_interval_seconds):
            try:
                self.prune()
            except OSError:
                logger.exception("Failed to prune verified download assets")

    @staticmethod
    def _copy_verified_source(
        source: Path,
        destination: Path,
    ) -> tuple[int, str]:
        try:
            source_stat = source.stat(follow_symlinks=False)
        except OSError as exc:
            raise ValueError("download_asset_source_not_file") from exc
        if not stat.S_ISREG(source_stat.st_mode) or source.is_symlink():
            raise ValueError("download_asset_source_not_file")
        read_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        write_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        source_fd = os.open(source, read_flags, 0o600)
        destination_fd: int | None = None
        try:
            opened_source_stat = os.fstat(source_fd)
            if not stat.S_ISREG(opened_source_stat.st_mode):
                raise ValueError("download_asset_source_not_file")
            destination_fd = os.open(destination, write_flags, 0o600)
            digest = hashlib.sha256()
            copied = 0
            while True:
                chunk = os.read(source_fd, _DIGEST_CHUNK_SIZE)
                if not chunk:
                    break
                _write_all(destination_fd, chunk)
                copied += len(chunk)
                digest.update(chunk)
            actual_digest = f"sha256:{digest.hexdigest()}"
            if copied != opened_source_stat.st_size:
                raise ValueError("download_asset_size_mismatch")
            os.fsync(destination_fd)
            os.chmod(destination, 0o400)
            return copied, actual_digest
        finally:
            os.close(source_fd)
            if destination_fd is not None:
                os.close(destination_fd)

    def _write_sidecar_atomic(
        self,
        sidecar_path: Path,
        payload: dict[str, Any],
        *, directory_fd: int | None = None,
    ) -> None:
        temp_path = self.root / f".{sidecar_path.stem}.{uuid.uuid4().hex}.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        file_descriptor = None
        try:
            file_descriptor = os.open(temp_path if directory_fd is None else temp_path.name,
                                      flags, 0o600, dir_fd=directory_fd)
            encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                                 allow_nan=False).encode("utf-8")
            _write_all(file_descriptor, encoded)
            os.fsync(file_descriptor)
            os.close(file_descriptor)
            file_descriptor = None
            if directory_fd is not None:
                os.replace(temp_path.name, sidecar_path.name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
                os.fsync(directory_fd)
            else:
                os.replace(temp_path, sidecar_path)
                if os.name != "nt":
                    parent_fd = os.open(self.root, os.O_RDONLY)
                    try:
                        os.fsync(parent_fd)
                    finally:
                        os.close(parent_fd)
        finally:
            if file_descriptor is not None:
                os.close(file_descriptor)
            if directory_fd is None:
                self._safe_unlink(temp_path)
            else:
                try:
                    os.unlink(temp_path.name, dir_fd=directory_fd)
                except FileNotFoundError:
                    pass
                except OSError:
                    logger.error("Sealed registration temporary cleanup failed")

    @staticmethod
    def _read_sidecar(sidecar_path: Path) -> dict[str, Any]:
        if not _regular_file_without_symlink(sidecar_path):
            raise ValueError("download_asset_sidecar_invalid")
        payload = json.loads(sidecar_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("download_asset_sidecar_invalid")
        return payload

    @staticmethod
    def _payload_matches_asset(
        payload: dict[str, Any],
        asset: VerifiedDownloadAsset,
    ) -> bool:
        return (
            payload.get("asset_id") == asset.asset_id
            and payload.get("sealed_path") == asset.sealed_path.as_posix()
            and float(payload.get("expires_at")) == asset.expires_at
            and int(payload.get("size_bytes")) == asset.size_bytes
            and payload.get("content_digest") == asset.content_digest
            and (asset.workspace_origin_digest is None
                 or _registration_digest(payload) == asset.workspace_origin_digest)
        )

    def _sidecar_path(self, asset_id: str) -> Path:
        normalized_id = _normalize_asset_id(asset_id)
        if not normalized_id:
            raise ValueError("download_asset_id_invalid")
        return self.root / f"{normalized_id}.json"

    def _safe_unlink(self, path: Path) -> None:
        if path.parent.resolve(strict=False) != self.root:
            return
        try:
            path.unlink(missing_ok=True)
        except OSError:
            logger.warning("Failed to remove verified download asset path=%s", path)


def _read_sidecar_at(root_fd, name):
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=root_fd)
    try:
        before = _file_identity(os.fstat(fd))
        if before["size"] > 65536:
            raise ValueError("download_asset_registration_invalid")
        encoded = os.read(fd, 65537)
        if len(encoded) != before["size"] or _file_identity(os.fstat(fd)) != before:
            raise ValueError("download_asset_registration_changed")
        value = json.loads(encoded)
        if type(value) is not dict:
            raise ValueError("download_asset_registration_invalid")
        return value
    finally:
        os.close(fd)


def _open_directory(path: Path, *, create: bool = False) -> int:
    """Owned directory FD with no symlink in any component (managed POSIX)."""
    if os.name != "posix" or not hasattr(os, "O_NOFOLLOW"):
        raise ValueError("sealed asset platform unsupported")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open("/", flags)
    try:
        for part in path.parts[1:]:
            if create:
                try:
                    os.mkdir(part, 0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass
            child = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _file_identity(value) -> dict:
    if not stat.S_ISREG(value.st_mode):
        raise ValueError("download_asset_not_regular")
    return {"device": value.st_dev, "inode": value.st_ino, "size": value.st_size,
            "mtime_ns": value.st_mtime_ns, "ctime_ns": value.st_ctime_ns}


def _registration_digest(payload: dict) -> str:
    keys = {"asset_id", "sealed_path", "expires_at", "size_bytes", "content_digest",
            "workspace_origin", "asset_root", "sealed_file", "file_name", "state"}
    if type(payload) is not dict or set(payload) != keys:
        raise ValueError("download_asset_origin_invalid")
    body = {key: value for key, value in payload.items() if key != "state"}
    return "sha256:" + hashlib.sha256(json.dumps(body, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _normalize_asset_id(value: str) -> str:
    normalized = str(value or "").strip().lower()
    if len(normalized) != 32:
        return ""
    try:
        int(normalized, 16)
    except ValueError:
        return ""
    return normalized


def _normalize_digest(value: str) -> str:
    normalized = str(value or "").strip().lower()
    normalized = normalized.removeprefix("sha256:")
    if len(normalized) != 64:
        raise ValueError("download_asset_digest_invalid")
    try:
        int(normalized, 16)
    except ValueError as exc:
        raise ValueError("download_asset_digest_invalid") from exc
    return f"sha256:{normalized}"


def _regular_file_without_symlink(path: Path) -> bool:
    try:
        mode = path.stat(follow_symlinks=False).st_mode
    except OSError:
        return False
    return stat.S_ISREG(mode) and not path.is_symlink()


def _write_all(file_descriptor: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(file_descriptor, view)
        if written <= 0:
            raise OSError("download asset write made no progress")
        view = view[written:]


_SHARED_VERIFIED_DOWNLOAD_ASSET_OWNER = VerifiedDownloadAssetOwner()


def get_verified_download_asset_owner() -> VerifiedDownloadAssetOwner:
    """Return the process-local owner backed by the shared delivery root."""

    return _SHARED_VERIFIED_DOWNLOAD_ASSET_OWNER
