# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Delivery fidelity: only plain Sources location intraword underscores vary.

This compares generated text, not rendered HTML or semantic truth. Live evidence
below is opaque fixture data; no absolute source path is opened by these tests.
"""

from copy import deepcopy
import json
import re

import pytest

from jiuwenswarm.agents.harness.work.research_review import review_research_report


def matches_reviewed_report(report, expected, sources):
    """Compare to the actual reviewed rendering without invoking review again."""
    if not isinstance(report, str) or not isinstance(expected, str):
        return False
    if report == expected:
        return True
    marker = "\n\n## Sources\n\n"
    before, separator, expected_sources = expected.rpartition(marker)
    actual_before, actual_separator, actual_sources = report.rpartition(marker)
    if not separator or (actual_before, actual_separator) != (before, separator):
        return False
    expected_rows = expected_sources.splitlines(keepends=True)
    actual_rows = actual_sources.splitlines(keepends=True)
    if not len(actual_rows) == len(expected_rows) == len(sources):
        return False
    for source, expected_row, actual_row in zip(sources, expected_rows, actual_rows, strict=True):
        if actual_row == expected_row:
            continue
        location = source.get("location")
        # Deliberately narrower than Markdown parsing: code/link/HTML/emphasis
        # delimiters, whitespace and backslashes permit exact bytes only.
        if not isinstance(location, str) or not location or not all(
            char.isalnum() or char in "/._:-%+~" for char in location
        ):
            return False
        suffix = " — " + location.replace("_", r"\_") + "\n"
        if not expected_row.endswith(suffix):
            return False
        prefix = expected_row[:-len(suffix)] + " — "
        parts = []
        for index, char in enumerate(location):
            if char == "_":
                escaped = re.escape("\\_")
                if 0 < index < len(location) - 1 and location[index - 1].isalnum() and location[index + 1].isalnum():
                    parts.append("(?:" + escaped + "|_)")
                else:
                    parts.append(escaped)
            else:
                parts.append(re.escape(char))
        pattern = re.escape(prefix) + "".join(parts) + re.escape("\n")
        if not re.fullmatch(pattern, actual_row):
            return False
    return True


def _matches(report, **inputs):
    reviewed = review_research_report(**inputs)
    assert reviewed["structural_valid"]
    return matches_reviewed_report(report, reviewed["rendered_markdown"], inputs["sources"])


def _inputs(location="/tmp/work_run/source_a.md"):
    return {
        "sources": [{"id": "source_a.md", "text": "A recorded fact.\n", "start_line": 1,
                     "complete": True, "title": "Source_a title", "location": location}],
        "claims": [{"id": "claim-1", "section": "Findings", "kind": "fact", "text": "A recorded fact",
                    "refs": [{"source_id": "source_a.md", "start_line": 1, "end_line": 1,
                              "quote": "A recorded fact."}]}],
        "question": "Compare source_a observations",
    }


@pytest.mark.parametrize("variant", ["exact", "bare", "mixed"])
def test_only_confirmed_sources_path_underscores_may_vary(variant):
    inputs = _inputs()
    before = deepcopy(inputs)
    report = review_research_report(**inputs)["rendered_markdown"]
    if variant == "bare":
        report = report.replace(r"/tmp/work\_run/source\_a.md", "/tmp/work_run/source_a.md")
    elif variant == "mixed":
        report = report.replace(r"/tmp/work\_run/source\_a.md", r"/tmp/work_run/source\_a.md")
    assert _matches(report, **inputs)
    assert inputs == before


@pytest.mark.parametrize("old,new", [
    ("recorded fact", "invented fact"),
    ("source_a.md:1)", "source_a.md:2)"),
    ("source_a.md:1-1", "source_b.md:1-1"),
    (r"Source\_a title", "Source_a title"),
    (r"Compare source\_a", "Compare source_a"),
    (r"/tmp/work\_run/source\_a.md", "/tmp/other_run/source_a.md"),
    ("## Findings", "## Observations"),
    (" — ", " - "),
    ("A recorded fact", "`A recorded fact`"),
    ("A recorded fact", "[A recorded fact](https://example.test)"),
    ("A recorded fact", "<span>A recorded fact</span>"),
])
def test_non_location_changes_still_fail(old, new):
    inputs = _inputs()
    expected = review_research_report(**inputs)["rendered_markdown"]
    changed = expected.replace(old, new)
    assert changed != expected
    assert not _matches(changed, **inputs)


@pytest.mark.parametrize("location", [
    "/tmp/_emphasis_/source.md", "/tmp/double__underscore/source.md",
    "`/tmp/work_run/source.md`", "[/tmp/work_run](target)",
    "<span>/tmp/work_run</span>", "/tmp/*work_run*/source.md",
    "/tmp/work_run (copy)/source.md", r"C:\work_run\source.md",
])
def test_non_intraword_or_markup_locations_require_exact_bytes(location):
    inputs = _inputs(location)
    expected = review_research_report(**inputs)["rendered_markdown"]
    body, marker, source_rows = expected.rpartition("\n\n## Sources\n\n")
    changed = body + marker + source_rows.replace(r"\_", "_")
    assert _matches(expected, **inputs)
    assert changed != expected
    assert not _matches(changed, **inputs)


@pytest.mark.parametrize("suffix", ["\n", " ", "<!-- note -->"])
def test_trailing_bytes_are_not_stripped(suffix):
    inputs = _inputs()
    expected = review_research_report(**inputs)["rendered_markdown"]
    assert not _matches(expected + suffix, **inputs)


def test_unicode_alphanumeric_intraword_location_is_equivalent():
    inputs = _inputs("/资料/研究_记录2/source_a.md")
    expected = review_research_report(**inputs)["rendered_markdown"]
    changed = expected.replace(r"/资料/研究\_记录2/source\_a.md", "/资料/研究_记录2/source_a.md")
    assert _matches(changed, **inputs)


def test_absent_location_does_not_authorize_source_id_or_title_rewrites():
    inputs = _inputs()
    del inputs["sources"][0]["location"]
    expected = review_research_report(**inputs)["rendered_markdown"]
    assert _matches(expected, **inputs)
    assert not _matches(expected.replace(r"Source\_a", "Source_a"), **inputs)


def test_non_text_review_is_not_accepted():
    assert not matches_reviewed_report(None, None, [])
    assert not matches_reviewed_report("report", None, [])


def test_helper_does_not_reinvoke_review(monkeypatch):
    import sys
    inputs = _inputs()
    expected = review_research_report(**inputs)["rendered_markdown"]
    report = expected.replace(r"/tmp/work\_run/source\_a.md", "/tmp/work_run/source_a.md")
    def forbidden(*_args, **_kwargs):
        raise AssertionError("Comparison must not create a new observed review call")
    monkeypatch.setattr(sys.modules[__name__], "review_research_report", forbidden)
    assert matches_reviewed_report(report, expected, inputs["sources"])


# Minimal exact excerpts from the actual tool result and saved artifact.
# The original failed pytest and full reports remain unchanged external evidence.

_LIVE_SOURCES_JSON = r'''[
  {
    "complete": true,
    "id": "source-a.md",
    "location": "/tmp/r1-12-quality/live-accepted/opencode/isolated-tmp/pytest-of-leewanlong/pytest-0/test_work_research_real_extern0/workspace/source-a.md",
    "start_line": 1,
    "text": "# Pilot A field log\nDate: 2026-09-01\nPrimary observation: offline retrieval took 42 seconds for 12 documents.\nAll 12 documents were indexed locally. No network requirement was tested.",
    "title": "Pilot A field log"
  },
  {
    "complete": true,
    "id": "source-b.md",
    "location": "/tmp/r1-12-quality/live-accepted/opencode/isolated-tmp/pytest-of-leewanlong/pytest-0/test_work_research_real_extern0/workspace/source-b.md",
    "start_line": 1,
    "text": "# Pilot B field log\nDate: 2026-09-02\nPrimary observation: connected retrieval took 31 seconds for 12 documents.\nA network connection was required. The conditions differ from Pilot A; this is not a controlled benchmark and does not establish superiority.",
    "title": "Pilot B field log"
  }
]'''

_LIVE_EXPECTED = r'''# Research Report

Pilot A's offline retrieval took 42 seconds for 12 documents (source-a.md:3).

## Sources

- source-a.md:1-4 — Pilot A field log — /tmp/r1-12-quality/live-accepted/opencode/isolated-tmp/pytest-of-leewanlong/pytest-0/test\_work\_research\_real\_extern0/workspace/source-a.md
- source-b.md:1-4 — Pilot B field log — /tmp/r1-12-quality/live-accepted/opencode/isolated-tmp/pytest-of-leewanlong/pytest-0/test\_work\_research\_real\_extern0/workspace/source-b.md
'''

_LIVE_REPORT = r'''# Research Report

Pilot A's offline retrieval took 42 seconds for 12 documents (source-a.md:3).

## Sources

- source-a.md:1-4 — Pilot A field log — /tmp/r1-12-quality/live-accepted/opencode/isolated-tmp/pytest-of-leewanlong/pytest-0/test_work_research_real_extern0/workspace/source-a.md
- source-b.md:1-4 — Pilot B field log — /tmp/r1-12-quality/live-accepted/opencode/isolated-tmp/pytest-of-leewanlong/pytest-0/test_work_research_real_extern0/workspace/source-b.md
'''

_ORIGINAL_REVIEW_SHA256 = '9bd3e2a0900cff8437fa003ec00634a56e7a1aa003ee201317fb594bc55c5f90'


def test_original_opencode_sources_excerpt_retains_the_delivery_false_positive():
    sources = json.loads(_LIVE_SOURCES_JSON)
    assert _LIVE_REPORT != _LIVE_EXPECTED
    assert matches_reviewed_report(_LIVE_REPORT, _LIVE_EXPECTED, sources)
    assert not matches_reviewed_report(_LIVE_REPORT.replace("42 seconds", "43 seconds"), _LIVE_EXPECTED, sources)
    assert not matches_reviewed_report(_LIVE_REPORT.replace("source-a.md:3)", "source-a.md:4)"), _LIVE_EXPECTED, sources)


@pytest.mark.parametrize("replacement", [
    "`/tmp/work_run/source_a.md`", "[/tmp/work_run/source_a.md](target)",
    "<span>/tmp/work_run/source_a.md</span>", "*/tmp/work_run/source_a.md*",
])
def test_a_plain_path_cannot_be_replaced_with_markup(replacement):
    inputs = _inputs()
    expected = review_research_report(**inputs)["rendered_markdown"]
    changed = expected.replace(r"/tmp/work\_run/source\_a.md", replacement)
    assert not matches_reviewed_report(changed, expected, inputs["sources"])


def test_same_location_in_body_is_not_given_sources_equivalence():
    inputs = _inputs()
    inputs["claims"][0]["text"] = "Observed /tmp/work_run/source_a.md"
    expected = review_research_report(**inputs)["rendered_markdown"]
    changed = expected.replace(r"/tmp/work\_run/source\_a.md", "/tmp/work_run/source_a.md", 1)
    assert not matches_reviewed_report(changed, expected, inputs["sources"])
