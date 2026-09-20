# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Host-owned execution selection; model-provider settings are separate."""
from dataclasses import dataclass
from collections.abc import Mapping
from types import MappingProxyType

from openjiuwen.harness.engine import resolve_execution_spec
from openjiuwen.harness_protocol import AgentExecutionSpec


def parse_execution_config(value: Mapping[str, object]) -> AgentExecutionSpec:
    """Parse only the dedicated execution config, never model settings."""
    allowed = {"provider_id", "config_revision", "requested_mode", "provider_config"}
    if not isinstance(value, Mapping):
        raise TypeError("execution configuration must be an object")
    if set(value) - allowed:
        raise ValueError("unknown execution configuration fields")
    if not {"provider_id", "config_revision"} <= set(value):
        raise ValueError("execution provider_id and config_revision are required")
    return AgentExecutionSpec(**dict(value))


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
    ) -> None:
        if not isinstance(profiles, Mapping) or not profiles:
            raise ValueError("execution profiles must be a non-empty mapping")
        snapshots: dict[str, AgentExecutionSpec] = {}
        for profile_id, value in profiles.items():
            if not isinstance(profile_id, str) or not profile_id.strip() or profile_id != profile_id.strip():
                raise ValueError("execution profile IDs must be normalized strings")
            snapshots[profile_id] = parse_execution_config(value)
        if default_profile_id not in snapshots:
            raise ValueError("default execution profile is not configured")
        self._profiles = MappingProxyType(snapshots)
        self._default_profile_id = default_profile_id

    @property
    def profile_ids(self) -> tuple[str, ...]:
        """List selectable identifiers without exposing provider configuration."""
        return tuple(self._profiles)

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


def load_execution_catalog(
    config: Mapping[str, object],
) -> ExecutionConfigCatalog | None:
    """Load the optional execution section of an already resolved server config.

    Absence keeps legacy sessions on their existing path. A present but broken
    section is an error, so a typo cannot silently select another engine.
    """
    if not isinstance(config, Mapping):
        raise TypeError("server configuration must be an object")
    if "execution" not in config:
        return None
    section = config["execution"]
    if not isinstance(section, Mapping):
        raise TypeError("execution configuration must be an object")
    profiles = section.get("profiles")
    default_profile_id = section.get("default_profile_id")
    if not isinstance(default_profile_id, str):
        raise ValueError("execution default_profile_id is required")
    return ExecutionConfigCatalog(
        profiles,
        default_profile_id=default_profile_id,
    )
