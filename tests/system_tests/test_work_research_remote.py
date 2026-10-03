# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Opt-in real model research delegation and evidence artifacts.

RUN_WORK_RESEARCH_REMOTE=1 with WORK_RESEARCH_API_BASE / WORK_RESEARCH_API_KEY
and optionally WORK_RESEARCH_MODEL. Credentials are passed to the existing
Provider configuration; the test does not log or persist them itself.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from contextlib import suppress
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
    assert any(
        "No network requirement was tested" in paragraph
        and re.search(
            r"unknown|untested|not (?:formally )?(?:verified|tested|proven)|does not (?:prove|establish)|not proof",
            paragraph,
            re.I,
        )
        for paragraph in re.split(r"\n\s*\n", findings)
    ), "Report never qualifies the untested requirement as unknown"
    assert not re.search(
        r"(?:meaning|confirm(?:s|ing))[^.\n]{0,100}(?:without network dependency|no network dependenc|network-independent)",
        report,
        re.I,
    ), "Untested evidence was converted into independence"
    _check_local_index_locator(report)
    return ledger


def _check_local_index_locator(report: str) -> None:
    """Check the concrete compound-claim citation missed in the OpenCode run."""
    findings = report.split("## Sources", 1)[0]
    for match in re.finditer(r"indexed locally", findings, re.I):
        adjacent = findings[match.end() :].split("\n", 1)[0]
        citation = re.search(
            r"source-a\.md[`*]?\s*(?::|L|lines?\s+)([34])(?:\s*[-–]\s*L?([34]))?",
            adjacent,
            re.I,
        )
        assert citation is not None, (
            "Local indexing claim lacks an adjacent source-a line locator"
        )
        first, last = int(citation[1]), int(citation[2] or citation[1])
        assert first <= 4 <= last, (
            "Local indexing claim is on source-a.md line 4, not line 3"
        )


def _native_wait_problem(result, root: Path) -> str | None:
    """Canary guard only: a completed empty child is not a research delivery."""
    data = getattr(result, "data", result)
    if not isinstance(data, dict) or not isinstance(data.get("statuses"), dict):
        return "unrecognized child wait result"
    for child_id, status in data["statuses"].items():
        if status in {"running", "pending"}:
            continue
        if status != "completed":
            return f"child ended with {status}"
        if not data.get("results", {}).get(child_id):
            return "completed child returned no result"
        if not all(
            (root / name).is_file()
            for name in ("research-report.md", "research-evidence.json")
        ):
            return "completed child did not create both requested artifacts"
    return None


def _native_research_work_config(root: Path):
    from openjiuwen.core.sys_operation import LocalWorkConfig
    from jiuwenswarm.agents.harness.work.research import _RESEARCH_SKILLS

    return LocalWorkConfig(
        restrict_to_sandbox=True,
        sandbox_root=[str(root), str(_RESEARCH_SKILLS)],
        # Empty allowlists mean unrestricted in the locked core. Use its explicit host deny hook.
        shell_allowlist=[],
        dangerous_patterns=[r"[\s\S]"],
    )


def _native_research_parent(model, root, operation, spec, trace_rail):
    from openjiuwen.harness import create_deep_agent

    return create_deep_agent(
        model=model,
        workspace=str(root),
        sys_operation=operation,
        subagents=[spec],
        rails=[trace_rail],
        enable_subagent_runtime=True,
        max_iterations=6,
        enable_task_loop=False,
        enable_read_image_multimodal=False,
        system_prompt=(
            "Delegate the complete assignment once to research_agent. Use only subagent_spawn "
            "and subagent_wait. If the child fails or returns no result, report failure immediately; "
            "never search for missing files. After successful child completion return only its artifact paths."
        ),
    )


_RUN_BUDGET_S = 360
_CLEANUP_BUDGET_S = 30
_RESEARCH_TASK = (
    "Compare the two recorded retrieval runs in source-a.md and source-b.md: what do they "
    "establish about offline retrieval and benchmark comparability? These are two temporary "
    "four-line synthetic field logs, not user documents. Read both original files with tools. "
    "Write research-report.md (at most 300 words) with Scope, Findings, Limitations and Sources, "
    "adjacent citations, both numeric observations and the comparability caveat. "
    + _EVIDENCE_REQUEST
    + " Read back and verify both outputs. Use only these sources, no web searches. "
    "Do not restate the complete report in your final reply: return paths and verification "
    "in at most 40 words. Batch independent source reads when supported."
)


def _write_sources(root: Path) -> None:
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


class _ResearchTrace:
    """Test-only observations: no source text, arguments, env or credentials."""

    def __init__(self, provider: str, root: Path):
        self.provider = provider
        self.root = root
        self.started = time.monotonic()
        self.events = []
        self.outcome = "running"
        self.child_providers = []

    def mark(self, stage: str, **details):
        self.events.append(
            {
                "stage": stage,
                "seconds": round(time.monotonic() - self.started, 3),
                **details,
            }
        )

    async def watch(self, runtime=None):
        seen = set()
        children = set()
        while True:
            for name in ("research-report.md", "research-evidence.json"):
                if name not in seen and (self.root / name).exists():
                    seen.add(name)
                    self.mark("artifact_observed", artifact=name)
            if runtime is not None:
                for child_id, child in tuple(runtime._factory._live.items()):
                    if child_id not in children:
                        children.add(child_id)
                        self.child_providers.append(child.binding.provider_id)
                        self.mark("child_bound", provider=child.binding.provider_id)
            await asyncio.sleep(0.25)

    def save(self):
        destination = os.environ.get("WORK_RESEARCH_EVIDENCE_DIR")
        if not destination:
            return
        output = Path(destination) / self.provider
        output.mkdir(parents=True, exist_ok=True)
        for name in (
            "source-a.md",
            "source-b.md",
            "research-report.md",
            "research-evidence.json",
        ):
            path = self.root / name
            if path.is_file():
                (output / name).write_bytes(path.read_bytes())
        (output / "timing.json").write_text(
            json.dumps(
                {
                    "provider": self.provider,
                    "outcome": self.outcome,
                    "execution_budget_seconds": _RUN_BUDGET_S,
                    "cleanup_budget_seconds": _CLEANUP_BUDGET_S,
                    "report_word_budget": 300,
                    "child_reply_word_budget": 40,
                    "child_providers": self.child_providers,
                    "events": self.events,
                },
                indent=2,
            )
        )


def _check_report(root: Path):
    report = (root / "research-report.md").read_text()
    _check_evidence(root, report)
    for fragment in ("42", "31", "source-a.md", "source-b.md"):
        assert fragment in report, f"Missing evidence {fragment}"
    assert any(
        word in report.lower()
        for word in (
            "not a controlled",
            "not directly",
            "different conditions",
            "cannot",
            "uncontrolled",
            "不能",
            "不可比",
        )
    ), "Missing benchmark comparability caveat"
    return report


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["codex", "opencode"])
async def test_work_research_real_external_cited_artifact(
    tmp_path: Path, provider: str
):
    if provider == "codex":
        pytest.importorskip("openai_codex")
    root = tmp_path / "workspace"
    _write_sources(root)
    trace = _ResearchTrace(provider, root)
    model = {
        "model": os.environ.get("WORK_RESEARCH_MODEL", "glm-5.2"),
        "provider": "work_research_remote",
        "api_base": os.environ["WORK_RESEARCH_API_BASE"],
        "api_key": os.environ["WORK_RESEARCH_API_KEY"],
    }
    if provider == "codex":
        home, codex_home = tmp_path / "home", tmp_path / "codex"
        for path in (home, codex_home, codex_home / "skills"):
            path.mkdir()
        provider_config = {
            "inherit_process_env": False,
            "env": {
                "HOME": str(home),
                "CODEX_HOME": str(codex_home),
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            },
            "mcp_required": True,
            "model": model,
            # Do not silently retry a stalled turn and multiply the test budget.
            "turn_idle_timeout_s": 100,
            "turn_idle_retries": 0,
        }
    else:
        cli = Path(
            os.environ.get(
                "WORK_RESEARCH_OPENCODE_CLI",
                str(Path.home() / ".opencode/bin/opencode"),
            )
        )
        assert cli.is_file(), (
            "A real OpenCode CLI is required for the selected provider"
        )
        runtime_root = tmp_path / "opencode-runtime"
        runtime_root.mkdir(mode=0o700)
        provider_config = {
            "cli_path": str(cli),
            "runtime_root": str(runtime_root),
            "model": model,
            "turn_timeout_s": _RUN_BUDGET_S,
        }
    spec = AgentExecutionSpec(
        provider,
        "r1-12-closure",
        authorization=ExecutionAuthorization(full_access=True),
        provider_config=provider_config,
    )
    route = _route(root, spec)
    adapter = EngineAgentAdapter(route)
    query = (
        "Use product subagent_spawn exactly once with subagent_type research_agent and pass "
        "the entire assignment below. Your role is only to delegate and wait. "
        "Then use subagent_wait with the exact returned ID and timeout_ms=60000; "
        "if still running wait again. When completed, return ONLY the two artifact paths, "
        "without opening files yourself or repeating the report. Assignment: "
        + _RESEARCH_TASK
    )
    request = AgentRequest(
        request_id="r1-12-research",
        channel_id="web",
        session_id="r1-a2-session",
        params={"mode": "agent", "query": query},
        is_stream=True,
    )
    request._execution_route = route
    runtime = None
    watcher = None
    terminal = None
    parent_text = []
    try:
        trace.mark("construction_start")
        await adapter.create_instance(mode="agent")
        runtime = adapter._subagent_runtime
        watcher = asyncio.create_task(trace.watch(runtime))
        adapter.select_execution_for_request(request)
        trace.mark("execution_start")
        async with asyncio.timeout(_RUN_BUDGET_S):
            async for chunk in adapter.process_message_stream_impl(
                request, {"query": query}
            ):
                payload = chunk.payload or {}
                event = payload.get("event_type")
                if event == "chat.tool_call":
                    trace.mark(
                        "parent_tool_start",
                        tool=(payload.get("tool_call") or {}).get("name", ""),
                    )
                elif event == "chat.tool_result":
                    trace.mark("parent_tool_end", tool=payload.get("tool_name", ""))
                elif event == "chat.delta":
                    parent_text.append(payload.get("content", ""))
                elif event == "chat.final":
                    terminal = payload.get("terminal_status")
                    trace.mark("parent_terminal", terminal=terminal)
        assert terminal == "completed", (
            "Provider stream closed without successful terminal"
        )
        assert "research-report.md" in "".join(parent_text)
        assert trace.child_providers == [provider], (
            "Research child did not use the parent's Provider"
        )
        _check_report(root)
        trace.mark("quality_gate_passed")
        trace.outcome = "passed"
    except BaseException as exc:
        trace.outcome = type(exc).__name__
        trace.mark(
            "failed",
            exception_type=type(exc).__name__,
            error_code=getattr(getattr(exc, "error", None), "code", None),
        )
        raise
    finally:
        trace.mark("cleanup_start")
        try:
            async with asyncio.timeout(_CLEANUP_BUDGET_S):
                await adapter.cleanup()
            assert runtime is None or not runtime.has_control()
            trace.mark("cleanup_complete")
        except BaseException as exc:
            trace.outcome = "cleanup_" + type(exc).__name__
            trace.mark("cleanup_failed", exception_type=type(exc).__name__)
            raise
        finally:
            if watcher is not None:
                watcher.cancel()
                with suppress(asyncio.CancelledError):
                    await watcher
            trace.save()


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
    )
    from openjiuwen.core.sys_operation.cwd import init_cwd
    from jiuwenswarm.agents.harness.work.research import build_research_agent_config

    root = tmp_path / "workspace"
    _write_sources(root)
    trace = _ResearchTrace("native", root)
    calls = []
    child_failures = []

    class Trace(AgentRail):
        def __init__(self, scope):
            self.scope = scope

        def fork_for_agent(self):
            return type(self)(self.scope)

        async def before_model_call(self, ctx):
            if self.scope == "parent":
                names = {tool.name for tool in ctx.inputs.tools or []}
                assert names and all(name.startswith("subagent_") for name in names), (
                    names
                )
                trace.mark("parent_tools_checked", tools=sorted(names))
                if child_failures:
                    ctx.request_force_finish(
                        {
                            "result_type": "error",
                            "output": "Research canary child failed",
                        }
                    )
            trace.mark(
                "model_start", scope=self.scope, iteration=ctx.inputs.react_iteration
            )

        async def after_model_call(self, ctx):
            trace.mark(
                "model_end",
                scope=self.scope,
                iteration=ctx.inputs.react_iteration,
                finish_reason=getattr(ctx.inputs.response, "finish_reason", None),
                output_tokens=getattr(
                    getattr(ctx.inputs.response, "usage_metadata", None),
                    "output_tokens",
                    None,
                ),
            )

        async def before_tool_call(self, ctx):
            calls.append(ctx.inputs.tool_name)
            trace.mark("tool_start", scope=self.scope, tool=ctx.inputs.tool_name)

        async def after_tool_call(self, ctx):
            trace.mark("tool_end", scope=self.scope, tool=ctx.inputs.tool_name)
            if self.scope == "parent" and ctx.inputs.tool_name == "subagent_wait":
                problem = _native_wait_problem(ctx.inputs.tool_result, root)
                if problem:
                    child_failures.append(problem)
                    trace.mark("child_delivery_failed", reason=problem)

    model = Model(
        model_client_config=ModelClientConfig(
            client_provider="OpenAI",
            api_base=os.environ["WORK_RESEARCH_API_BASE"],
            api_key=os.environ["WORK_RESEARCH_API_KEY"],
            timeout=90,
        ),
        model_config=ModelRequestConfig(
            model=os.environ.get("WORK_RESEARCH_MODEL", "glm-5.2"),
            temperature=0.1,
            max_tokens=8192,
        ),
    )
    card = SysOperationCard(
        id="r1-12-native",
        mode=OperationMode.LOCAL,
        work_config=_native_research_work_config(root),
    )
    parent = None
    watcher = asyncio.create_task(trace.watch())
    await Runner.start()
    try:
        trace.mark("construction_start")
        Runner.resource_mgr.add_sys_operation(card)
        operation = Runner.resource_mgr.get_sys_operation(card.id)
        init_cwd(str(root), workspace=str(root), project_root=str(root))
        spec = build_research_agent_config(
            model,
            workspace=str(root),
            sys_operation=operation,
            language="en",
            max_iterations=12,
        )
        spec.enable_read_image_multimodal = False
        spec.rails.append(Trace("child"))
        parent = _native_research_parent(model, root, operation, spec, Trace("parent"))
        trace.mark("execution_start", model_max_tokens=8192)
        async with asyncio.timeout(_RUN_BUDGET_S):
            result = await Runner.run_agent(
                parent,
                {
                    "query": (
                        "Spawn one research_agent with this complete assignment: "
                        "Use list_skill to inspect evidence-research and read its instructions, then "
                        + _RESEARCH_TASK
                        + " Parent: wait for the exact child ID with timeout_ms=240000, "
                        "wait again if running, then return only the two paths."
                    )
                },
                session="r1-12-native-session",
            )
        trace.mark("parent_terminal")
        assert not child_failures, child_failures
        assert isinstance(result, dict) and result.get("result_type") == "answer", (
            result
        )
        assert "research-report.md" in str(result)
        _check_report(root)
        for name in (
            "subagent_spawn",
            "subagent_wait",
            "list_skill",
            "read_file",
            "write_file",
        ):
            assert name in calls, f"Required actual tool call missing: {name}"
        assert spec.model is model
        trace.mark("quality_gate_passed")
        trace.outcome = "passed"
    except BaseException as exc:
        trace.outcome = type(exc).__name__
        trace.mark(
            "failed",
            exception_type=type(exc).__name__,
            error_code=getattr(getattr(exc, "error", None), "code", None),
        )
        raise
    finally:
        trace.mark("cleanup_start")
        try:
            async with asyncio.timeout(_CLEANUP_BUDGET_S):
                if parent is not None:
                    from openjiuwen.harness.tools.subagent import (
                        release_subagent_control,
                    )

                    await release_subagent_control(
                        parent, "r1-12-native-session", reason="test_finished"
                    )
                    await parent.stop()
                Runner.resource_mgr.remove_sys_operation(sys_operation_id=card.id)
                await Runner.stop()
            trace.mark("cleanup_complete")
        except BaseException as exc:
            trace.outcome = "cleanup_" + type(exc).__name__
            trace.mark("cleanup_failed", exception_type=type(exc).__name__)
            raise
        finally:
            watcher.cancel()
            with suppress(asyncio.CancelledError):
                await watcher
            trace.save()
