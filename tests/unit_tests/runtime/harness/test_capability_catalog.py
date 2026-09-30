"""R1-11C effective Tool and product-subagent capability catalog."""

from dataclasses import replace

import pytest

from openjiuwen.harness_protocol import (
    ExecutionAuthorization,
    ProviderCapability,
    ProviderCapabilityInventory,
    ProviderCapabilityKind,
    RuntimeSurface,
)

from jiuwenswarm.runtime.harness.capability_catalog import (
    CapabilityAvailability,
    CapabilityKind,
    CapabilitySource,
    CapabilityUnavailableReason,
    compile_capability_catalog,
)
from jiuwenswarm.runtime.harness.context_bridge import build_external_context
from jiuwenswarm.runtime.harness.external_subagent_profiles import (
    surface_external_subagent_profiles,
)
from tests.unit_tests.runtime.harness.test_surface_policy import _compiled


def _inventory(*entries: ProviderCapability) -> ProviderCapabilityInventory:
    return ProviderCapabilityInventory(
        "codex",
        (
            ProviderCapability(
                "filesystem",
                ProviderCapabilityKind.CATEGORY,
                frozenset({RuntimeSurface.CODE}),
            ),
            ProviderCapability(
                "terminal",
                ProviderCapabilityKind.CATEGORY,
                frozenset({RuntimeSurface.CODE}),
            ),
            *entries,
        ),
    )


def _catalog(tmp_path, *, work_mode="code", browser=False, authorization=None):
    tmp_path.mkdir(parents=True, exist_ok=True)
    surface = _compiled(tmp_path, work_mode=work_mode)
    profiles = surface_external_subagent_profiles(
        work_mode, browser_available=browser
    )
    return surface, compile_capability_catalog(
        surface,
        provider_inventory=_inventory(
            ProviderCapability(
                "portable-helper",
                ProviderCapabilityKind.SKILL,
                source="portable_skill",
            )
        ),
        product_tool_names=("subagent_spawn", "subagent_wait"),
        product_subagent_types=tuple(item.subagent_type for item in profiles),
        authorization=authorization or ExecutionAuthorization(),
    )


def test_work_and_code_mount_distinct_subagents_and_explain_missing_categories(
    tmp_path,
) -> None:
    _, work = _catalog(tmp_path / "work", work_mode="work")
    _, code = _catalog(tmp_path / "code", work_mode="code")
    work_subagents = {item.name for item in work.available(CapabilityKind.SUBAGENT)}
    code_subagents = {item.name for item in code.available(CapabilityKind.SUBAGENT)}

    assert work_subagents == {"general-purpose", "research_agent"}
    assert code_subagents == {
        "general-purpose",
        "explore_agent",
        "plan_agent",
        "code_agent",
    }
    assert "research_agent" not in code_subagents
    assert "code_agent" not in work_subagents
    assert "portable-helper" in {
        item.name for item in work.available(CapabilityKind.SKILL)
    }
    assert {"filesystem", "terminal"} <= {
        item.name for item in code.available(CapabilityKind.CATEGORY)
    }
    assert "filesystem" not in {
        item.name for item in work.entries if item.kind is CapabilityKind.CATEGORY
    }
    work_categories = {
        item.name: item
        for item in work.entries
        if item.kind is CapabilityKind.CATEGORY
    }
    assert work_categories["documents"].availability is CapabilityAvailability.UNAVAILABLE
    assert (
        work_categories["documents"].unavailable_reason
        is CapabilityUnavailableReason.PROVIDER_UNSUPPORTED
    )
    assert (
        work_categories["browser"].unavailable_reason
        is CapabilityUnavailableReason.NOT_INSTALLED
    )
    assert work_categories["subagents"].availability is CapabilityAvailability.AVAILABLE
    assert work_categories["documents"].reason


def test_browser_and_provider_native_skill_share_one_catalog(tmp_path) -> None:
    _, catalog = _catalog(tmp_path, browser=True)

    assert any(
        item.name == "browser_agent"
        and item.source is CapabilitySource.PRODUCT_SUBAGENT
        for item in catalog.available()
    )
    assert any(
        item.name == "portable-helper"
        and item.source is CapabilitySource.PROVIDER_NATIVE
        for item in catalog.available(CapabilityKind.SKILL)
    )
    assert catalog.fingerprint == replace(catalog).fingerprint


def test_authorization_changes_approval_flag_without_adding_names(tmp_path) -> None:
    _, restricted = _catalog(tmp_path / "restricted")
    _, full = _catalog(
        tmp_path / "full",
        authorization=ExecutionAuthorization(full_access=True),
    )

    restricted_keys = {(entry.kind, entry.name) for entry in restricted.entries}
    full_keys = {(entry.kind, entry.name) for entry in full.entries}
    assert restricted_keys == full_keys
    assert any(entry.requires_authorization for entry in restricted.available())
    assert not any(entry.requires_authorization for entry in full.available())


def test_authoritative_product_tool_conflict_fails_closed(tmp_path) -> None:
    surface = _compiled(tmp_path)
    inventory = _inventory(
        ProviderCapability(
            "subagent_spawn",
            ProviderCapabilityKind.TOOL,
            source="native-plugin",
        )
    )

    with pytest.raises(ValueError, match="conflicts"):
        compile_capability_catalog(
            surface,
            provider_inventory=inventory,
            product_tool_names=("subagent_spawn",),
            product_subagent_types=("general-purpose",),
            authorization=ExecutionAuthorization(),
        )


def test_catalog_is_projected_into_provider_context(tmp_path) -> None:
    surface, catalog = _catalog(tmp_path)
    surface = replace(surface, capability_catalog=catalog)
    context = build_external_context(
        paths=surface.identity.paths,
        host_session_id=surface.identity.binding.host_session_id,
        channel_id="web",
        provider_id="codex",
        surface=surface,
    )

    assert context.metadata["capability_catalog_fingerprint"] == catalog.fingerprint
    assert context.metadata["capability_catalog"]["surface"] == "code"
