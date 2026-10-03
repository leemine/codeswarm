# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Regression guards for actual live research failures, without model calls."""

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from tests.system_tests.test_work_research_remote import (
    _check_evidence,
    _check_local_index_locator,
    _native_research_parent,
    _native_research_work_config,
    _native_wait_problem,
    _write_sources,
)


@pytest.mark.parametrize("conclusion", ["unknown", "absent", "not required"])
def test_actual_codex_faithful_paraphrase_keeps_unknown_gate(tmp_path, conclusion):
    # This validates the network canary, not the report's separately reviewed
    # limitations citations; the original report remains unmodified evidence.
    report = (
        Path(__file__).parent / "fixtures/codex-faithful-network-paraphrase.md"
    ).read_text()
    root = tmp_path / "workspace"
    _write_sources(root)
    ledger = {
        "sources": {
            "source-a.md": {
                "network_requirement": "unknown",
                "exact_quote": "No network requirement was tested.",
                "line_number": 4,
            },
            "source-b.md": {
                "network_requirement": "required_in_observed_run",
                "exact_quote": "A network connection was required.",
                "line_number": 4,
            },
        }
    }
    (root / "research-evidence.json").write_text(json.dumps(ledger))
    if conclusion == "unknown":
        assert _check_evidence(root, report) == ledger
    else:
        corrupted = report.replace(
            "untested requirement remains unknown",
            f"untested requirement remains {conclusion}",
        )
        assert corrupted != report
        with pytest.raises(AssertionError, match="absent dependency"):
            _check_evidence(root, corrupted)


def test_actual_opencode_report_rejects_compound_claim_wrong_line():
    report = (
        Path(__file__).parent / "fixtures/opencode-mislocated-indexing.md"
    ).read_text()
    with pytest.raises(AssertionError, match="line 4"):
        _check_local_index_locator(report)
    # Positive range covers both the measured retrieval and the local-indexing fact.
    _check_local_index_locator(report.replace("[source-a.md:3]", "[source-a.md:3-4]"))


@pytest.mark.parametrize(
    "citation",
    [
        "source-a.md:4",
        "source-a.md:L4",
        "source-a.md, L4",
        "source-a.md L4",
        "source-a.md line 4",
        "source-a.md:3-4",
        "source-a.md:L3-L4",
        "source-a.md, L3–L4",
        "`source-a.md`:4",
    ],
)
def test_local_index_locator_accepts_supported_syntax(citation):
    _check_local_index_locator(f"All 12 documents were indexed locally [{citation}].")


@pytest.mark.parametrize(
    "claim",
    [
        "All documents were indexed locally [source-a.md:3]. Network was untested [source-a.md, L4].",
        "All documents were indexed locally [source-a.md]. Network was untested [source-a.md, L4].",
        "All documents were indexed locally. Network was untested [source-a.md, L4].",
        "All documents were indexed locally [source-a.md:3-9].",
    ],
)
def test_local_index_locator_does_not_borrow_or_accept_invalid_range(claim):
    with pytest.raises(AssertionError):
        _check_local_index_locator(claim)


@pytest.mark.parametrize("status", ["failed", "cancelled"])
def test_failed_child_cannot_become_a_successful_delivery(tmp_path, status):
    assert _native_wait_problem({"statuses": {"child": status}}, tmp_path)


def test_empty_completed_and_missing_artifacts_fail_without_search(tmp_path):
    result = {"statuses": {"child": "completed"}, "results": {"child": ""}}
    assert (
        _native_wait_problem(result, tmp_path) == "completed child returned no result"
    )
    result["results"]["child"] = "research-report.md research-evidence.json"
    assert "did not create" in _native_wait_problem(result, tmp_path)
    for name in ("research-report.md", "research-evidence.json"):
        (tmp_path / name).write_text("fixture")
    assert _native_wait_problem(result, tmp_path) is None
    assert _native_wait_problem({"statuses": {"child": "running"}}, tmp_path) is None


@pytest.mark.asyncio
async def test_native_canary_parent_can_review_files_without_widening_permissions(tmp_path):
    from openjiuwen.core.runner import Runner
    from openjiuwen.core.single_agent.rail.base import AgentRail
    from openjiuwen.core.sys_operation import SysOperationCard, OperationMode
    from openjiuwen.core.sys_operation.cwd import init_cwd
    from jiuwenswarm.agents.harness.work.research import build_research_agent_config

    seen = []

    class StopBeforeModel(AgentRail):
        async def before_model_call(self, ctx):
            seen.extend(tool.name for tool in ctx.inputs.tools or [])
            ctx.request_force_finish(
                {"result_type": "answer", "output": "tools inspected"}
            )

    card = SysOperationCard(
        id="research-guard-tools",
        mode=OperationMode.LOCAL,
        work_config=_native_research_work_config(tmp_path),
    )
    parent = None
    await Runner.start()
    try:
        Runner.resource_mgr.add_sys_operation(card)
        operation = Runner.resource_mgr.get_sys_operation(card.id)
        init_cwd(str(tmp_path), workspace=str(tmp_path), project_root=str(tmp_path))
        model = MagicMock()
        spec = build_research_agent_config(
            model, workspace=str(tmp_path), sys_operation=operation
        )
        parent = _native_research_parent(
            model, tmp_path, operation, spec, StopBeforeModel()
        )
        result = await Runner.run_agent(
            parent,
            {"query": "Inspect registered tools only"},
            session="research-guard-tools",
        )
        assert result["result_type"] == "answer"
        assert {
            "subagent_spawn",
            "subagent_wait",
            "subagent_list",
            "subagent_send_input",
            "subagent_close",
            "subagent_resume",
            "read_file",
        } <= set(seen)
        denied = await operation.shell().execute_cmd(
            "printf canary-shell-must-be-denied"
        )
        assert denied.code != 0
        outside = tmp_path.parent / f"{tmp_path.name}-outside.txt"
        outside.write_text("outside fixture")
        try:
            assert (
                await operation.fs().read_file(str(outside), only_read=True)
            ).code != 0
        finally:
            outside.unlink()
    finally:
        if parent is not None:
            await parent.stop()
        Runner.resource_mgr.remove_sys_operation(sys_operation_id=card.id)
        await Runner.stop()
