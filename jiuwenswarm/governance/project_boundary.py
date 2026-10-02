# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Project ACL checks for retained AgentServer resource entry points.

The ProjectAdapter owns project CRUD policy; this closes older Session,
history, lifecycle and file routes to the same protected resources. It does
not create Session sharing, a new subscription service or a tool sandbox.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from jiuwenswarm.governance.contracts import ProjectAction, TrustedIdentity


class ProjectAccessDenied(PermissionError):
    code = "PROJECT_ACCESS_DENIED"


_READ_METHODS = frozenset({
    "session.list", "session.archived.list", "session.get_metadata",
    "session.preview", "session.plan_status", "history.get",
    "history.list_turns", "files.list", "files.get", "file.download_verified_chunk",
    "path.get", "path.select_directory", "path.select_files", "document.formats",
    "project.lifecycle", "team.snapshot", "team.members.get", "team.history.get",
    "team.bindings.list", "command.context", "command.recap", "command.status",
    "command.diff", "command.session", "memory.list", "memory.open", "memory.status",
})
_EXECUTION_PREFIXES = ("chat.", "command.", "heartbeat.", "team.")
_RESOURCE_PREFIXES = (
    "project.", "session.", "history.", "file.", "files.", "path.",
    "document.", "media.", "im.file", "chat.", "command.", "heartbeat.",
    "team.", "cron.sessions.", "memory.", "harmonyos.",
)


def request_project_action(method: str) -> ProjectAction:
    if method in _READ_METHODS:
        return "read"
    if method in {"project.delete", "project.acl.update"}:
        return "admin"
    if method.startswith(_EXECUTION_PREFIXES) or method in {
        "session.create", "session.switch", "session.fork", "session.input.intent",
    }:
        return "execute"
    return "write"


def _referenced_values(value: Any, keys: frozenset[str]) -> set[str]:
    """Inspect structured routing fields, never interpret prompt text as IDs."""
    result: set[str] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            if key in keys and isinstance(child, str) and child.strip():
                result.add(child.strip())
            elif key in keys and isinstance(child, list):
                result.update(item.strip() for item in child if isinstance(item, str) and item.strip())
            elif isinstance(child, (dict, list)):
                result.update(_referenced_values(child, keys))
    elif isinstance(value, list):
        for child in value:
            result.update(_referenced_values(child, keys))
    return result


def authorize_resource_request(
    request: Any, identity: TrustedIdentity | None, *, access_store: Any = None,
) -> None:
    """Re-read current ACL before dispatch; missing identity never grants access.

    Unscoped legacy inventory/file APIs cannot distinguish audiences yet. If
    they could expose an inaccessible protected project they fail closed;
    authorized scoped APIs remain available. Audience-filtered sharing is R2-B.
    """
    method = getattr(getattr(request, "req_method", None), "value", "")
    if not method.startswith(_RESOURCE_PREFIXES):
        return
    # ProjectAdapter performs its own operation-specific checks and filtering.
    if method.startswith("project.") and method not in {
        "project.delete", "project.lifecycle", "project.sessions.archive",
        "project.sessions.delete_archived",
    }:
        return
    from jiuwenswarm.server.runtime.session import project_store
    from jiuwenswarm.server.runtime.session.project_access import (
        ProjectAccessStore, ProjectAccessDenied as StorageAccessDenied,
    )
    from jiuwenswarm.server.runtime.session.session_metadata import get_session_metadata

    access = access_store if access_store is not None else ProjectAccessStore()
    projects = project_store.list_projects(include_hidden=True, cache_bust=True)
    params = request.params if isinstance(request.params, dict) else {}
    project_ids = _referenced_values(params, frozenset({"project_id"}))
    session_ids = _referenced_values(params, frozenset({
        "session_id", "target_session_id", "source_session_id", "previous_session_id",
    }))
    if request.session_id:
        session_ids.add(request.session_id)
    paths = _referenced_values(params, frozenset({
        "project_dir", "cwd", "path", "file_path", "resolved_path", "initial_dir", "trusted_dirs",
    }))
    source_projects: set[str] = set()
    for session_id in session_ids:
        metadata = get_session_metadata(session_id, cache_bust=True, enable_writeback=False)
        if isinstance(metadata, dict):
            if metadata.get("project_id"):
                project_ids.add(str(metadata["project_id"]))
                source_projects.add(str(metadata["project_id"]))
            if metadata.get("project_dir"):
                paths.add(str(metadata["project_dir"]))
    if method == "session.rebind_project" and any(access.is_protected(pid) for pid in source_projects):
        # This legacy operation rewrites project provenance. Authorized context
        # handoff needs R2-B3; do not turn protected history into legacy data.
        raise ProjectAccessDenied("protected session project rebinding is not supported")
    # A legacy ID can alias a protected workspace (including another surface).
    # Resolve its stored directory before checking the whole protected scope.
    paths.update(p.project_dir for p in projects if p.project_id in project_ids and p.project_dir)
    for project in projects:
        if not project.project_dir:
            continue
        root = Path(project.project_dir).resolve()
        for raw in paths:
            candidate = Path(raw).expanduser()
            if candidate.is_absolute():
                candidate = candidate.resolve()
                if candidate.is_relative_to(root) or root.is_relative_to(candidate):
                    project_ids.add(project.project_id)
    protected = {pid for pid in project_ids if access.is_protected(pid)}
    # A fabricated/unrelated session ID or a token-only file request cannot
    # suppress the inventory check by merely being present on the wire.
    unscoped = (
        not project_ids
        or method in {"session.list", "session.archived.list", "cron.sessions.delete"}
        or method.startswith(("file.", "files.", "path.", "document.", "media.", "im.file", "team."))
        or (method == "project.lifecycle" and (params.get("events") or params.get("inventory")))
    )
    if unscoped:
        try:
            protected.update(access.protected_ids())
        except StorageAccessDenied as exc:
            raise ProjectAccessDenied("project authorization storage unavailable") from exc
    actor_id = identity.actor_id if isinstance(identity, TrustedIdentity) else ""
    action = request_project_action(method)
    for project_id in protected:
        decision = access.authorize(project_id, actor_id, action)
        if not actor_id or not decision.allowed:
            raise ProjectAccessDenied("project access denied")
