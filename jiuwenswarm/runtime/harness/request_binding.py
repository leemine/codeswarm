# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Bind a selected execution only after product request admission succeeds."""

from __future__ import annotations

from pathlib import Path
from typing import Any


def bind_admitted_request_execution(
    agent_manager: Any,
    request: Any,
    project_dir: str | None,
    *,
    session_metadata: dict[str, Any] | None = None,
) -> None:
    """Pin the server-owned selection before an Agent or MCP child is built.

    A chat request cannot install raw provider settings or switch an existing
    Session. The product admission callback has already authorized its
    subject, mode and Workspace before this function is called.
    """
    session_id = str(getattr(request, "session_id", "") or "").strip()
    if not session_id:
        return
    if session_metadata is None:
        from jiuwenswarm.server.runtime.session.session_metadata import (
            get_session_metadata,
        )

        session_metadata = get_session_metadata(
            session_id, cache_bust=True, enable_writeback=False
        )
    if not isinstance(session_metadata, dict):
        session_metadata = {}
    selected_profile_id = session_metadata.get("execution_profile_id")
    params = getattr(request, "params", None)
    requested_profile_id = (
        params.get("execution_profile_id") if isinstance(params, dict) else None
    )
    if requested_profile_id is not None and requested_profile_id != selected_profile_id:
        raise ValueError("execution profile is immutable after session creation")
    if not isinstance(selected_profile_id, str) or not selected_profile_id:
        return

    from jiuwenswarm.common.config import get_config
    from jiuwenswarm.common.utils import get_default_project_session_workspace_dir
    from jiuwenswarm.runtime.harness.config_source import load_execution_catalog

    catalog = load_execution_catalog(get_config())
    if catalog is None:
        raise RuntimeError("session execution profile is no longer configured")
    source = catalog.source(explicit_profile_id=selected_profile_id)
    spec = source.resolve()
    if spec.config_revision != session_metadata.get("execution_config_revision"):
        raise RuntimeError("session execution configuration changed")
    from openjiuwen.harness.engine.config import config_fingerprint

    if config_fingerprint(spec) != session_metadata.get("execution_config_fingerprint"):
        raise RuntimeError("session execution configuration changed")
    workspace = Path(
        project_dir or get_default_project_session_workspace_dir(session_id)
    ).expanduser().resolve(strict=False)
    channel_id = str(getattr(request, "channel_id", "") or "")
    subject = str(
        getattr(request, "user_id", "")
        or session_metadata.get("user_id")
        or f"{channel_id}:{session_id}"
    ).strip()
    bindings = agent_manager.execution_bindings
    bound = bindings.bind(
        source,
        subject_id=subject,
        host_session_id=session_id,
        workspace=str(workspace),
    )
    setattr(request, "_bound_execution", bound)
    setattr(request, "_execution_source", source)
    setattr(request, "_execution_bindings", bindings)
    remember = getattr(agent_manager, "remember_execution_binding", None)
    if callable(remember):
        remember(channel_id, session_id, bound.binding)
