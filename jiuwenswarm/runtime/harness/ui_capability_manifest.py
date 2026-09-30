# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Provider-neutral UI projection of one effective Surface capability catalog."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import Enum

from openjiuwen.harness_protocol import RuntimeSurface

from jiuwenswarm.runtime.harness.capability_catalog import (
    CapabilityAvailability,
    CapabilityKind,
    CapabilityUnavailableReason,
    EffectiveCapability,
    EffectiveCapabilityCatalog,
)


class UICapabilityState(str, Enum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    NEEDS_INSTALL = "needs_install"
    NEEDS_AUTH = "needs_auth"
    DEGRADED = "degraded"
    NOT_APPLICABLE = "not_applicable"


_UI_CAPABILITY_AREAS = (
    "documents",
    "web",
    "artifacts",
    "filesystem",
    "terminal",
    "git",
    "diff",
    "test",
    "review",
    "lsp",
    "browser",
    "subagents",
    "memory",
)

_SURFACE_AREAS = {
    RuntimeSurface.WORK: frozenset(
        {"documents", "web", "artifacts", "browser", "subagents", "memory"}
    ),
    RuntimeSurface.CODE: frozenset(
        {
            "filesystem",
            "terminal",
            "git",
            "diff",
            "test",
            "review",
            "lsp",
            "browser",
            "subagents",
            "memory",
        }
    ),
}


@dataclass(frozen=True, slots=True)
class UICapability:
    capability_id: str
    state: UICapabilityState
    reason_code: str = ""
    reason: str = ""
    requires_authorization: bool = False

    def __post_init__(self) -> None:
        if self.capability_id not in _UI_CAPABILITY_AREAS:
            raise ValueError("unknown UI capability area")
        if not isinstance(self.state, UICapabilityState):
            raise TypeError("UI capability state must be UICapabilityState")
        if self.state in {
            UICapabilityState.AVAILABLE,
            UICapabilityState.NOT_APPLICABLE,
        } and (self.reason_code or self.reason):
            raise ValueError("available/not-applicable capability cannot carry a reason")
        if self.state not in {
            UICapabilityState.AVAILABLE,
            UICapabilityState.NOT_APPLICABLE,
        } and (not self.reason_code or not self.reason):
            raise ValueError("non-available UI capability requires a reason")
        if not isinstance(self.requires_authorization, bool):
            raise TypeError("requires_authorization must be boolean")

    def record(self) -> dict[str, object]:
        return {
            "id": self.capability_id,
            "state": self.state.value,
            "reason_code": self.reason_code,
            "reason": self.reason,
            "requires_authorization": self.requires_authorization,
        }


@dataclass(frozen=True, slots=True)
class UICapabilityManifest:
    provider_id: str
    surface: RuntimeSurface
    state: UICapabilityState
    entries: tuple[UICapability, ...]
    restart_required: bool = False
    schema_version: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.provider_id, str) or not self.provider_id.strip():
            raise ValueError("UI capability manifest requires provider_id")
        if not isinstance(self.surface, RuntimeSurface):
            raise TypeError("UI capability manifest surface must be RuntimeSurface")
        if self.state not in {
            UICapabilityState.AVAILABLE,
            UICapabilityState.DEGRADED,
            UICapabilityState.UNAVAILABLE,
        }:
            raise ValueError("UI capability manifest has an invalid aggregate state")
        if self.schema_version != 1:
            raise ValueError("unsupported UI capability manifest schema")
        if not isinstance(self.restart_required, bool):
            raise TypeError("restart_required must be boolean")
        entries = tuple(self.entries)
        if any(not isinstance(item, UICapability) for item in entries):
            raise TypeError("UI capability manifest contains an invalid entry")
        if tuple(item.capability_id for item in entries) != _UI_CAPABILITY_AREAS:
            raise ValueError("UI capability manifest must contain every area in order")

    def record(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "provider_id": self.provider_id,
            "surface": self.surface.value,
            "state": self.state.value,
            "restart_required": self.restart_required,
            "entries": [entry.record() for entry in self.entries],
        }

    @property
    def fingerprint(self) -> str:
        payload = json.dumps(
            self.record(), sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode()
        return hashlib.sha256(payload).hexdigest()


def _entry_state(entry: EffectiveCapability) -> UICapabilityState:
    if entry.availability is CapabilityAvailability.AVAILABLE:
        return UICapabilityState.AVAILABLE
    if entry.unavailable_reason is CapabilityUnavailableReason.NOT_INSTALLED:
        return UICapabilityState.NEEDS_INSTALL
    return UICapabilityState.UNAVAILABLE


def _category_entry(
    capability_id: str,
    *,
    applicable: bool,
    categories: dict[str, EffectiveCapability],
) -> UICapability:
    if not applicable:
        return UICapability(capability_id, UICapabilityState.NOT_APPLICABLE)
    capability = categories.get(capability_id.casefold())
    if capability is None:
        return UICapability(
            capability_id,
            UICapabilityState.UNAVAILABLE,
            "manifest_category_missing",
            "The effective capability catalog omitted this required Surface area.",
        )
    state = _entry_state(capability)
    return UICapability(
        capability_id,
        state,
        capability.unavailable_reason.value if capability.unavailable_reason else "",
        capability.reason,
        capability.requires_authorization,
    )


def compile_ui_capability_manifest(
    catalog: EffectiveCapabilityCatalog,
    *,
    restart_required: bool = False,
) -> UICapabilityManifest:
    """Project one authoritative catalog into stable product UI areas.

    ``requires_authorization`` deliberately remains orthogonal to availability:
    per-action approval is rendered by the existing AuthorizationPrompt and is
    not a persistent capability failure.
    """

    categories = {
        entry.name.casefold(): entry
        for entry in catalog.entries
        if entry.kind is CapabilityKind.CATEGORY
    }
    applicable = _SURFACE_AREAS[catalog.surface]
    entries = tuple(
        _category_entry(
            capability_id,
            applicable=capability_id in applicable,
            categories=categories,
        )
        for capability_id in _UI_CAPABILITY_AREAS
    )
    active = [entry for entry in entries if entry.state is not UICapabilityState.NOT_APPLICABLE]
    unavailable = [entry for entry in active if entry.state is not UICapabilityState.AVAILABLE]
    state = (
        UICapabilityState.AVAILABLE
        if not unavailable
        else UICapabilityState.UNAVAILABLE
        if len(unavailable) == len(active)
        else UICapabilityState.DEGRADED
    )
    return UICapabilityManifest(
        catalog.provider_id,
        catalog.surface,
        state,
        entries,
        restart_required=restart_required,
    )


def compile_native_ui_capability_manifest(
    surface: RuntimeSurface,
) -> UICapabilityManifest:
    """Describe the established Native product Surface without a second catalog.

    Native remains the product baseline and does not use the External provider
    inventory.  The result still uses the same UI contract and applicability
    matrix, so consumers never branch on Provider-specific payload shapes.
    """

    applicable = _SURFACE_AREAS[surface]
    entries = tuple(
        UICapability(
            capability_id,
            (
                UICapabilityState.AVAILABLE
                if capability_id in applicable
                else UICapabilityState.NOT_APPLICABLE
            ),
        )
        for capability_id in _UI_CAPABILITY_AREAS
    )
    return UICapabilityManifest(
        "native",
        surface,
        UICapabilityState.AVAILABLE,
        entries,
    )


__all__ = [
    "UICapability",
    "UICapabilityManifest",
    "UICapabilityState",
    "compile_native_ui_capability_manifest",
    "compile_ui_capability_manifest",
]
