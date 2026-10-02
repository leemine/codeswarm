# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Trusted product subagent profile catalog for External providers."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType


GENERAL_PURPOSE_SUBAGENT_TYPE = "general-purpose"
BROWSER_SUBAGENT_TYPE = "browser_agent"
RESEARCH_SUBAGENT_TYPE = "research_agent"


class ExternalSubagentExecutionKind(str, Enum):
    """Host execution adapter required by a product subagent profile."""

    GENERIC = "generic"
    BROWSER = "browser"


@dataclass(frozen=True, slots=True)
class ExternalSubagentProfile:
    """Trusted host-owned behavior for one accepted ``subagent_type``."""

    subagent_type: str
    execution_kind: ExternalSubagentExecutionKind
    description: str = ""
    advertised: bool = False
    compatibility_alias: bool = False


class ExternalSubagentProfileUnavailableError(ValueError):
    """Raised before side effects when a profile has no admitted adapter."""


_LEGACY_GENERIC_ALIASES = (
    "code_agent",
    "explore_agent",
    "mobile_gui_agent",
    "plan_agent",
    "verification_agent",
)


_profile_catalog = {
    GENERAL_PURPOSE_SUBAGENT_TYPE: ExternalSubagentProfile(
        subagent_type=GENERAL_PURPOSE_SUBAGENT_TYPE,
        execution_kind=ExternalSubagentExecutionKind.GENERIC,
        description=(
            "same Provider, fixed parent configuration and workspace; "
            "display_name and role describe the delegated assignment"
        ),
        advertised=True,
    ),
    RESEARCH_SUBAGENT_TYPE: ExternalSubagentProfile(
        subagent_type=RESEARCH_SUBAGENT_TYPE,
        execution_kind=ExternalSubagentExecutionKind.GENERIC,
        description=(
            "Work evidence research using the same Provider and admitted workspace; "
            "scope, inspect sources, compare evidence and return a cited report"
        ),
        compatibility_alias=True,
    ),
    BROWSER_SUBAGENT_TYPE: ExternalSubagentProfile(
        subagent_type=BROWSER_SUBAGENT_TYPE,
        execution_kind=ExternalSubagentExecutionKind.BROWSER,
        description=(
            "same Provider Browser child with one host-managed Profile, "
            "identity-bound Browser runtime and admitted capability set"
        ),
        advertised=False,
    ),
}
for _subagent_type in _LEGACY_GENERIC_ALIASES:
    _profile_catalog[_subagent_type] = ExternalSubagentProfile(
        subagent_type=_subagent_type,
        execution_kind=ExternalSubagentExecutionKind.GENERIC,
        compatibility_alias=True,
    )

EXTERNAL_SUBAGENT_PROFILES = MappingProxyType(_profile_catalog)


def resolve_external_subagent_profile(
    subagent_type: object,
) -> ExternalSubagentProfile:
    """Resolve an exact trusted profile name without starting child resources."""

    if not isinstance(subagent_type, str) or not subagent_type.strip():
        raise ValueError("External subagent type is required")
    normalized = subagent_type.strip()
    profile = EXTERNAL_SUBAGENT_PROFILES.get(normalized)
    if profile is None:
        raise ValueError(f"Unsupported External subagent type: {normalized}")
    return profile


def validate_external_subagent_request(
    *,
    subagent_type: object,
    browser_capabilities: tuple[str, ...] | None,
    browser_available: bool = False,
) -> ExternalSubagentProfile:
    """Validate profile-specific arguments before child resource allocation."""

    profile = resolve_external_subagent_profile(subagent_type)
    if profile.execution_kind is ExternalSubagentExecutionKind.BROWSER:
        if not browser_available:
            raise ExternalSubagentProfileUnavailableError(
                "browser_agent requires a host Browser admission adapter"
            )
        from openjiuwen.harness.tools.browser_move.playwright_runtime.browser_capabilities import (
            resolve_browser_capabilities,
        )

        resolved = resolve_browser_capabilities(browser_capabilities)
        if resolved.rejected_names:
            raise ValueError(
                "Unsupported Browser capabilities: "
                + ", ".join(resolved.rejected_names)
            )
        return profile
    if browser_capabilities is not None:
        raise ValueError(
            "browser_capabilities are only valid for the dedicated browser_agent"
        )
    return profile


def render_external_subagent_catalog(
    *,
    browser_available: bool = False,
    work_research_enabled: bool = False,
) -> str:
    """Render only profiles that the current product runtime can construct."""

    return "\n".join(
        f"- {profile.subagent_type}: {profile.description}"
        for profile in EXTERNAL_SUBAGENT_PROFILES.values()
        if profile.advertised
        or (work_research_enabled and profile.subagent_type == RESEARCH_SUBAGENT_TYPE)
        or (
            browser_available
            and profile.execution_kind is ExternalSubagentExecutionKind.BROWSER
        )
    )


__all__ = [
    "BROWSER_SUBAGENT_TYPE",
    "EXTERNAL_SUBAGENT_PROFILES",
    "GENERAL_PURPOSE_SUBAGENT_TYPE",
    "RESEARCH_SUBAGENT_TYPE",
    "ExternalSubagentExecutionKind",
    "ExternalSubagentProfile",
    "ExternalSubagentProfileUnavailableError",
    "resolve_external_subagent_profile",
    "render_external_subagent_catalog",
    "validate_external_subagent_request",
]
