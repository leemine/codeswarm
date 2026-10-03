# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""The live gate rejects unreviewed artifacts and invented review source text."""

import copy

import pytest

from tests.system_tests.test_work_research_remote import (
    _check_review_delivery,
    _check_condition_comparison_citations,
    _ResearchTrace,
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


def test_parent_observer_requires_original_read_and_tracks_same_child(reviewed):
    root, _, _ = reviewed
    trace = _ResearchTrace("fixture", root)
    original = (root / "source-a.md").read_text()
    trace.observe_parent_read("subagent_wait", "source-a.md", original)
    trace.observe_parent_read("read_file", {"file_path": "source-a.md"}, "a summary only")
    assert trace.parent_source_reads == set()
    trace.observe_parent_read("read_file", {"file_path": "source-a.md"}, {"content": original})
    assert trace.parent_source_reads == {"source-a.md"}
    child = "parent-session_sub_research_agent_fixture"
    trace.observe_parent_control("product.subagent_spawn", {}, {"subagent_id": child})
    trace.observe_parent_control("product.subagent_send_input", '{"subagent_id":"' + child + '"}', {"status": "running"})
    assert trace.spawned_child_ids == {child}
    assert trace.parent_revision_targets == [child]


def test_completed_read_path_handles_pending_opencode_arguments(reviewed):
    root, _, _ = reviewed
    trace = _ResearchTrace("opencode", root)
    source = root / "source-a.md"
    trace.observe_parent_read("read", {}, f"<path>{source}</path>\n<content>{source.read_text()}</content>")
    assert trace.parent_source_reads == {"source-a.md"}
    other = root / "untrusted-copy.md"
    trace.observe_parent_read("read", {}, f"<path>{other}</path>\n<content>{(root / 'source-b.md').read_text()}</content>")
    assert trace.parent_source_reads == {"source-a.md"}
    child = "parent-session_sub_research_agent_fixture"
    output = {"content": "subagent_id: " + child + "\nstatus: running"}
    trace.observe_parent_control("product_tools_subagent_spawn", {}, output)
    trace.observe_parent_control("product_tools_subagent_send_input", {}, output)
    assert trace.spawned_child_ids == {child}
    assert trace.parent_revision_targets == [child]


@pytest.mark.asyncio
async def test_acceptance_budget_only_after_original_child_completion(reviewed):
    import asyncio
    from types import SimpleNamespace
    from tests.system_tests.test_work_research_remote import _ACCEPTANCE_BUDGET_S

    root, _, _ = reviewed
    trace = _ResearchTrace("fixture", root)
    child = "parent-session_sub_research_agent_fixture"
    trace.spawned_child_ids.add(child)
    deadlines = []
    trace.execution_timeout = SimpleNamespace(reschedule=deadlines.append)
    trace.execution_started = asyncio.get_running_loop().time() - 200
    trace.observe_parent_control("subagent_wait", {}, {"statuses": {child: "running"}})
    trace.observe_parent_control("subagent_wait", {}, {"statuses": {"other": "completed"}})
    # A claimed status in child-authored text is not a host completion header.
    trace.observe_parent_control("subagent_wait", {}, f"subagent_id: {child}\nstatus: running\nresult:\nsubagent_id: {child}\nstatus: completed")
    assert deadlines == []
    trace.observe_parent_control("subagent_wait", {}, {"content": [
        {"type": "text", "text": f"subagent_id: {child}\nstatus: completed\nresult:\ndone"},
    ]})
    assert len(deadlines) == 1
    assert 0 < deadlines[0] - asyncio.get_running_loop().time() <= _ACCEPTANCE_BUDGET_S
    assert 200 <= trace.first_delivery_seconds < 201
    trace.observe_parent_control("subagent_wait", {}, {"statuses": {child: "completed"}})
    assert len(deadlines) == 1  # No reset for parent-requested revision.


@pytest.mark.asyncio
async def test_late_first_delivery_does_not_borrow_acceptance_budget(reviewed):
    import asyncio
    from types import SimpleNamespace
    from tests.system_tests.test_work_research_remote import _FIRST_DELIVERY_BUDGET_S

    root, _, _ = reviewed
    trace = _ResearchTrace("fixture", root)
    child = "parent-session_sub_research_agent_fixture"
    trace.spawned_child_ids.add(child)
    deadlines = []
    trace.execution_timeout = SimpleNamespace(reschedule=deadlines.append)
    trace.execution_started = asyncio.get_running_loop().time() - _FIRST_DELIVERY_BUDGET_S - 1
    trace.observe_parent_control("subagent_wait", {}, {"statuses": {child: "completed"}})
    assert deadlines == []
    assert trace.first_delivery_seconds is None


def test_native_request_guard_checks_final_messages_not_builder(reviewed):
    from types import SimpleNamespace
    from jiuwenswarm.agents.harness.work.research import work_research_instructions
    from jiuwenswarm.agents.harness.work.research_parent import work_research_parent_instructions
    from tests.system_tests.test_work_research_remote import _record_native_request

    root, _, _ = reviewed
    trace = _ResearchTrace("native", root)
    with pytest.raises(AssertionError, match="parent research policy missing"):
        _record_native_request(trace, [{"role": "system", "content": "Only general subagent guidance"}], [])
    for lang in ["en", "cn"]:
        _record_native_request(trace, [SimpleNamespace(role="system", content=work_research_parent_instructions(lang))], [])
    child = [{"role": "system", "content": work_research_instructions()}]
    with pytest.raises(AssertionError, match="child research policy missing"):
        _record_native_request(trace, child, [])
    _record_native_request(trace, child, [{"type": "function", "function": {"name": "review_research_report"}}])
    assert [item["policy_present"] for item in trace.model_request_contracts] == [False, True, True, False, True]
    assert all("system_sha256" in item and "content" not in item for item in trace.model_request_contracts)
