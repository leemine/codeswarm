# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from openjiuwen.harness_protocol import RuntimeSurface

from jiuwenswarm.runtime.harness.capability_catalog import (
    CapabilityAvailability,
    CapabilityKind,
    CapabilitySource,
    CapabilityUnavailableReason,
    EffectiveCapability,
    EffectiveCapabilityCatalog,
)
from jiuwenswarm.runtime.harness.ui_capability_manifest import (
    UICapabilityState,
    compile_native_ui_capability_manifest,
    compile_ui_capability_manifest,
)


def _category(
    name: str,
    *,
    availability: CapabilityAvailability = CapabilityAvailability.AVAILABLE,
    reason: str = "",
    unavailable_reason: CapabilityUnavailableReason | None = None,
    requires_authorization: bool = False,
) -> EffectiveCapability:
    return EffectiveCapability(
        name,
        CapabilityKind.CATEGORY,
        CapabilitySource.PROVIDER_NATIVE,
        availability,
        reason,
        unavailable_reason,
        requires_authorization,
    )


def test_work_manifest_maps_install_and_surface_states_without_faking_auth_failure():
    catalog = EffectiveCapabilityCatalog(
        "codex",
        RuntimeSurface.WORK,
        (
            _category("documents", requires_authorization=True),
            _category("web"),
            _category("artifacts"),
            _category(
                "browser",
                availability=CapabilityAvailability.UNAVAILABLE,
                reason="managed Browser is not installed",
                unavailable_reason=CapabilityUnavailableReason.NOT_INSTALLED,
            ),
            _category("subagents"),
            _category("memory"),
        ),
    )

    manifest = compile_ui_capability_manifest(catalog)
    by_id = {entry.capability_id: entry for entry in manifest.entries}

    assert manifest.state is UICapabilityState.DEGRADED
    assert by_id["documents"].state is UICapabilityState.AVAILABLE
    assert by_id["documents"].requires_authorization is True
    assert by_id["browser"].state is UICapabilityState.NEEDS_INSTALL
    assert by_id["git"].state is UICapabilityState.NOT_APPLICABLE
    assert len(manifest.fingerprint) == 64


def test_code_manifest_fails_closed_when_required_category_is_missing():
    names = {
        "filesystem",
        "terminal",
        "git",
        "diff",
        "test",
        "review",
        "browser",
        "subagents",
        "memory",
    }
    catalog = EffectiveCapabilityCatalog(
        "opencode",
        RuntimeSurface.CODE,
        tuple(_category(name) for name in names),
    )

    manifest = compile_ui_capability_manifest(catalog, restart_required=True)
    by_id = {entry.capability_id: entry for entry in manifest.entries}

    assert manifest.restart_required is True
    assert manifest.state is UICapabilityState.DEGRADED
    assert by_id["lsp"].state is UICapabilityState.UNAVAILABLE
    assert by_id["lsp"].reason_code == "manifest_category_missing"
    assert by_id["documents"].state is UICapabilityState.NOT_APPLICABLE


def test_native_uses_same_manifest_shape_and_surface_applicability():
    manifest = compile_native_ui_capability_manifest(RuntimeSurface.CODE)
    by_id = {entry.capability_id: entry for entry in manifest.entries}

    assert manifest.provider_id == "native"
    assert manifest.state is UICapabilityState.AVAILABLE
    assert by_id["review"].state is UICapabilityState.AVAILABLE
    assert by_id["artifacts"].state is UICapabilityState.NOT_APPLICABLE
