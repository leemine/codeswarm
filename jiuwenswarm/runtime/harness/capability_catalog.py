# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""One cold-start capability catalog for an External product Surface."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import Enum

from openjiuwen.harness_protocol import (
    ExecutionAuthorization,
    ProviderCapabilityInventory,
    ProviderCapabilityKind,
    RuntimeSurface,
)

from jiuwenswarm.runtime.harness.surface import EffectiveSurfaceSnapshot

_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@/-]{0,255}")


class CapabilityKind(str, Enum):
    CATEGORY = "category"
    TOOL = "tool"
    SKILL = "skill"
    MCP_SERVER = "mcp_server"
    PLUGIN = "plugin"
    SUBAGENT = "subagent"


class CapabilitySource(str, Enum):
    PRODUCT_TOOL = "product_tool"
    PROVIDER_NATIVE = "provider_native"
    PRODUCT_SUBAGENT = "product_subagent"


class CapabilityAvailability(str, Enum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"


class CapabilityUnavailableReason(str, Enum):
    """Stable explanation for an unavailable semantic capability."""

    NOT_INSTALLED = "not_installed"
    PROVIDER_UNSUPPORTED = "provider_unsupported"


@dataclass(frozen=True, slots=True)
class EffectiveCapability:
    name: str
    kind: CapabilityKind
    source: CapabilitySource
    availability: CapabilityAvailability = CapabilityAvailability.AVAILABLE
    reason: str = ""
    unavailable_reason: CapabilityUnavailableReason | None = None
    requires_authorization: bool = False
    provider_source: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not _NAME_RE.fullmatch(self.name):
            raise ValueError("capability name must be normalized and path-safe")
        for value, expected, label in (
            (self.kind, CapabilityKind, "kind"),
            (self.source, CapabilitySource, "source"),
            (self.availability, CapabilityAvailability, "availability"),
        ):
            if not isinstance(value, expected):
                raise TypeError(f"capability {label} has an invalid value")
        if self.availability is CapabilityAvailability.AVAILABLE and self.reason:
            raise ValueError("available capability must not carry an unavailable reason")
        if (
            self.availability is CapabilityAvailability.AVAILABLE
            and self.unavailable_reason is not None
        ):
            raise ValueError("available capability must not carry an unavailable reason code")
        if self.availability is CapabilityAvailability.UNAVAILABLE:
            if not self.reason:
                raise ValueError("unavailable capability requires a reason")
            if not isinstance(self.unavailable_reason, CapabilityUnavailableReason):
                raise ValueError("unavailable capability requires a reason code")
        if not isinstance(self.requires_authorization, bool):
            raise TypeError("capability requires_authorization must be boolean")
        if not isinstance(self.provider_source, str):
            raise TypeError("capability provider_source must be a string")

    @property
    def key(self) -> tuple[CapabilityKind, str]:
        return self.kind, self.name.casefold()

    def record(self) -> dict[str, object]:
        return {
            "name": self.name,
            "kind": self.kind.value,
            "source": self.source.value,
            "availability": self.availability.value,
            "reason": self.reason,
            "unavailable_reason": (
                self.unavailable_reason.value
                if self.unavailable_reason is not None
                else None
            ),
            "requires_authorization": self.requires_authorization,
            "provider_source": self.provider_source,
        }


@dataclass(frozen=True, slots=True)
class EffectiveCapabilityCatalog:
    provider_id: str
    surface: RuntimeSurface
    entries: tuple[EffectiveCapability, ...]
    schema_version: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.provider_id, str) or not self.provider_id.strip():
            raise ValueError("capability catalog requires a provider_id")
        if not isinstance(self.surface, RuntimeSurface):
            raise TypeError("capability catalog surface must be RuntimeSurface")
        if self.schema_version != 1:
            raise ValueError("unsupported capability catalog schema")
        entries = tuple(self.entries)
        if any(not isinstance(item, EffectiveCapability) for item in entries):
            raise TypeError("capability catalog contains an invalid entry")
        keys: set[tuple[CapabilityKind, str]] = set()
        for entry in entries:
            if entry.key in keys:
                raise ValueError(
                    f"duplicate effective capability: {entry.kind.value}:{entry.name}"
                )
            keys.add(entry.key)
        object.__setattr__(
            self,
            "entries",
            tuple(sorted(entries, key=lambda item: (item.kind.value, item.name.casefold()))),
        )

    def available(self, kind: CapabilityKind | None = None) -> tuple[EffectiveCapability, ...]:
        return tuple(
            entry
            for entry in self.entries
            if entry.availability is CapabilityAvailability.AVAILABLE
            and (kind is None or entry.kind is kind)
        )

    def record(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "provider_id": self.provider_id,
            "surface": self.surface.value,
            "entries": [entry.record() for entry in self.entries],
        }

    @property
    def fingerprint(self) -> str:
        payload = json.dumps(
            self.record(), sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode()
        return hashlib.sha256(payload).hexdigest()


_PROVIDER_KIND = {
    ProviderCapabilityKind.CATEGORY: CapabilityKind.CATEGORY,
    ProviderCapabilityKind.TOOL: CapabilityKind.TOOL,
    ProviderCapabilityKind.SKILL: CapabilityKind.SKILL,
    ProviderCapabilityKind.MCP_SERVER: CapabilityKind.MCP_SERVER,
    ProviderCapabilityKind.PLUGIN: CapabilityKind.PLUGIN,
}


def compile_capability_catalog(
    surface: EffectiveSurfaceSnapshot,
    *,
    provider_inventory: ProviderCapabilityInventory,
    product_tool_names: tuple[str, ...],
    product_subagent_types: tuple[str, ...],
    authorization: ExecutionAuthorization,
) -> EffectiveCapabilityCatalog:
    """Merge authoritative namespaces and explain every requested category."""

    policy = surface.runtime_policy
    if policy is None:
        raise ValueError("Surface runtime policy must be compiled before capabilities")
    provider_id = surface.identity.binding.provider_id
    if provider_inventory.provider_id != provider_id:
        raise ValueError("Provider capability inventory does not match the Surface")
    runtime_surface = RuntimeSurface(surface.identity.work_mode)
    provider_entries = provider_inventory.for_surface(runtime_surface)
    native_tools = {
        item.name.casefold()
        for item in provider_entries
        if item.kind is ProviderCapabilityKind.TOOL
    }
    normalized_product_tools: dict[str, str] = {}
    for name in product_tool_names:
        if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
            raise ValueError("product tool catalog contains an invalid name")
        key = name.casefold()
        if key in normalized_product_tools:
            raise ValueError(f"duplicate product tool capability: {name}")
        if key in native_tools:
            raise ValueError(
                f"provider-native tool conflicts with authoritative product tool: {name}"
            )
        normalized_product_tools[key] = name

    entries = [
        EffectiveCapability(
            item.name,
            _PROVIDER_KIND[item.kind],
            CapabilitySource.PROVIDER_NATIVE,
            requires_authorization=(
                not authorization.full_access
                and item.kind in {ProviderCapabilityKind.CATEGORY, ProviderCapabilityKind.TOOL}
            ),
            provider_source=item.source,
        )
        for item in provider_entries
    ]
    entries.extend(
        EffectiveCapability(
            name,
            CapabilityKind.TOOL,
            CapabilitySource.PRODUCT_TOOL,
            requires_authorization=not authorization.full_access,
        )
        for name in normalized_product_tools.values()
    )
    subagents: dict[str, str] = {}
    for name in product_subagent_types:
        if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
            raise ValueError("product subagent catalog contains an invalid name")
        key = name.casefold()
        if key in subagents:
            raise ValueError(f"duplicate product subagent capability: {name}")
        subagents[key] = name
        entries.append(
            EffectiveCapability(
                name,
                CapabilityKind.SUBAGENT,
                CapabilitySource.PRODUCT_SUBAGENT,
                requires_authorization=not authorization.full_access,
            )
        )

    available_categories = {
        item.name.casefold()
        for item in provider_entries
        if item.kind is ProviderCapabilityKind.CATEGORY
    }
    if subagents:
        available_categories.add("subagents")
    if "browser_agent" in subagents:
        available_categories.add("browser")
    existing_category_names = {
        entry.name.casefold()
        for entry in entries
        if entry.kind is CapabilityKind.CATEGORY
    }
    for name in policy.required_capabilities:
        key = name.casefold()
        if key in existing_category_names:
            continue
        available = key in available_categories
        host_optional = key in {"browser", "subagents"}
        entries.append(
            EffectiveCapability(
                name,
                CapabilityKind.CATEGORY,
                (
                    CapabilitySource.PRODUCT_SUBAGENT
                    if key in {"subagents", "browser"}
                    else CapabilitySource.PROVIDER_NATIVE
                ),
                (
                    CapabilityAvailability.AVAILABLE
                    if available
                    else CapabilityAvailability.UNAVAILABLE
                ),
                (
                    ""
                    if available
                    else (
                        f"{provider_id} does not load this capability "
                        f"for {runtime_surface.value}"
                    )
                ),
                (
                    None
                    if available
                    else CapabilityUnavailableReason.NOT_INSTALLED
                    if host_optional
                    else CapabilityUnavailableReason.PROVIDER_UNSUPPORTED
                ),
                requires_authorization=available and not authorization.full_access,
            )
        )
    return EffectiveCapabilityCatalog(provider_id, runtime_surface, tuple(entries))


__all__ = [
    "CapabilityAvailability",
    "CapabilityKind",
    "CapabilitySource",
    "CapabilityUnavailableReason",
    "EffectiveCapability",
    "EffectiveCapabilityCatalog",
    "compile_capability_catalog",
]
