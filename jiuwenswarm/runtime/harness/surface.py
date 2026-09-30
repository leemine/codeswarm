# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Host Surface identity; Provider authorization remains in ExecutionBinding.

This A slice carries identity and execution state only. Context, capability and
sandbox compilation belong to subsequent slices and are not implied by a mode.
"""

from __future__ import annotations

from dataclasses import dataclass, fields, replace
from typing import TYPE_CHECKING, Any, Mapping
from pathlib import Path

from openjiuwen.harness.engine import ExecutionBinding
from openjiuwen.harness_protocol import (
    ExecutionAuthorization,
    HarnessRuntimePolicy,
    RuntimeExecutionState,
    RuntimeSurface,
    SourceDiscovery,
    WorkspaceAccess,
)

from jiuwenswarm.common.mode_matrix import (
    compose_web_mode,
    deprecate_mode,
    is_new_canonical_mode,
)
from jiuwenswarm.common.runtime_workspace import RuntimeWorkspacePaths

if TYPE_CHECKING:
    from jiuwenswarm.runtime.harness.capability_catalog import (
        EffectiveCapabilityCatalog,
    )
    from jiuwenswarm.runtime.harness.ui_capability_manifest import (
        UICapabilityManifest,
    )


class SurfaceAdmissionError(ValueError):
    """A Session cannot safely use the requested Surface."""


def canonical_surface_mode(metadata: Mapping[str, Any]) -> str:
    raw = metadata.get("mode")
    if not isinstance(raw, str) or not raw.strip():
        raise SurfaceAdmissionError(
            "Surface mode is missing or ambiguous; new Session required"
        )
    mode = deprecate_mode(raw)
    work_mode = metadata.get("work_mode")
    if work_mode is not None and work_mode not in ("work", "code"):
        raise SurfaceAdmissionError("Surface work_mode is invalid")
    # Historical Web clients composed agent/agent.plan/team with work_mode.
    # Three-part persisted canonical modes are already authoritative.
    if raw in ("agent", "agent.plan", "team") and work_mode:
        composed = compose_web_mode(raw, work_mode)
        if composed:
            mode = deprecate_mode(composed[2])
    if not is_new_canonical_mode(mode):
        raise SurfaceAdmissionError(
            "Surface mode is missing or ambiguous; new Session required"
        )
    if work_mode and mode.split(".")[1] != work_mode:
        raise SurfaceAdmissionError("Surface mode conflicts with work_mode")
    return mode


def creation_surface(metadata: Mapping[str, Any]) -> dict[str, Any]:
    """Capture creation inputs in existing metadata before any Provider starts."""
    mode = canonical_surface_mode(metadata)
    return {
        "schema_version": 1,
        "creation_mode": mode,
        **{
            key: metadata.get(key) or ""
            for key in (
                "session_id",
                "channel_id",
                "user_id",
                "project_id",
                "project_dir",
                "team_name",
                "execution_profile_id",
                "execution_config_revision",
                "execution_config_fingerprint",
            )
        },
    }


def validate_surface_metadata(metadata: Mapping[str, Any]) -> str:
    mode = canonical_surface_mode(metadata)
    stored = metadata.get("surface_creation")
    if stored is not None:
        expected = creation_surface(metadata)
        if not isinstance(stored, dict) or not is_new_canonical_mode(
            stored.get("creation_mode")
        ):
            raise SurfaceAdmissionError("Surface creation identity is invalid")
        if stored["creation_mode"].rsplit(".", 1)[0] != mode.rsplit(".", 1)[0]:
            raise SurfaceAdmissionError(
                "Surface identity changed; new Session required"
            )
        expected["creation_mode"] = stored["creation_mode"]
        if stored != expected:
            raise SurfaceAdmissionError(
                "Surface creation identity changed; new Session required"
            )
    return mode


def validate_surface_request(
    metadata: Mapping[str, Any], params: Mapping[str, Any]
) -> str:
    mode = validate_surface_metadata(metadata)
    work_mode = params.get("work_mode")
    if work_mode is not None and work_mode != mode.split(".")[1]:
        raise SurfaceAdmissionError(
            "Surface work_mode is immutable; new Session required"
        )
    for field in ("project_id", "project_dir"):
        requested = params.get(field)
        stored = metadata.get(field) or ""
        if requested:
            matches = (
                Path(requested).expanduser().resolve()
                == Path(stored).expanduser().resolve()
                if field == "project_dir" and stored
                else requested == stored
            )
            if not matches:
                raise SurfaceAdmissionError(
                    "Surface project is immutable; new Session required"
                )
    if params.get("mode"):
        requested = canonical_surface_mode(
            {"mode": params["mode"], "work_mode": mode.split(".")[1]}
        )
        if requested.rsplit(".", 1)[0] != mode.rsplit(".", 1)[0]:
            raise SurfaceAdmissionError(
                "Surface topology is immutable; new Session required"
            )
        return requested
    return mode


@dataclass(frozen=True, slots=True)
class SessionSurfaceIdentity:
    """Persisted host fields complement, never replace, the Provider Binding."""

    binding: ExecutionBinding
    paths: RuntimeWorkspacePaths
    channel_id: str
    project_id: str
    team_name: str
    execution_profile_id: str
    work_mode: str
    topology: str
    creation_mode: str

    def record(self) -> dict[str, Any]:
        # Binding and runtime paths already have their own recovery codecs.
        return {
            field.name: getattr(self, field.name)
            for field in fields(self)
            if field.name not in ("binding", "paths")
        }


def build_surface_identity(
    *,
    metadata: Mapping[str, Any],
    binding: ExecutionBinding,
    paths: RuntimeWorkspacePaths,
    channel_id: str,
) -> SessionSurfaceIdentity:
    mode = validate_surface_metadata(metadata)
    if metadata.get("session_id") not in (None, "", binding.host_session_id):
        raise SurfaceAdmissionError("Surface belongs to another Session")
    if metadata.get("channel_id") not in (None, "", channel_id):
        raise SurfaceAdmissionError("Surface channel changed")
    if metadata.get("user_id") not in (None, "", binding.subject_id):
        raise SurfaceAdmissionError("Surface subject changed")
    root = paths.runtime_workspace_root.resolve()
    if str(root) != binding.workspace or paths.project_root.resolve() != root:
        raise SurfaceAdmissionError("Surface workspace differs from Binding")
    for path in (paths.cwd, paths.outputs_dir):
        if path is not None and not path.resolve().is_relative_to(root):
            raise SurfaceAdmissionError("Surface cwd/outputs escape the workspace")
    declared_root = metadata.get("project_dir")
    if (
        declared_root
        and Path(declared_root).expanduser().resolve() != paths.project_root
    ):
        raise SurfaceAdmissionError("Surface project workspace changed")
    stored = metadata.get("surface_creation")
    # A legacy archive cannot recover the original normal/plan state. Preserve
    # only its unambiguous Surface; execution state is deliberately not identity.
    creation_mode = (
        stored["creation_mode"] if stored else mode.rsplit(".", 1)[0] + ".normal"
    )
    return SessionSurfaceIdentity(
        binding,
        paths,
        channel_id,
        str(metadata.get("project_id") or ""),
        str(metadata.get("team_name") or ""),
        str(metadata.get("execution_profile_id") or ""),
        mode.split(".")[1],
        "team" if mode.startswith("team.") else "single",
        creation_mode,
    )


def validate_external_surface_state(
    mode: str,
    runtime_policy: HarnessRuntimePolicy | None = None,
    *,
    require_policy: bool = True,
) -> None:
    if mode.startswith("team."):
        raise SurfaceAdmissionError(
            "External Team Surface requires the Team integration"
        )
    if require_policy and runtime_policy is None:
        raise SurfaceAdmissionError("External Surface runtime policy is not compiled")
    if runtime_policy is None:
        return
    if runtime_policy.surface.value != mode.split(".")[1]:
        raise SurfaceAdmissionError("External Surface runtime policy changed Surface")
    state = mode.rsplit(".", 1)[1]
    if runtime_policy.execution_state.value != state:
        raise SurfaceAdmissionError("External Surface runtime policy changed execution state")
    if state == "plan" and runtime_policy.workspace_access is not WorkspaceAccess.READ_ONLY:
        raise SurfaceAdmissionError("External Plan Surface must be read-only")


@dataclass(frozen=True, slots=True)
class EffectiveSurfaceSnapshot:
    identity: SessionSurfaceIdentity
    initial_mode: str
    policy_revision: str = "surface-identity-v1"
    runtime_policy: HarnessRuntimePolicy | None = None
    capability_catalog: "EffectiveCapabilityCatalog | None" = None
    ui_capability_manifest: "UICapabilityManifest | None" = None

    def validate_mode(self, mode: str, *, require_policy: bool = True) -> None:
        canonical = canonical_surface_mode(
            {"mode": mode, "work_mode": self.identity.work_mode}
        )
        if canonical.rsplit(".", 1)[0] != self.initial_mode.rsplit(".", 1)[0]:
            raise SurfaceAdmissionError(
                "Surface identity changed; new Session required"
            )
        validate_external_surface_state(
            canonical,
            self.runtime_policy,
            require_policy=require_policy,
        )


_SURFACE_POLICY_REVISION = "r1-11b-v1"

_SURFACE_REQUIREMENTS = {
    "work": {
        "memory_sources": ("product_history", "project_memory"),
        "required_capabilities": (
            "documents", "web", "artifacts", "browser", "subagents", "memory",
        ),
        "artifact_kinds": ("document", "web", "media", "file"),
    },
    "code": {
        "memory_sources": ("product_history", "project_memory", "coding_memory"),
        "required_capabilities": (
            "filesystem", "terminal", "git", "diff", "test", "review", "lsp",
            "browser", "subagents", "memory",
        ),
        "artifact_kinds": ("file_change", "diff", "test", "review", "terminal"),
    },
}


def compile_surface_policy(
    snapshot: EffectiveSurfaceSnapshot,
    *,
    authorization: ExecutionAuthorization,
    include_personal_context: bool,
) -> EffectiveSurfaceSnapshot:
    """Compile one cold-start policy without changing Session identity."""

    mode = canonical_surface_mode(
        {"mode": snapshot.initial_mode, "work_mode": snapshot.identity.work_mode}
    )
    validate_external_surface_state(mode, require_policy=False)
    state = RuntimeExecutionState(mode.rsplit(".", 1)[1])
    access = (
        WorkspaceAccess.READ_ONLY
        if state is RuntimeExecutionState.PLAN
        else WorkspaceAccess.FULL_ACCESS
        if authorization.full_access
        else WorkspaceAccess.WORKSPACE_WRITE
    )
    requirements = _SURFACE_REQUIREMENTS[snapshot.identity.work_mode]
    context_sources = ["project_rules", "authorized_attachments"]
    memory_sources = list(requirements["memory_sources"])
    if include_personal_context:
        context_sources.append("personal_context")
        memory_sources.append("personal_context")
    policy = HarnessRuntimePolicy(
        revision=_SURFACE_POLICY_REVISION,
        surface=RuntimeSurface(snapshot.identity.work_mode),
        execution_state=state,
        workspace_access=access,
        context_sources=tuple(context_sources),
        memory_sources=tuple(memory_sources),
        required_capabilities=requirements["required_capabilities"],
        artifact_kinds=requirements["artifact_kinds"],
        source_discovery=SourceDiscovery.EXPLICIT_ONLY,
    )
    return replace(
        snapshot,
        policy_revision=policy.revision,
        runtime_policy=policy,
    )
