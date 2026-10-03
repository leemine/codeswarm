# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Lightweight delivery checks do not impose the removed renderer/JSON contract."""
import pytest

from tests.system_tests.test_work_research_remote import _check_report, _write_sources


@pytest.fixture
def root(tmp_path):
    path = tmp_path / 'workspace'
    _write_sources(path)
    return path


@pytest.mark.parametrize('locator', ['source-a.md:3', 'source-a.md:L3', 'source-a.md, L3', 'source-a.md lines 1-4', 'source-a.md#L3', 'source-a.md#L3-L4'])
def test_available_locators_do_not_force_renderer_or_four_artifacts(root, locator):
    report = f'Our scope uses two supplied logs. Observation [{locator}]; comparison [source-b.md:3-4].\n'
    (root / 'research-report.md').write_text(report)
    assert _check_report(root) == report
    assert sorted(path.name for path in root.iterdir()) == ['research-report.md', 'source-a.md', 'source-b.md']


@pytest.mark.parametrize('report', ['', 'Only source-a.md:3 is cited.', 'source-a.md:0 source-b.md:4',
                                     'source-a.md:3 source-b.md:3-5', 'source-a.md:4-2 source-b.md:4'])
def test_missing_or_out_of_bounds_locators_are_mechanical_failures(root, report):
    (root / 'research-report.md').write_text(report)
    with pytest.raises(AssertionError):
        _check_report(root)


def test_missing_report_is_not_replaced_by_an_inline_completion_claim(root):
    with pytest.raises(AssertionError, match='not saved'):
        _check_report(root)


def test_locator_availability_is_explicitly_not_a_semantic_or_omission_judge(root):
    # Deliberately wrong conclusion: passing this mechanical check must never
    # count as factual acceptance. Independent review must reject the claim.
    report = 'Pilot A universally has no dependency (source-a.md:4). Pilot B is superior (source-b.md:4).'
    (root / 'research-report.md').write_text(report)
    assert _check_report(root) == report
    # A scope-limited omission is not required to borrow another source's range.
    report = 'source-a.md does not report repeated runs (source-a.md:1-4). B warns against superiority (source-b.md:4).'
    (root / 'research-report.md').write_text(report)
    assert _check_report(root) == report
