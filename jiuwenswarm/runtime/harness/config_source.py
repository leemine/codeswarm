# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Host-owned execution selection; model-provider settings are separate."""
from dataclasses import dataclass, replace
from collections.abc import Mapping
from types import MappingProxyType

from openjiuwen.harness.engine import resolve_execution_spec
from openjiuwen.harness_protocol import AgentExecutionSpec, ExecutionAuthorization
from openjiuwen.harness_providers.construction import apply_legacy_full_access


def parse_execution_config(value: Mapping[str, object]) -> AgentExecutionSpec:
    """Parse only the dedicated execution config, never model settings."""
    allowed = {"provider_id", "config_revision", "requested_mode", "provider_config", "authorization"}
    if not isinstance(value, Mapping):
        raise TypeError("execution configuration must be an object")
    if set(value) - allowed:
        raise ValueError("unknown execution configuration fields")
    if not {"provider_id", "config_revision"} <= set(value):
        raise ValueError("execution provider_id and config_revision are required")
    values = dict(value)
    authorization = values.get("authorization")
    if authorization is not None:
        if not isinstance(authorization, Mapping) or set(authorization) != {"full_access"}:
            raise ValueError("execution authorization must contain only full_access")
        values["authorization"] = ExecutionAuthorization(**dict(authorization))
    return AgentExecutionSpec(**values)


@dataclass(frozen=True, slots=True)
class ExecutionConfigSource:
    """Complete snapshots; project is supplied only for a project request."""

    explicit: AgentExecutionSpec | None = None
    project: AgentExecutionSpec | None = None
    default: AgentExecutionSpec | None = None

    def resolve(self) -> AgentExecutionSpec:
        return resolve_execution_spec(explicit=self.explicit, project=self.project, default=self.default)


class ExecutionConfigCatalog:
    """Resolve public selection IDs against server-owned execution snapshots.

    The caller must authorize a project choice before passing its profile ID.
    Raw provider configuration from a chat request never enters this catalog.
    """

    def __init__(
        self,
        profiles: Mapping[str, Mapping[str, object]],
        *,
        default_profile_id: str,
        host_authorization: ExecutionAuthorization | None = None,
    ) -> None:
        if not isinstance(profiles, Mapping) or not profiles:
            raise ValueError("execution profiles must be a non-empty mapping")
        snapshots: dict[str, AgentExecutionSpec] = {}
        for profile_id, value in profiles.items():
            if not isinstance(profile_id, str) or not profile_id.strip() or profile_id != profile_id.strip():
                raise ValueError("execution profile IDs must be normalized strings")
            spec = parse_execution_config(value)
            if host_authorization is not None:
                if spec.authorization is not None:
                    spec = replace(spec, authorization=host_authorization)
                elif host_authorization.full_access:
                    spec = apply_legacy_full_access(spec)
            snapshots[profile_id] = spec
        if default_profile_id not in snapshots:
            raise ValueError("default execution profile is not configured")
        self._profiles = MappingProxyType(snapshots)
        self._default_profile_id = default_profile_id

    @property
    def profile_ids(self) -> tuple[str, ...]:
        """List selectable identifiers without exposing provider configuration."""
        return tuple(self._profiles)

    @property
    def default_profile_id(self) -> str:
        return self._default_profile_id

    def source(
        self,
        *,
        explicit_profile_id: str | None = None,
        project_profile_id: str | None = None,
    ) -> ExecutionConfigSource:
        """Return frozen candidates; unknown explicit choices never fall back."""
        def selected(profile_id: str | None) -> AgentExecutionSpec | None:
            if profile_id is None:
                return None
            if not isinstance(profile_id, str) or profile_id not in self._profiles:
                raise ValueError("unknown execution profile ID")
            return self._profiles[profile_id]

        return ExecutionConfigSource(
            explicit=selected(explicit_profile_id),
            project=selected(project_profile_id),
            default=self._profiles[self._default_profile_id],
        )


# These stable IDs name trusted built-in recipes, never request-supplied config.
INSTALLED_EXECUTION_PROFILES = MappingProxyType({
    "builtin:opencode": "opencode",
    "builtin:codex": "codex",
})


def installed_execution_profiles() -> tuple[str, ...]:
    """Discover executable presence only; never start a CLI or inspect login."""
    from shutil import which
    return tuple(profile for profile, executable in INSTALLED_EXECUTION_PROFILES.items() if which(executable))


def load_execution_catalog(
    config: Mapping[str, object],
    *, selected_profile_id: str | None = None,
) -> ExecutionConfigCatalog | None:
    """Load the optional execution section of an already resolved server config.

    Absence keeps legacy sessions on their existing path. A present but broken
    section is an error, so a typo cannot silently select another engine.
    """
    if not isinstance(config, Mapping):
        raise TypeError("server configuration must be an object")
    if selected_profile_id in INSTALLED_EXECUTION_PROFILES:
        # Explicit, host-owned defaults do not replace configured profiles or
        # the legacy Native default. Resolution is independent of installation:
        # a removed CLI must fail at startup, not relabel an existing Session.
        from jiuwenswarm.governance.organization_auth import configured_authenticator
        if configured_authenticator() is not None:
            raise ValueError("installed engine defaults require a personal environment")
        if "execution" in config:
            existing = load_execution_catalog(config)
            if selected_profile_id in existing.profile_ids:
                return existing
            if any(existing.source(explicit_profile_id=profile).resolve().provider_id
                   == INSTALLED_EXECUTION_PROFILES[selected_profile_id] for profile in existing.profile_ids):
                raise ValueError("use the configured execution profile for this engine")
        provider = INSTALLED_EXECUTION_PROFILES[selected_profile_id]
        permissions = config.get("permissions")
        enabled = permissions.get("enabled") if isinstance(permissions, Mapping) else None
        provider_config = {}
        if provider == "opencode":
            from jiuwenswarm.common.utils import get_agent_workspace_dir
            provider_config["runtime_root"] = str(get_agent_workspace_dir().resolve() / "opencode-runtime")
            # The OpenCode adapter requires an explicit model in its isolated
            # process. Reuse the host's personal model decoder/default selector.
            # Unsupported model transports remain a startup error, not fallback.
            from jiuwenswarm.common.config import get_default_models
            entries = get_default_models(dict(config))
            entry = next((item for item in entries if item.get("is_default")), entries[0] if entries else {})
            client = entry.get("model_client_config", {})
            if (client.get("client_provider") == "OpenAI" and client.get("model_name")
                    and client.get("api_base") and not client.get("custom_headers")
                    and client.get("auth_mode", "api_key") in (None, "", "api_key")
                    and client.get("api_mode", "chat_completions") in (None, "", "chat_completions")
                    and not str(client.get("api_key", "")).startswith("jiuwen-login:")):
                provider_config["model"] = {
                    "model": client["model_name"], "api_base": client["api_base"],
                    "api_key": client.get("api_key") or None,
                }
        return ExecutionConfigCatalog({selected_profile_id: {
            "provider_id": provider,
            "config_revision": "installed-engine-v1",
            "provider_config": provider_config,
            "authorization": {"full_access": enabled is False},
        }}, default_profile_id=selected_profile_id)
    if "execution" not in config:
        return None
    section = config["execution"]
    if not isinstance(section, Mapping):
        raise TypeError("execution configuration must be an object")
    profiles = section.get("profiles")
    default_profile_id = section.get("default_profile_id")
    if not isinstance(default_profile_id, str):
        raise ValueError("execution default_profile_id is required")
    permissions = config.get("permissions")
    enabled = permissions.get("enabled") if isinstance(permissions, Mapping) else None
    authorization = ExecutionAuthorization(full_access=not enabled) if isinstance(enabled, bool) else None
    return ExecutionConfigCatalog(
        profiles,
        default_profile_id=default_profile_id,
        host_authorization=authorization,
    )


def source_for_bound_fingerprint(
    source: ExecutionConfigSource, fingerprint: str, *, allow_runtime_authorization: bool = True
) -> ExecutionConfigSource:
    """Recover construction identity only when the sole delta is explicit host authorization.

    This does not apply permissions. The live Runtime must confirm the current
    host decision before admitting another Turn. Provider payload/revision/mode
    are included in both candidate hashes and cannot be waived by this path.
    Legacy vendor flags and adding/removing explicit authorization remain strict.
    """
    from openjiuwen.harness.engine.config import config_fingerprint

    spec = source.resolve()
    if config_fingerprint(spec) == fingerprint:
        return source
    if allow_runtime_authorization and spec.authorization is not None and spec.provider_id in {"opencode", "codex"}:
        original = replace(spec, authorization=ExecutionAuthorization(not spec.authorization.full_access))
        if config_fingerprint(original) == fingerprint:
            return ExecutionConfigSource(explicit=original)
    raise ValueError("execution configuration fingerprint changed")
