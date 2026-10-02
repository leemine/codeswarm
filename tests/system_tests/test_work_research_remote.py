# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Opt-in real model + Codex research delegation and evidence artifact.

RUN_WORK_RESEARCH_REMOTE=1 with WORK_RESEARCH_API_BASE / WORK_RESEARCH_API_KEY
and optionally WORK_RESEARCH_MODEL. Credentials are passed to the existing
Provider configuration; the test does not log or persist them itself.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path

import pytest

from openjiuwen.harness_protocol import AgentExecutionSpec, ExecutionAuthorization
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.server.runtime.agent_adapter.engine_adapter import EngineAgentAdapter
from .test_external_codex_product_route_local import _route

pytestmark = [
    pytest.mark.integration,
    pytest.mark.system,
    pytest.mark.skipif(
        os.environ.get("RUN_WORK_RESEARCH_REMOTE") != "1",
        reason="real remote research is opt-in",
    ),
]

_EVIDENCE_REQUEST = (
    " Also write research-evidence.json with a sources object keyed by source-a.md and source-b.md. "
    "For each file extract network_requirement (choose exactly unknown, required_in_observed_run, "
    "or absent), an exact_quote about whether the network requirement was tested or required, "
    "and its integer line_number. Determine these values from actual numbered file reads. "
    "The report must distinguish an untested requirement from an absent dependency, and a "
    "requirement in an observed run from an architecture-wide conclusion. Before returning, "
    "read back both outputs and check every claim, quote and locator against the original files."
)


def _check_evidence(root: Path, report: str) -> dict:
    """Canary-specific evidence gate, independent of the model's completion claim."""
    ledger = json.loads((root / "research-evidence.json").read_text())
    sources = ledger["sources"]
    expected = {
        "source-a.md": ("unknown", "No network requirement was tested."),
        "source-b.md": (
            "required_in_observed_run",
            "A network connection was required.",
        ),
    }
    for source, (assessment, quote) in expected.items():
        entry = sources[source]
        assert entry["network_requirement"] == assessment, (
            f"Unsupported network conclusion: {source}"
        )
        assert entry["exact_quote"] == quote
        line = entry.get("line_number", entry.get("line"))
        assert type(line) is int
        lines = (root / source).read_text().splitlines()
        assert 1 <= line <= len(lines)
        assert quote in lines[line - 1], f"Incorrect source locator: {source}"
    # Reject the concrete overclaims and wrong citation seen in the first run.
    findings = report.split("## Sources", 1)[0]
    for paragraph in re.split(r"\n\s*\n", findings):
        if "A network connection was required" in paragraph:
            assert not re.search(
                r"A network connection was required[^)\n]{0,50}source-b\.md[^\n]{0,10}(?:line[s]?\s+|:)3\b",
                paragraph,
                re.I,
            )
        if "No network requirement was tested" in paragraph:
            assert re.search(
                r"unknown|untested|not (?:formally )?(?:verified|tested|proven)|does not (?:prove|establish)|not proof",
                paragraph,
                re.I,
            ), "Untested evidence was not qualified as unknown"
    assert not re.search(
        r"(?:meaning|confirm(?:s|ing))[^.\n]{0,100}(?:without network dependency|no network dependenc|network-independent)",
        report,
        re.I,
    ), "Untested evidence was converted into independence"
    return ledger


@pytest.mark.asyncio
async def test_work_research_real_codex_cited_artifact(tmp_path: Path):
    pytest.importorskip("openai_codex")
    root = tmp_path / "workspace"
    home = tmp_path / "home"
    codex_home = tmp_path / "codex"
    for path in (root, home, codex_home, codex_home / "skills"):
        path.mkdir()
    (root / "source-a.md").write_text(
        "# Pilot A field log\nDate: 2026-09-01\n"
        "Primary observation: offline retrieval took 42 seconds for 12 documents.\n"
        "All 12 documents were indexed locally. No network requirement was tested.\n",
    )
    (root / "source-b.md").write_text(
        "# Pilot B field log\nDate: 2026-09-02\n"
        "Primary observation: connected retrieval took 31 seconds for 12 documents.\n"
        "A network connection was required. The conditions differ from Pilot A; "
        "this is not a controlled benchmark and does not establish superiority.\n",
    )
    spec = AgentExecutionSpec(
        "codex",
        "r1-12-remote",
        authorization=ExecutionAuthorization(full_access=True),
        provider_config={
            "inherit_process_env": False,
            "env": {
                "HOME": str(home),
                "CODEX_HOME": str(codex_home),
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            },
            "mcp_required": True,
            "model": {
                "model": os.environ.get("WORK_RESEARCH_MODEL", "glm-5.2"),
                "provider": "work_research_remote",
                "api_base": os.environ["WORK_RESEARCH_API_BASE"],
                "api_key": os.environ["WORK_RESEARCH_API_KEY"],
            },
        },
    )
    route = _route(root, spec)
    adapter = EngineAgentAdapter(route)
    query = (
        "Use the product subagent_spawn tool once with subagent_type research_agent "
        "to compare source-a.md and source-b.md for offline suitability. Ask that child "
        "to inspect both files using tools, write research-report.md in this workspace, "
        "with Scope, Findings, Limitations and Sources sections, adjacent source citations, "
        "both numeric observations and the comparability caveat, then read back the report. "
        "Use subagent_wait with the exact returned ID until finished, and return the report path. "
        "Do not answer the research yourself or use external sources."
        + _EVIDENCE_REQUEST
    )
    request = AgentRequest(
        request_id="r1-12-research",
        channel_id="web",
        session_id="r1-a2-session",
        params={"mode": "agent", "query": query},
        is_stream=True,
    )
    request._execution_route = route
    chunks = []
    runtime = None
    try:
        await adapter.create_instance(mode="agent")
        runtime = adapter._subagent_runtime
        adapter.select_execution_for_request(request)
        async with asyncio.timeout(240):
            async for chunk in adapter.process_message_stream_impl(
                request, {"query": query}
            ):
                chunks.append(chunk.payload or {})
    finally:
        async with asyncio.timeout(30):
            await adapter.cleanup()
    report = (root / "research-report.md").read_text()
    ledger = _check_evidence(root, report)
    for fragment in ("42", "31", "source-a.md", "source-b.md"):
        assert fragment in report, f"Missing evidence {fragment}"
    assert any(
        word in report.lower()
        for word in (
            "not a controlled",
            "not directly",
            "different conditions",
            "不能",
            "不可比",
        )
    )
    rendered = json.dumps(chunks, ensure_ascii=False)
    assert "research_agent" in rendered
    assert "research-report.md" in rendered
    assert runtime is not None and not runtime.has_control()
    evidence_dir = os.environ.get("WORK_RESEARCH_EVIDENCE_DIR")
    if evidence_dir:
        output = Path(evidence_dir)
        output.mkdir(parents=True, exist_ok=True)
        (output / "research-report.md").write_text(report)
        (output / "research-evidence.json").write_text(json.dumps(ledger, indent=2))
        (output / "events.json").write_text(rendered)
        (output / "result.json").write_text(
            json.dumps(
                {
                    "provider": "codex",
                    "remote_model": os.environ.get("WORK_RESEARCH_MODEL", "glm-5.2"),
                    "sources": ["source-a.md", "source-b.md"],
                    "cleanup_complete": not runtime.has_control(),
                },
                indent=2,
            )
        )


@pytest.mark.asyncio
async def test_work_research_real_native_cited_artifact(tmp_path: Path):
    from openjiuwen.core.foundation.llm import (
        Model,
        ModelClientConfig,
        ModelRequestConfig,
    )
    from openjiuwen.core.runner import Runner
    from openjiuwen.core.single_agent.rail.base import AgentRail
    from openjiuwen.core.sys_operation import (
        SysOperationCard,
        OperationMode,
        LocalWorkConfig,
    )
    from openjiuwen.core.sys_operation.cwd import init_cwd
    from openjiuwen.harness import create_deep_agent
    from openjiuwen.harness.rails.sys_operation_rail import SysOperationRail
    from jiuwenswarm.agents.harness.work.research import build_research_agent_config

    root = tmp_path / "workspace"
    root.mkdir()
    (root / "source-a.md").write_text(
        "# Pilot A field log\nDate: 2026-09-01\n"
        "Primary observation: offline retrieval took 42 seconds for 12 documents.\n"
        "All 12 documents were indexed locally. No network requirement was tested.\n",
    )
    (root / "source-b.md").write_text(
        "# Pilot B field log\nDate: 2026-09-02\n"
        "Primary observation: connected retrieval took 31 seconds for 12 documents.\n"
        "A network connection was required. The conditions differ from Pilot A; "
        "this is not a controlled benchmark and does not establish superiority.\n",
    )
    calls = []

    class Trace(AgentRail):
        async def before_tool_call(self, ctx):
            calls.append(getattr(ctx.inputs, "tool_name", ""))

    model = Model(
        model_client_config=ModelClientConfig(
            client_provider="OpenAI",
            api_base=os.environ["WORK_RESEARCH_API_BASE"],
            api_key=os.environ["WORK_RESEARCH_API_KEY"],
            timeout=90,
        ),
        model_config=ModelRequestConfig(
            model=os.environ.get("WORK_RESEARCH_MODEL", "glm-5.2"), temperature=0.1
        ),
    )
    card = SysOperationCard(
        id="r1-12-native", mode=OperationMode.LOCAL, work_config=LocalWorkConfig()
    )
    parent = None
    await Runner.start()
    try:
        Runner.resource_mgr.add_sys_operation(card)
        operation = Runner.resource_mgr.get_sys_operation(card.id)
        init_cwd(str(root), workspace=str(root), project_root=str(root))
        spec = build_research_agent_config(
            model, workspace=str(root), sys_operation=operation, language="en"
        )
        spec.rails.append(Trace())
        parent = create_deep_agent(
            model=model,
            workspace=str(root),
            sys_operation=operation,
            subagents=[spec],
            rails=[SysOperationRail(), Trace()],
            enable_subagent_runtime=True,
            max_iterations=15,
            enable_task_loop=False,
            system_prompt="Delegate the requested research to research_agent using the existing product subagent tools. Wait for completion and return the artifact path.",
        )
        async with asyncio.timeout(240):
            result = await Runner.run_agent(
                parent,
                {
                    "query": (
                        "Delegate one research_agent to compare source-a.md and source-b.md for offline suitability. "
                        "Tell it to use list_skill to inspect evidence-research, read both files with tools, "
                        "write research-report.md with Scope, Findings, Limitations and Sources, adjacent citations, "
                        "both numeric observations and the comparability caveat, then read back the file. "
                        "Wait for the exact child ID until completion. Do not do the research yourself."
                        + _EVIDENCE_REQUEST
                    )
                },
                session="r1-12-native-session",
            )
        report = (root / "research-report.md").read_text()
        ledger = _check_evidence(root, report)
        for fragment in ("42", "31", "source-a.md", "source-b.md"):
            assert fragment in report
        assert "subagent_spawn" in calls
        assert "read_file" in calls
        assert "write_file" in calls
        assert "list_skill" in calls
        evidence_dir = os.environ.get("WORK_RESEARCH_EVIDENCE_DIR")
        if evidence_dir:
            output = Path(evidence_dir) / "native"
            output.mkdir(parents=True, exist_ok=True)
            (output / "research-report.md").write_text(report)
            (output / "research-evidence.json").write_text(json.dumps(ledger, indent=2))
            (output / "tools.json").write_text(json.dumps(calls))
            (output / "result.txt").write_text(str(result))
    finally:
        if parent is not None:
            from openjiuwen.harness.tools.subagent import release_subagent_control

            async with asyncio.timeout(30):
                await release_subagent_control(
                    parent, "r1-12-native-session", reason="test_finished"
                )
                await parent.stop()
        Runner.resource_mgr.remove_sys_operation(sys_operation_id=card.id)
        await Runner.stop()
