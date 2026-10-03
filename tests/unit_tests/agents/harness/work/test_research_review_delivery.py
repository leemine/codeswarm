# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""The live gate rejects unreviewed artifacts and invented review source text."""

import copy

import pytest

from tests.system_tests.test_work_research_remote import (
    _check_review_delivery,
    _check_condition_comparison_citations,
    _write_sources,
)


@pytest.fixture
def reviewed(tmp_path):
    root = tmp_path / "workspace"
    _write_sources(root)
    sources = [
        {"id": name, "text": (root / name).read_text(), "start_line": 1,
         "complete": True}
        for name in ("source-a.md", "source-b.md")
    ]
    # Gate fixtures describe observations; they are not a model quality verdict.
    return root, "reviewed report\n", [{
        "sources": sources,
        "result": {"structural_valid": True, "rendered_markdown": "reviewed report\n"},
    }]


def test_review_delivery_matches_original_source_and_rendering(reviewed):
    _check_review_delivery(*reviewed)


@pytest.mark.parametrize("problem", ["no_review", "unbounded", "invalid", "rewrite", "source", "false_complete"])
def test_review_delivery_rejects_bypasses(reviewed, problem):
    root, report, records = copy.deepcopy(reviewed)
    if problem == "no_review":
        records.clear()
    elif problem == "unbounded":
        records *= 4
    elif problem == "invalid":
        records[-1]["result"]["structural_valid"] = False
    elif problem == "rewrite":
        report += "unreviewed additional fact"
    elif problem == "source":
        records[-1]["sources"][0]["text"] = "Invented original source"
    else:
        records[-1]["sources"][0]["text"] = "# Pilot A field log\n"
    with pytest.raises(AssertionError):
        _check_review_delivery(root, report, records)


def test_comparison_needs_both_condition_observations_not_just_caveat():
    claim = "The runs used different conditions (offline vs connected), so they are not a controlled benchmark"
    with pytest.raises(AssertionError, match="source-a.md observation"):
        _check_condition_comparison_citations(claim + " (source-b.md:4).")
    with pytest.raises(AssertionError, match="source-b.md observation"):
        _check_condition_comparison_citations(claim + " (source-a.md:3; source-b.md:4).")
    _check_condition_comparison_citations(claim + " (source-a.md:3; source-b.md:3-4).")
