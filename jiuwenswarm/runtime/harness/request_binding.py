# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Bind a selected execution only after product request admission succeeds."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from openjiuwen.harness.engine import ExecutionBinding

from jiuwenswarm.common.runtime_workspace import (
    RuntimeWorkspacePaths,
    bind_session_runtime_workspace,
)
from jiuwenswarm.runtime.harness.binding_store import (
    BoundExecution,
    ExecutionBindingStore,
)
from jiuwenswarm.runtime.harness.config_source import ExecutionConfigSource
from jiuwenswarm.runtime.harness.recovery_store import (
    ExecutionRecoveryUnavailableError,
    SessionExecutionRecovery,
)

from jiuwenswarm.runtime.harness.surface import (
    EffectiveSurfaceSnapshot, build_surface_identity, validate_surface_request,
)


@dataclass(frozen=True, slots=True)
class AdmittedExecutionRoute:
    """Authorized construction inputs frozen before an Agent is allocated."""

    channel_id: str
    source: ExecutionConfigSource
    bindings: ExecutionBindingStore
    bound: BoundExecution
    runtime_paths: RuntimeWorkspacePaths
    recovery: SessionExecutionRecovery | None = None
    surface: EffectiveSurfaceSnapshot | None = None
    # Host-only admission provenance; never populated from request metadata.
    trusted_subject_id: str | None = None

    @property
    def provider_id(self) -> str:
        return self.bound.binding.provider_id

    @property
    def cache_identity(self) -> tuple[str, ...]:
        """Complete host and Provider identity for one External root."""
        return (self.channel_id, *self.bound.binding.cache_key)


def bind_admitted_request_execution(
    agent_manager: Any,
    request: Any,
    project_dir: str | None,
    *,
    session_metadata: dict[str, Any] | None = None,
    trusted_subject_id: str | None = None,
) -> AdmittedExecutionRoute | None:
    """Pin the server-owned selection before an Agent or MCP child is built.

    A chat request cannot install raw provider settings or switch an existing
    Session. The product admission callback has already authorized its
    subject, mode and Workspace before this function is called.
    """
    if trusted_subject_id is not None and not trusted_subject_id.strip():
        raise ValueError("Trusted execution subject must not be empty")
    session_id = str(getattr(request, "session_id", "") or "").strip()
    if not session_id:
        return None
    if session_metadata is None:
        from jiuwenswarm.server.runtime.session.session_metadata import (
            get_session_metadata,
        )

        session_metadata = get_session_metadata(
            session_id, cache_bust=True, enable_writeback=False, infer_defaults=False
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
        return None

    from jiuwenswarm.common.config import get_config
    from jiuwenswarm.common.utils import get_agent_workspace_dir
    from jiuwenswarm.runtime.harness.config_source import load_execution_catalog

    catalog = load_execution_catalog(get_config(), selected_profile_id=selected_profile_id)
    if catalog is None:
        raise ExecutionRecoveryUnavailableError(
            "execution profile is no longer configured"
        )
    try:
        source = catalog.source(explicit_profile_id=selected_profile_id)
    except ValueError as exc:
        raise ExecutionRecoveryUnavailableError(
            "execution profile is no longer configured"
        ) from exc
    spec = source.resolve()
    if spec.config_revision != session_metadata.get("execution_config_revision"):
        raise ExecutionRecoveryUnavailableError(
            "execution configuration revision changed"
        )
    from jiuwenswarm.runtime.harness.config_source import source_for_bound_fingerprint

    try:
        source = source_for_bound_fingerprint(
            source, session_metadata.get("execution_config_fingerprint"),
            allow_runtime_authorization=not str(session_metadata.get("mode", "")).startswith("team"),
        )
        spec = source.resolve()
    except ValueError as exc:
        raise ExecutionRecoveryUnavailableError(str(exc)) from exc
    mode = None
    if spec.provider_id != "native":
        mode = validate_surface_request(session_metadata, params or {})
        if mode.startswith("team.") and not session_metadata.get("team_name"):
            from jiuwenswarm.runtime.harness.surface import SurfaceAdmissionError
            raise SurfaceAdmissionError("Team identity is not assigned yet")
    runtime_paths = bind_session_runtime_workspace(
        internal_workspace_dir=get_agent_workspace_dir(),
        project_dir=project_dir,
        session_id=session_id,
    )
    channel_id = str(getattr(request, "channel_id", "") or "").strip() or "default"
    subject = str(
        trusted_subject_id
        or getattr(request, "user_id", "")
        or session_metadata.get("user_id")
        or f"{channel_id}:{session_id}"
    ).strip()
    from jiuwenswarm.runtime.harness.installed_codex import materialize_codex_source

    source = materialize_codex_source(
        source, profile_id=selected_profile_id, paths=runtime_paths,
        subject_id=subject, session_id=session_id,
    )
    spec = source.resolve()
    prospective_binding = ExecutionBinding.create(
        spec,
        subject_id=subject,
        host_session_id=session_id,
        workspace=str(runtime_paths.runtime_workspace_root),
    )
    # A live Session remains authoritative even if its recovery file vanished.
    # Reject a changed scope before creating an archive or inserting a binding.
    remembered = getattr(agent_manager, "_session_execution_bindings", None)
    current = remembered.get((channel_id, session_id)) if isinstance(remembered, dict) else None
    if current is not None and current.cache_key != prospective_binding.cache_key:
        raise ExecutionRecoveryUnavailableError("execution Binding changed")
    surface = None
    if mode is not None:
        surface_metadata = session_metadata
        if trusted_subject_id is not None:
            # validate_surface_request above already checked the persisted routing
            # identity against surface_creation. Bind its execution scope to the
            # host identity without rewriting history/UI user_id. Recovery and
            # the live Binding still reject an existing execution subject change.
            surface_metadata = {**session_metadata, "user_id": subject}
            creation = session_metadata.get("surface_creation")
            if isinstance(creation, dict):
                surface_metadata["surface_creation"] = {**creation, "user_id": subject}
        identity = build_surface_identity(
            metadata=surface_metadata, binding=prospective_binding,
            paths=runtime_paths, channel_id=channel_id,
        )
        surface = EffectiveSurfaceSnapshot(identity, mode)
        # Product context and Provider permissions are compiled by the
        # adapter after the current host policy switches are known.
        surface.validate_mode(mode, require_policy=False, topology=identity.topology)

    recovery = SessionExecutionRecovery(
        session_id=session_id,
        execution_profile_id=selected_profile_id,
        binding=prospective_binding,
        runtime_paths=runtime_paths,
        surface_identity=surface.identity if surface else None,
    )
    bindings = agent_manager.execution_bindings
    bound = bindings.bind(
        source,
        subject_id=subject,
        host_session_id=session_id,
        workspace=str(runtime_paths.runtime_workspace_root),
    )
    route = AdmittedExecutionRoute(
        channel_id=channel_id,
        source=source,
        bindings=bindings,
        bound=bound,
        runtime_paths=runtime_paths,
        recovery=recovery,
        surface=surface,
        trusted_subject_id=subject if trusted_subject_id is not None else None,
    )
    setattr(request, "_bound_execution", bound)
    setattr(request, "_execution_source", source)
    setattr(request, "_execution_bindings", bindings)
    setattr(request, "_runtime_workspace_paths", runtime_paths)
    setattr(request, "_execution_route", route)
    remember = getattr(agent_manager, "remember_execution_binding", None)
    if callable(remember):
        remember(channel_id, session_id, bound.binding)
    return route
