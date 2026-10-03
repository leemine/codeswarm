# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Public read observations bind full source and final report bytes.

These deterministic fixtures exercise actual Native/c9 and external CLI result
shapes. They do not simulate a model's semantic audit or positive acceptance.
"""
import hashlib
import json

import pytest

from openjiuwen.core.foundation.tool.schema import ToolOutput
from tests.system_tests.test_work_research_remote import (
    _ResearchTrace,
    _check_parent_acceptance,
    _write_sources,
)


REPORT = "# Research Report\n\n## Findings\n\n42 seconds (source-a.md:3).\n\n## Sources\n\n- source-a.md:1-4\n"


def _cat_n(text):
    return "\n".join(f"{i:>6}\t{line}" for i, line in enumerate(text.splitlines(), 1))


def _opencode(path, text):
    # Fixed CLI completed read, copied as a shape from the public 24641f38 run.
    lines = text.splitlines()
    return (
        f"<path>{path}</path>\n<type>file</type>\n<content>\n"
        + "\n".join(f"{i}: {line}" for i, line in enumerate(lines, 1))
        + f"\n\n(End of file - total {len(lines)} lines)\n</content>"
    )


@pytest.fixture
def trace(tmp_path):
    root = tmp_path / "workspace"
    _write_sources(root)
    (root / "research-report.md").write_text(REPORT)
    value = _ResearchTrace("fixture", root)
    value.spawned_child_ids = {"parent_sub_research_original"}
    # Only metadata needed by the observational gate; semantic review is separate.
    value.reviews = [{"sources": [], "claims": [{"id": "C1"}],
                      "result": {"input_fingerprint": "fixture-fingerprint"}}]
    value.mark("research_review")
    (root / "research-review-input.json").write_text(json.dumps({"sources": [], "claims": [{"id": "C1"}]}))
    (root / "research-audit.json").write_text(json.dumps({
        "checked_claim_ids": ["C1"], "input_fingerprint": "fixture-fingerprint", "unresolved_issues": [],
    }))
    return value


def _full_sources(trace):
    for name in ("source-a.md", "source-b.md"):
        trace.observe_parent_read("read_file", {"file_path": str(trace.root / name)},
                                  {"content": (trace.root / name).read_text()})


@pytest.mark.parametrize("source", ["source-a.md", "source-b.md"])
@pytest.mark.parametrize("missing", [0, 1, 2, 3])
def test_each_original_source_line_is_required(trace, source, missing):
    lines = (trace.root / source).read_text().splitlines()
    del lines[missing]
    trace.observe_parent_read("read_file", {"file_path": source}, {"content": "\n".join(lines)})
    assert trace.parent_source_reads == set()


def test_findings_only_is_not_a_full_source_read(trace):
    for name in ("source-a.md", "source-b.md"):
        tail = "\n".join((trace.root / name).read_text().splitlines()[2:])
        trace.observe_parent_read("read_file", {"file_path": name}, {"content": tail})
    trace.observe_parent_read("read_file", {"file_path": "research-report.md"}, {"content": REPORT})
    with pytest.raises(AssertionError, match="both original source"):
        _check_parent_acceptance(trace)


@pytest.mark.parametrize("wrapper", ["native", "opencode", "mcp_text", "mcp_blocks", "codex"])
def test_real_public_result_shapes_read_complete_files(trace, wrapper):
    for name in ("source-a.md", "source-b.md", "research-report.md"):
        path = trace.root / name
        text = path.read_text()
        if wrapper == "native":
            tool, args = "read_file", {"file_path": str(path)}
            result = ToolOutput(success=True, data={"file_path": str(path), "content": _cat_n(text),
                                                    "line_count": len(text.splitlines())})
        elif wrapper == "codex":
            tool, args = "exec_command", {"cmd": f"cat -n {name}"}
            result = {"content": "Chunk ID: fixture\nWall time: 0.001 seconds\n"
                      "Process exited with code 0\nOriginal token count: 50\nOutput:\n" + _cat_n(text)}
        else:
            tool, args = "read", {}  # STARTED arguments may still be empty.
            result = _opencode(path, text)
            if wrapper == "mcp_text":
                result = {"content": result}
            elif wrapper == "mcp_blocks":
                result = {"content": [{"type": "text", "text": result}]}
        trace.observe_parent_read(tool, args, result)
    _check_parent_acceptance(trace)
    assert trace.parent_report_read_hashes[-1]["sha256"] == hashlib.sha256(REPORT.removesuffix("\n").encode()).hexdigest()


@pytest.mark.parametrize("mutation", ["stale", "whitespace", "locator", "added", "missing", "ordered_list"])
def test_report_must_equal_final_content_not_only_have_sources_heading(trace, mutation):
    _full_sources(trace)
    changed = {
        "stale": REPORT.replace("42 seconds", "41 seconds"),
        "whitespace": REPORT.replace("42 seconds", "42  seconds"),
        "locator": REPORT.replace("source-a.md:3", "source-a.md:4"),
        "added": REPORT + "Unreviewed sentence.\n",
        "missing": REPORT.replace("# Research Report\n\n", ""),
        "ordered_list": REPORT.replace("42 seconds", "1: 42 seconds"),
    }[mutation]
    trace.observe_parent_read("read_file", {"file_path": "research-report.md"}, {"content": changed})
    assert trace.parent_report_reads  # Actual read, but of the wrong report content.
    with pytest.raises(AssertionError, match="final reviewed report content"):
        _check_parent_acceptance(trace)


def test_report_read_before_last_review_does_not_accept_later_revision(trace):
    _full_sources(trace)
    trace.observe_parent_read("read_file", {"file_path": "research-report.md"}, {"content": REPORT})
    trace.events.append({"stage": "research_review", "seconds": trace.parent_report_reads[-1] + 1})
    with pytest.raises(AssertionError, match="final reviewed report content"):
        _check_parent_acceptance(trace)


@pytest.mark.parametrize("tool,args,result_kind", [
    ("read", {}, "foreign_path"),
    ("read", {}, "missing_path"),
    ("read_file", {"file_path": "elsewhere/source-a.md"}, "plain"),
    ("read_file", {"file_path": "source-a.md.bak"}, "plain"),
    ("read_file", {"file_path": "source-a.md"}, "failed_native"),
    ("read", {}, "failed_mcp"),
    ("write_file", {"file_path": "source-a.md"}, "plain"),
])
def test_wrong_path_or_failed_or_non_read_tool_is_not_evidence(trace, tool, args, result_kind):
    text = (trace.root / "source-a.md").read_text()
    result = {"content": text}
    if result_kind == "foreign_path":
        result = _opencode(trace.root / "other/source-a.md", text)
    elif result_kind == "missing_path":
        result = {"content": _cat_n(text)}
    elif result_kind == "failed_native":
        result = ToolOutput(success=False, data={"content": _cat_n(text)}, error="failed")
    elif result_kind == "failed_mcp":
        result = {"isError": True, "content": [{"type": "text", "text": _opencode(trace.root / "source-a.md", text)}]}
    trace.observe_parent_read(tool, args, result)
    assert trace.parent_source_reads == set()


def test_completed_read_path_overrides_misleading_started_args(trace):
    trace.observe_parent_read("read", {"filePath": str(trace.root / "source-a.md")},
                              _opencode(trace.root / "other.md", (trace.root / "source-a.md").read_text()))
    assert trace.parent_source_reads == set()


def test_numbered_partial_report_is_not_a_complete_read(trace):
    _full_sources(trace)
    text = _cat_n(REPORT)
    text = text.replace("     1\t", "     2\t", 1)
    trace.observe_parent_read("read_file", {"file_path": "research-report.md"}, {"content": text})
    with pytest.raises(AssertionError, match="final reviewed report content"):
        _check_parent_acceptance(trace)


def test_save_keeps_hashes_without_copied_read_contents(trace, monkeypatch, tmp_path):
    _full_sources(trace)
    trace.observe_parent_read("read_file", {"file_path": "research-report.md"}, {"content": REPORT})
    destination = tmp_path / "evidence"
    monkeypatch.setenv("WORK_RESEARCH_EVIDENCE_DIR", str(destination))
    trace.save()
    timing = json.loads((destination / "fixture/timing.json").read_text())
    assert timing["parent_report_read_hashes"] == trace.parent_report_read_hashes
    assert all(set(record) == {"seconds", "sha256"} for record in timing["parent_report_read_hashes"])
    assert REPORT not in json.dumps(timing)
