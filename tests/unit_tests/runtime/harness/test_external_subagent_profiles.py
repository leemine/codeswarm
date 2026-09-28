# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Tests for the trusted External product-subagent profile catalog."""

from __future__ import annotations

import pytest

from jiuwenswarm.runtime.harness.external_subagent_profiles import (
    BROWSER_SUBAGENT_TYPE,
    EXTERNAL_SUBAGENT_PROFILES,
    GENERAL_PURPOSE_SUBAGENT_TYPE,
    ExternalSubagentExecutionKind,
    ExternalSubagentProfileUnavailableError,
    render_external_subagent_catalog,
    resolve_external_subagent_profile,
    validate_external_subagent_request,
)


def test_catalog_advertises_only_general_purpose_until_browser_is_ready() -> None:
    advertised = {
        name
        for name, profile in EXTERNAL_SUBAGENT_PROFILES.items()
        if profile.advertised
    }

    assert advertised == {GENERAL_PURPOSE_SUBAGENT_TYPE}
    assert (
        EXTERNAL_SUBAGENT_PROFILES[BROWSER_SUBAGENT_TYPE].execution_kind
        is ExternalSubagentExecutionKind.BROWSER
    )
    description = render_external_subagent_catalog()
    assert "general-purpose" in description
    assert "browser_agent" not in description
    assert "explore_agent" not in description

    admitted_description = render_external_subagent_catalog(browser_available=True)
    assert "general-purpose" in admitted_description
    assert "browser_agent" in admitted_description


@pytest.mark.parametrize(
    "subagent_type",
    [
        GENERAL_PURPOSE_SUBAGENT_TYPE,
        "code_agent",
        "explore_agent",
        "mobile_gui_agent",
        "plan_agent",
        "research_agent",
        "verification_agent",
    ],
)
def test_existing_generic_types_resolve_without_browser_semantics(
    subagent_type: str,
) -> None:
    profile = validate_external_subagent_request(
        subagent_type=subagent_type,
        browser_capabilities=None,
    )

    assert profile.execution_kind is ExternalSubagentExecutionKind.GENERIC


@pytest.mark.parametrize("subagent_type", ["", "unknown", "Browser_Agent", 7])
def test_unknown_types_fail_closed(subagent_type: object) -> None:
    with pytest.raises(ValueError):
        resolve_external_subagent_profile(subagent_type)


def test_browser_profile_fails_until_dedicated_adapter_is_installed() -> None:
    with pytest.raises(
        ExternalSubagentProfileUnavailableError,
        match="Browser admission adapter",
    ):
        validate_external_subagent_request(
            subagent_type=BROWSER_SUBAGENT_TYPE,
            browser_capabilities=("core",),
        )


def test_browser_profile_resolves_only_with_host_admission() -> None:
    profile = validate_external_subagent_request(
        subagent_type=BROWSER_SUBAGENT_TYPE,
        browser_capabilities=("core", "vision"),
        browser_available=True,
    )

    assert profile.execution_kind is ExternalSubagentExecutionKind.BROWSER


def test_admitted_browser_profile_rejects_unknown_capability() -> None:
    with pytest.raises(ValueError, match="unknown-browser-capability"):
        validate_external_subagent_request(
            subagent_type=BROWSER_SUBAGENT_TYPE,
            browser_capabilities=("unknown-browser-capability",),
            browser_available=True,
        )


@pytest.mark.parametrize("browser_capabilities", [(), ("core",)])
def test_generic_profile_rejects_browser_capabilities(
    browser_capabilities: tuple[str, ...],
) -> None:
    with pytest.raises(ValueError, match="only valid"):
        validate_external_subagent_request(
            subagent_type=GENERAL_PURPOSE_SUBAGENT_TYPE,
            browser_capabilities=browser_capabilities,
        )
