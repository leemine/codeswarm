# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Actual report regressions: negated conclusions and inspected citation scope."""
from pathlib import Path

import pytest

from tests.system_tests.test_work_research_remote import (
    _check_absent_dependency_claims,
    _check_exclusive_network_claims,
    _check_single_record_citations,
    _check_report_citation_coverage,
    _write_sources,
)


@pytest.mark.parametrize("provider", ["native", "opencode"])
def test_real_negated_absence_is_not_an_assertion(provider):
    report = (Path(__file__).parent / "fixtures" / f"{provider}-negated-absence-report.md").read_text()
    _check_absent_dependency_claims(report)
    # A valid negation cannot hide a separate, incorrect assertion.
    with pytest.raises(AssertionError, match="absent dependency"):
        _check_absent_dependency_claims(report + "\nNevertheless, the network dependency is absent.")
    replacement = "evidence that" if provider == "opencode" else "that"
    original = "not evidence that" if provider == "opencode" else "not that"
    assert original in report
    with pytest.raises(AssertionError, match="absent dependency"):
        _check_absent_dependency_claims(report.replace(original, replacement))


@pytest.mark.parametrize("claim", [
    "An untested requirement remains absent.",
    "The network dependency is not required.",
    "The network requirement was zero.",
    "It is not only evidence that a network dependency is absent.",
    "It is not evidence of failure; the network dependency is absent.",
])
def test_direct_or_separately_asserted_absence_still_rejected(claim):
    with pytest.raises(AssertionError, match="absent dependency"):
        _check_absent_dependency_claims(claim)


def _report(provider):
    return (Path(__file__).parent / "fixtures" / f"{provider}-negated-absence-report.md").read_text()


def test_real_native_missing_date_and_full_range_remain_quality_failures(tmp_path):
    root = tmp_path / "workspace"
    _write_sources(root)
    report = _report("native")
    with pytest.raises(AssertionError, match="Date 2026-09-01"):
        _check_report_citation_coverage(root, report)
    dated = report.replace(
        "Time range: 2026-09-01 to 2026-09-02.",
        "Time range: 2026-09-01 (source-a.md:2) to 2026-09-02 (source-b.md:2).",
    )
    with pytest.raises(AssertionError, match="Whole-source omission"):
        _check_report_citation_coverage(root, dated)
    covered = dated.replace(
        "The inspected sources do not report repetitions, controls, or architecture-wide network behavior.",
        "The inspected sources do not report repetitions, controls, or architecture-wide network behavior (source-a.md:1-4; source-b.md:1-4).",
    )
    _check_report_citation_coverage(root, covered)


def test_real_opencode_dates_and_full_ranges_are_supported(tmp_path):
    root = tmp_path / "workspace"
    _write_sources(root)
    report = _report("opencode")
    _check_report_citation_coverage(root, report)
    with pytest.raises(AssertionError, match="Date 2026-09-01"):
        _check_report_citation_coverage(root, report.replace("dated 2026-09-01, source-a.md:2", "dated 2026-09-01, source-a.md:1"))
    with pytest.raises(AssertionError, match="Whole-source omission"):
        _check_report_citation_coverage(root, report.replace("source-a.md:1-4", "source-a.md:2-4"))


@pytest.mark.parametrize("locator", ["source-a.md:2", "source-a.md:L2", "source-a.md, L2", "source-a.md lines 1-4"])
def test_dates_allow_supported_covering_locators_without_borrowing(tmp_path, locator):
    root = tmp_path / "workspace"
    _write_sources(root)
    _check_report_citation_coverage(root, f"Date 2026-09-01 ({locator}).")
    with pytest.raises(AssertionError, match="Date"):
        _check_report_citation_coverage(root, f"Date 2026-09-01. Another fact ({locator}).")


@pytest.mark.parametrize("verb", ["required", "requires", "needed", "needs"])
def test_unknown_cannot_be_excluded_by_an_exclusive_network_claim(verb):
    with pytest.raises(AssertionError, match="exclusive network"):
        _check_exclusive_network_claims(f"Only Pilot B {verb} a network connection. Pilot A is unknown.")


@pytest.mark.parametrize("claim", [
    "Only Pilot B documented a required network connection.",
    "Only Pilot B reported a required network connection.",
    "Only Pilot B is documented as requiring a network connection.",
    "Pilot B required a network connection; Pilot A is unknown.",
    "It does not establish that only Pilot B required a network connection.",
    "We cannot conclude that only Pilot B required a network connection.",
])
def test_documented_requirement_is_not_exclusive_real_world_requirement(claim):
    _check_exclusive_network_claims(claim)


def test_actual_exclusive_report_fails_despite_correct_unknown_disclaimer():
    report = (Path(__file__).parent / "fixtures/opencode-unsupported-exclusivity.md").read_text()
    assert "dependency is unknown, not absent" in report
    with pytest.raises(AssertionError, match="exclusive network"):
        _check_exclusive_network_claims(report)
    _check_exclusive_network_claims(report.replace(
        "only Pilot B required a network connection", "only Pilot B documented a required network connection"
    ))


def test_actual_single_record_fact_requires_both_adjacent_sources():
    report = (Path(__file__).parent / "fixtures/codex-single-record-uncited.md").read_text()
    with pytest.raises(AssertionError, match="Single-record fact"):
        _check_single_record_citations(report)
    _check_single_record_citations(report.replace(
        "Each log records a single run;", "Each log records a single run (source-a.md:3; source-b.md:3);"
    ))


@pytest.mark.parametrize("claim", [
    "Each log records a single primary observation (source-a.md:3, source-b.md:3).",
    "Both sources record one run (source-a.md:L1-L4; source-b.md, L3).",
    "Pilot A records one run (source-a.md:3). Pilot B records one run (source-b.md:3).",
    "Source A records one observation (source-a.md:3); Source B records one observation (source-b.md:3).",
])
def test_single_record_facts_allow_combined_or_separate_cited_sentences(claim):
    _check_single_record_citations(claim)


@pytest.mark.parametrize("claim", [
    "Each log records a single run (source-a.md:3).",
    "Each log records a single run. Sources: source-a.md:1-4, source-b.md:1-4.",
    "Pilot A records one run. Pilot B records one run (source-a.md:3, source-b.md:3).",
    "Each log records a single run (source-a.md:2, source-b.md:3).",
])
def test_single_record_facts_cannot_borrow_other_sentence_or_wrong_line(claim):
    with pytest.raises(AssertionError, match="Single-record fact"):
        _check_single_record_citations(claim)
