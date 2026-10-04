"""Private owner download permits over the existing Session/resource authority.

Signed artifact facts identify a previously published file; they grant nothing.
Callers retain their authenticated principal and call ``check`` at their final
delivery sink. No HTTP routes, credentials, queue or persistent ACL lives here.
"""

from __future__ import annotations

import os
import math
import stat
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

from .contracts import TrustedIdentity
from .resources import ResourceGuard, ResourceRequest

ARTIFACT_NAMESPACE = "workspace_artifact_v1"
MAX_DOWNLOAD_TOKEN_BYTES = 4096
MAX_DOWNLOAD_CHUNK_BYTES = 65536


class WorkspaceDownloadDenied(PermissionError):
    """Generic denial: do not include token, path or underlying policy errors."""


def _deny():
    raise WorkspaceDownloadDenied("owner Workspace download denied")


@dataclass(frozen=True, slots=True)
class _Source:
    identity: TrustedIdentity
    session_id: str
    owner_revision: int
    source_epoch: int
    session_generation: int
    project_generation: int
    binding: tuple


@dataclass(frozen=True, slots=True)
class _File:
    device: int
    inode: int
    size: int
    mtime_ns: int
    ctime_ns: int

    @classmethod
    def from_stat(cls, value):
        if not stat.S_ISREG(value.st_mode):
            _deny()
        return cls(
            value.st_dev,
            value.st_ino,
            value.st_size,
            value.st_mtime_ns,
            value.st_ctime_ns,
        )


def _path(raw):
    if (
        not isinstance(raw, str)
        or not raw
        or len(raw) > 2048
        or not Path(raw).is_absolute()
        or str(Path(raw)) != raw
        or ".." in Path(raw).parts
        or "\x00" in raw
    ):
        _deny()
    return Path(raw)


def _source(host, identity, sid):
    # SharingHost's normal current authority includes continuation provenance.
    # Never substitute its cleanup-only stamp, which survives revocation.
    from jiuwenswarm.server.runtime.session import lifecycle
    from jiuwenswarm.server.runtime.session.sharing_host import (
        _metadata_cleanup_binding,
    )

    with host._storage._locked():
        record, owner, source, facts = host._current(host._storage._load(), sid)
        if identity != owner or "view" not in facts[0]:
            _deny()
        binding = _metadata_cleanup_binding(lifecycle.raw_metadata(sid))
        if binding["project_id"] != source[
            "project_id"
        ] or binding != _metadata_cleanup_binding(lifecycle.raw_metadata(sid)):
            _deny()
        root = _path(binding["project_dir"])
        if str(root.resolve()) != str(root):
            _deny()
        for value in binding.values():
            if value is not None and len(value) > 2048:
                _deny()
        return _Source(
            identity,
            sid,
            record["revision"],
            source["epoch"],
            source["session_generation"],
            source["project_generation"],
            tuple(binding.items()),
        ), host.owner_revision(sid, identity)


def _decision(host, source, path):
    binding = dict(source.binding)
    root, project = binding["project_dir"], binding["project_id"]
    if not _path(path).is_relative_to(_path(root)) or path == root:
        _deny()
    rows = host._storage.resource_grants(project, source.identity)["resources"]
    ids = {
        row["resource_id"]
        for row in rows
        if row["kind"] == "workspace"
        and row["reference"] == root
        and row["action"] == "read"
    }
    if len(ids) != 1:
        _deny()
    decision = ResourceGuard(host._storage).check(
        project, source.identity, ResourceRequest(next(iter(ids)), "read", path)
    )
    if decision.reference != root:
        _deny()
    return decision


@contextmanager
def _open(root, path):
    """Walk from / using owned directory FDs; no symlink or blocking FIFO open."""
    required = ("O_NOFOLLOW", "O_DIRECTORY", "O_NONBLOCK", "pread")
    if os.name != "posix" or any(not hasattr(os, name) for name in required):
        _deny()
    root_path, target = _path(root), _path(path)
    if not target.is_relative_to(root_path) or target == root_path:
        _deny()
    directory_flags = (
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    )
    fd = os.open("/", directory_flags)
    file_fd = None
    try:
        for part in root_path.parts[1:]:
            child = os.open(part, directory_flags, dir_fd=fd)
            os.close(fd)
            fd = child
        root_stat = os.fstat(fd)
        relative = target.relative_to(root_path).parts
        for part in relative[:-1]:
            child = os.open(part, directory_flags, dir_fd=fd)
            os.close(fd)
            fd = child
        file_fd = os.open(
            relative[-1],
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0),
            dir_fd=fd,
        )
        yield (
            file_fd,
            (root_stat.st_dev, root_stat.st_ino),
            _File.from_stat(os.fstat(file_fd)),
        )
    finally:
        if file_fd is not None:
            os.close(file_fd)
        os.close(fd)


@dataclass(frozen=True, slots=True, repr=False)
class WorkspaceArtifactIssuer:
    """Host-only source captured before the producing call's first await."""

    _host: object
    _identity: Callable
    _source: _Source
    _origin: Callable
    _paths: tuple[str, ...]
    _revision: int
    _decisions: tuple

    @classmethod
    def capture(
        cls,
        host,
        identity_resolver,
        session_id,
        *,
        channel_id,
        workspace,
        source_check,
        actual_paths,
    ):
        try:
            if (
                not callable(source_check)
                or source_check() is not True
                or type(actual_paths) is not tuple
                or not actual_paths
            ):
                _deny()
            for path in actual_paths:
                _path(path)
            identity = identity_resolver()
            if not isinstance(identity, TrustedIdentity):
                _deny()
            source, revision = _source(host, identity, session_id)
            binding = dict(source.binding)
            if (
                binding["channel_id"] != channel_id
                or binding["project_dir"] != workspace
            ):
                _deny()
            decisions = tuple(_decision(host, source, path) for path in actual_paths)
            if source_check() is not True or identity_resolver() != identity:
                _deny()
            return cls(host, identity_resolver, source, source_check, actual_paths, revision, decisions)
        except Exception:
            raise WorkspaceDownloadDenied("artifact source unavailable") from None

    def issue(self, path, session_id):
        try:
            if (
                path not in self._paths
                or self._origin() is not True
                or session_id != self._source.session_id
                or self._identity() != self._source.identity
            ):
                _deny()
            current, revision = _source(self._host, self._source.identity, session_id)
            if current != self._source or revision != self._revision:
                _deny()
            decision = self._decisions[self._paths.index(path)]
            if _decision(self._host, current, path) != decision:
                _deny()
            with _open(dict(current.binding)["project_dir"], path) as (_, root, file):
                proof = {
                    "schema_version": 1,
                    "source": asdict(current),
                    "root": list(root),
                    "file": asdict(file),
                }
            if (
                self._origin() is not True
                or self._identity() != current.identity
                or _source(self._host, current.identity, session_id)
                != (current, revision)
                or _decision(self._host, current, path) != decision
            ):
                _deny()
            return proof
        except Exception:
            raise WorkspaceDownloadDenied("artifact source unavailable") from None


def _parse(payload):
    if (
        type(payload) is not dict
        or set(payload) != {"path", "sid", "exp", ARTIFACT_NAMESPACE}
        or type(payload["sid"]) is not str
        or not payload["sid"]
    ):
        _deny()
    _path(payload["path"])
    if (
        type(payload["exp"]) not in (int, float)
        or not math.isfinite(payload["exp"])
        or payload["exp"] <= 0
    ):
        _deny()
    proof = payload[ARTIFACT_NAMESPACE]
    if (
        type(proof) is not dict
        or set(proof) != {"schema_version", "source", "root", "file"}
        or type(proof["schema_version"]) is not int
        or proof["schema_version"] != 1
    ):
        _deny()
    source = proof["source"]
    if type(source) is not dict or set(source) != set(_Source.__dataclass_fields__):
        _deny()
    identity = source["identity"]
    if type(identity) is not dict or set(identity) != {
        "actor_id",
        "subject_id",
        "authority",
    }:
        _deny()
    for key in (
        "owner_revision",
        "source_epoch",
        "session_generation",
        "project_generation",
    ):
        if type(source[key]) is not int or not 0 <= source[key] < 2**63:
            _deny()
    if (
        source["owner_revision"] < 1
        or source["source_epoch"] < 1
        or source["session_id"] != payload["sid"]
    ):
        _deny()
    from jiuwenswarm.server.runtime.session.sharing_host import (
        _metadata_cleanup_binding,
        _CLEANUP_BINDING_FIELDS,
    )

    binding = source["binding"]
    if (
        type(binding) not in (tuple, list)
        or len(binding) != len(_CLEANUP_BINDING_FIELDS)
        or any(type(row) not in (tuple, list) or len(row) != 2 for row in binding)
        or tuple(row[0] for row in binding) != _CLEANUP_BINDING_FIELDS
    ):
        _deny()
    normalized_binding = _metadata_cleanup_binding(dict(binding))
    source = _Source(
        **{
            **source,
            "identity": TrustedIdentity(**identity),
            "binding": tuple(normalized_binding.items()),
        }
    )
    file = proof["file"]
    if (
        type(file) is not dict
        or set(file) != set(_File.__dataclass_fields__)
        or any(
            type(value) is not int or not 0 <= value < 2**64 for value in file.values()
        )
    ):
        _deny()
    root = proof["root"]
    if (
        type(root) is not list
        or len(root) != 2
        or any(type(n) is not int or not 0 <= n < 2**64 for n in root)
    ):
        _deny()
    return source, tuple(root), _File(**file)


@dataclass(frozen=True, slots=True, repr=False)
class WorkspaceDownloadPermit:
    """One request's exact authority; never rebind on retry or expose on wire."""

    _host: object
    _identity: Callable
    _source: _Source
    _revision: int
    _decision: object
    _root: tuple
    _file: _File
    _path: str
    _token_check: Callable = field(repr=False)

    @property
    def size(self):
        return self._file.size

    @property
    def name(self):
        return Path(self._path).name

    @property
    def session_id(self):
        return self._source.session_id

    @classmethod
    def capture(cls, host, identity_resolver, session_id, token, *, token_validator):
        """token_validator must be the host's existing HMAC validator, never wire."""
        try:
            if (
                not isinstance(token, str)
                or not 0 < len(token.encode()) <= MAX_DOWNLOAD_TOKEN_BYTES
            ):
                _deny()
            payload = token_validator(token, session_id=session_id)
            source, root, file = _parse(payload)
            if (
                identity_resolver() != source.identity
                or session_id != source.session_id
            ):
                _deny()
            current, revision = _source(host, source.identity, session_id)
            if source != current:
                _deny()
            decision = _decision(host, source, payload["path"])

            def token_check():
                if token_validator(token, session_id=session_id) != payload:
                    _deny()

            permit = cls(
                host,
                identity_resolver,
                source,
                revision,
                decision,
                root,
                file,
                payload["path"],
                token_check,
            )
            permit.check()
            return permit
        except Exception:
            raise WorkspaceDownloadDenied("owner Workspace download denied") from None

    def _authority(self):
        self._token_check()
        if (
            self._identity() != self._source.identity
            or _source(self._host, self._source.identity, self.session_id)
            != (self._source, self._revision)
            or _decision(self._host, self._source, self._path) != self._decision
        ):
            _deny()

    def check(self):
        """Call after every wait and at the actual final headers/body sink."""
        try:
            self._authority()
            with _open(dict(self._source.binding)["project_dir"], self._path) as (
                _,
                root,
                file,
            ):
                if (root, file) != (self._root, self._file):
                    _deny()
            self._authority()
        except Exception:
            raise WorkspaceDownloadDenied("owner Workspace download denied") from None

    def read(self, offset: int, limit: int) -> bytes:
        """One bounded synchronous read, no retained FD and no await under lock."""
        try:
            if (
                type(offset) is not int
                or not 0 <= offset <= self.size
                or type(limit) is not int
                or not 1 <= limit <= MAX_DOWNLOAD_CHUNK_BYTES
            ):
                _deny()
            self._authority()
            with _open(dict(self._source.binding)["project_dir"], self._path) as (
                fd,
                root,
                file,
            ):
                if (root, file) != (self._root, self._file):
                    _deny()
                # Original sidecar is reentrant. No history lock or await is
                # acquired; revocation cannot pass the actual bounded read.
                with self._host._storage._locked():
                    self._authority()
                    data = os.pread(fd, min(limit, self.size - offset), offset)
                    if _File.from_stat(os.fstat(fd)) != self._file:
                        _deny()
                    self._authority()
            self.check()
            if len(data) != min(limit, self.size - offset):
                _deny()
            return data
        except Exception:
            raise WorkspaceDownloadDenied("owner Workspace download denied") from None


def capture_workspace_request(host, identity_resolver, session_id, params):
    """Compile the exact bounded consumer; path and authority are never inputs."""
    from jiuwenswarm.agents.harness.common.tools.web_file_download import validate_file_download_token
    if (type(params) is not dict or set(params) != {'token', 'offset', 'limit'}
            or type(params['offset']) is not int or params['offset'] < 0
            or type(params['limit']) is not int
            or not 1 <= params['limit'] <= MAX_DOWNLOAD_CHUNK_BYTES):
        raise WorkspaceDownloadDenied('invalid Workspace download request')
    permit = WorkspaceDownloadPermit.capture(host, identity_resolver, session_id,
        params['token'], token_validator=validate_file_download_token)
    if params['offset'] > permit.size:
        raise WorkspaceDownloadDenied('invalid Workspace download range')
    return permit


# Private per-call transport proof. No serialized request field can install it.
_workspace_send_guard = ContextVar('workspace_download_send_guard', default=None)


@dataclass
class _WorkspaceSendGuard:
    check: Callable
    active: bool = True
    request_id: str | None = None

    def consume(self, client, wire):
        request_id = wire.get('request_id')
        if (not self.active or not isinstance(request_id, str) or not request_id
                or (self.request_id is not None and self.request_id != request_id)):
            raise WorkspaceDownloadDenied('original download request ended or changed')
        self.check(client, wire)
        self.request_id = request_id


@contextmanager
def workspace_send_scope(guard):
    owner = _WorkspaceSendGuard(guard)
    marker = _workspace_send_guard.set(owner)
    try:
        yield
    finally:
        owner.active = False
        _workspace_send_guard.reset(marker)


def check_workspace_send(client, wire):
    guard = _workspace_send_guard.get()
    if guard is not None:
        guard.consume(client, wire)
    elif wire.get('method') == 'file.download_workspace_chunk':
        raise WorkspaceDownloadDenied('original local download transport required')
