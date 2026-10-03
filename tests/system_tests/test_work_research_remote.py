# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Opt-in real model research delegation and evidence artifacts.

RUN_WORK_RESEARCH_REMOTE=1 with WORK_RESEARCH_API_BASE / WORK_RESEARCH_API_KEY
and optionally WORK_RESEARCH_MODEL. Credentials are passed to the existing
Provider configuration; the test does not log or persist them itself.

OpenCode explicitly requests an 8192-token output budget and requires a core
that supports OpenCodeModelConfig.max_output_tokens. Running with a local core
candidate is joint source validation, not locked dependency acceptance.
"""

from __future__ import annotations

import asyncio
import copy
import functools
import hashlib
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
        re.search(r"Pilot\s+A|source-a\.md", paragraph, re.I)
        and re.search(r"network", paragraph, re.I)
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
    _check_absent_dependency_claims(findings)
    _check_local_index_locator(report)
    return ledger


def _check_absent_dependency_claims(findings: str) -> None:
    """Reject assertions of absence, preserving an explicitly negated that-clause."""
    for claim in re.finditer(
        r"(?:untested (?:network )?requirement|network (?:requirement|dependency))"
        r"\s+(?:is|remains|was|means)\s+(?:absent|not required|zero)\b",
        findings,
        re.I,
    ):
        # Only the immediately governing negation qualifies. A negation in an
        # earlier sentence/clause cannot excuse a later assertion of absence.
        negated = re.search(
            r"\bnot\s+(?:(?:evidence|proof)\s+)?that\s+(?:(?:a|the)\s+)?$",
            findings[:claim.start()],
            re.I,
        )
        assert negated, "Untested evidence was converted into an absent dependency"


def _check_exclusive_network_claims(report: str) -> None:
    """Unknown Pilot A cannot be excluded by saying only Pilot B needs a network."""
    findings = report.split("## Sources", 1)[0]
    for claim in re.finditer(
        r"\bonly\s+(?:pilot\s+b|source-b(?:\.md)?)\s+"
        r"(?:requires?|required|needs?|needed)\b[^.!?\n]*\bnetwork\b",
        findings,
        re.I,
    ):
        negated = re.search(
            r"\b(?:does not (?:prove|establish|show|mean)|"
            r"(?:cannot|can't) (?:conclude|infer)|not (?:evidence|proof))\s+that\s+$",
            findings[:claim.start()],
            re.I,
        )
        assert negated, (
            "Unknown Pilot A cannot support an exclusive network requirement for Pilot B"
        )


def _check_single_record_citations(report: str) -> None:
    """The observation on line 3 supports what each log records, not total runs."""
    findings = report.split("## Sources", 1)[0]
    record_claim = re.compile(
        r"(?P<subject>each (?:log|source)|both (?:logs|sources)|"
        r"(?:pilot|source|log)[ -][ab](?:\.md)?)\s+"
        r"(?:records?|recorded|reports?|reported)\s+"
        r"(?:a single|single|one)\s+(?:primary\s+)?(?:run|observation)\b",
        re.I,
    )
    for sentence in re.split(r"(?<=[.!?])\s+|\n\s*\n", findings):
        for claim in record_claim.finditer(sentence):
            subject = claim["subject"].lower()
            letters = ("a", "b") if subject.startswith(("each", "both")) else (
                re.search(r"[ -]([ab])(?:\.md)?$", subject)[1],
            )
            for letter in letters:
                source = f"source-{letter}.md"
                assert any(
                    1 <= first <= 3 <= last <= 4
                    for first, last in _source_citation_ranges(sentence, source)
                ), f"Single-record fact lacks adjacent {source} observation citation"


def _source_citation_ranges(text: str, source: str) -> list[tuple[int, int]]:
    return [
        (int(match[1]), int(match[2] or match[1]))
        for match in re.finditer(
            re.escape(source)
            + r"`?\s*(?:[:,]\s*L?\s*|L\s*|lines?\s+)(\d+)"
            r"(?:\s*[-–]\s*L?(\d+))?\b",
            text,
            re.I,
        )
    ]


def _check_report_citation_coverage(root: Path, report: str) -> None:
    """Check dates and whole-source omissions against this canary's source ranges."""
    findings = report.split("## Sources", 1)[0]
    sources = {
        name: (root / name).read_text().splitlines()
        for name in ("source-a.md", "source-b.md")
    }
    for source, lines in sources.items():
        for line_number, line in enumerate(lines, 1):
            for date in re.findall(r"\b\d{4}-\d{2}-\d{2}\b", line):
                # A complete sentence may combine two dated facts with separate
                # references; never borrow a reference from a later sentence.
                for sentence in re.split(r"(?<=[.!?])\s+|\n\s*\n", findings):
                    if date in sentence:
                        assert any(
                            first <= line_number <= last <= len(lines)
                            for first, last in _source_citation_ranges(sentence, source)
                        ), f"Date {date} lacks its adjacent {source} citation"
    limitations = findings.split("## Limitations", 1)[-1]
    omission = re.compile(
        r"\b(?:do(?:es)? not|don't|doesn't)\s+(?:report|describe|specify)\b"
        r"|\b(?:no|neither)\b[^.!?\n]*\breport(?:s|ed)?\b"
        r"|\bnot\s+(?:been\s+)?reported\b",
        re.I,
    )
    for paragraph in re.split(r"\n\s*\n", limitations):
        if not omission.search(paragraph):
            continue
        for source, lines in sources.items():
            assert (1, len(lines)) in _source_citation_ranges(paragraph, source), (
                f"Whole-source omission lacks inspected full range for {source}"
            )


def _check_local_index_locator(report: str) -> None:
    """Check the concrete compound-claim citation missed in the OpenCode run."""
    findings = report.split("## Sources", 1)[0]
    for match in re.finditer(r"indexed locally", findings, re.I):
        adjacent = findings[match.end() :].split("\n", 1)[0]
        # The first reference after this fact owns its locator. Do not skip a
        # filename-only/wrong reference and borrow a later sentence's good one.
        citation = re.search(r"`?source-a\.md`?", adjacent, re.I)
        assert citation is not None, (
            "Local indexing claim lacks an adjacent source-a citation"
        )
        assert not re.search(r"[.!?]\s+\S", adjacent[: citation.start()]), (
            "Local indexing claim cannot borrow a later sentence's citation"
        )
        locator = re.match(
            r"\s*(?:[:,]\s*L?\s*|L\s*|lines?\s+)(\d+)(?:\s*[-–]\s*L?(\d+))?\b",
            adjacent[citation.end() :],
            re.I,
        )
        assert locator is not None, (
            "Local indexing claim lacks an adjacent source-a line locator"
        )
        first, last = int(locator[1]), int(locator[2] or locator[1])
        assert 1 <= first <= 4 <= last <= 4, (
            "Local indexing claim is on source-a.md line 4, not line 3"
        )


def _check_condition_comparison_citations(report: str) -> None:
    """A comparison caveat alone cannot locate both specific observed modes."""
    for paragraph in re.split(r"\n\s*\n", report.split("## Sources", 1)[0]):
        if not re.search(r"\boffline\s+(?:vs\.?|versus|and)\s+connected\b", paragraph, re.I):
            continue
        for source in ("source-a.md", "source-b.md"):
            assert any(
                first <= 3 <= last <= 4
                for first, last in _source_citation_ranges(paragraph, source)
            ), f"Specific run-condition comparison lacks {source} observation citation"


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
    from openjiuwen.harness.rails.sys_operation_rail import SysOperationRail
    from jiuwenswarm.agents.harness.work.research_parent import WorkResearchTaskPromptRail

    return create_deep_agent(
        model=model,
        workspace=str(root),
        sys_operation=operation,
        subagents=[spec],
        rails=[SysOperationRail(), WorkResearchTaskPromptRail(enable_subagent_runtime=True), trace_rail],
        enable_subagent_runtime=True,
        max_iterations=12,
        enable_task_loop=False,
        enable_read_image_multimodal=False,
        system_prompt=(
            "Fulfill the user's research request using the available research_agent. "
            "Honor the loaded Work research acceptance policy and the admitted filesystem scope. "
            "If the child fails or returns no result, report the failure; never search outside the workspace."
        ),
    )


# The initial child retains its original bound. Only an observed completed
# delivery enables the separately bounded parent acceptance/one-revision phase.
_FIRST_DELIVERY_BUDGET_S = 360
_ACCEPTANCE_BUDGET_S = 240
_RUN_BUDGET_S = _FIRST_DELIVERY_BUDGET_S + _ACCEPTANCE_BUDGET_S
_CLEANUP_BUDGET_S = 30
_RESEARCH_TASK = (
    "Compare the two recorded retrieval runs in source-a.md and source-b.md: what do they "
    "establish about offline retrieval and benchmark comparability? These are two temporary "
    "four-line synthetic field logs, not user documents. Read both original files with tools. "
    "Write research-report.md (at most 300 words) with Scope, Findings, Limitations and Sources, "
    "adjacent citations, both numeric observations and the comparability caveat. "
    "Put a source filename plus exact line locator immediately after every factual claim "
    "(file:line, file:Lline or file, Lline are acceptable). If a sentence combines facts "
    "from different lines, cite each fact separately or give the covering line range. "
    "A later sentence's citation does not locate an earlier claim. Read the original "
    "numbered lines to determine every locator; do not guess. "
    + _EVIDENCE_REQUEST
    + " Preserve research-review-input.json with the final sources/claims/question tool input "
    "without changing or summarizing its schema (retain source text and all claim refs), "
    "and research-audit.json with checked_claim_ids, the exact last tool-returned "
    "input_fingerprint and unresolved_issues. "
    + " Read back and verify all four outputs. Use only these sources, no web searches. "
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
        self.reviews = []
        self.model_request_contracts = []
        self.parent_source_reads = set()
        self.parent_report_reads = []
        self.parent_revision_count = 0
        self.parent_final_text = ""
        self.spawned_child_ids = set()
        self.parent_revision_targets = []
        self.execution_timeout = None
        self.execution_started = None
        self.first_delivery_seconds = None

    def enable_acceptance_budget(self, child_ids):
        if self.first_delivery_seconds is not None or self.execution_timeout is None:
            return
        if len(self.spawned_child_ids) != 1 or child_ids != self.spawned_child_ids:
            return
        now = asyncio.get_running_loop().time()
        if now > self.execution_started + _FIRST_DELIVERY_BUDGET_S:
            return
        self.first_delivery_seconds = round(now - self.execution_started, 3)
        self.execution_timeout.reschedule(min(
            self.execution_started + _RUN_BUDGET_S, now + _ACCEPTANCE_BUDGET_S,
        ))
        self.mark("first_child_completed", acceptance_budget_seconds=_ACCEPTANCE_BUDGET_S)

    def observe_parent_control(self, tool, arguments, result):
        name = tool.rsplit(".", 1)[-1]
        if name.endswith("subagent_spawn"):
            self.spawned_child_ids.update(re.findall(
                r"\b[\w-]+_sub_[\w-]+\b", json.dumps(result, default=str),
            ))
        elif name.endswith("subagent_wait"):
            data = getattr(result, "data", result)
            completed = set()
            if isinstance(data, dict) and isinstance(data.get("statuses"), dict):
                completed = {sid for sid, status in data["statuses"].items() if status == "completed"}
            else:
                # External adapters project the host's rendered tool result.
                # Read only its status header, before child-authored result text.
                if isinstance(data, dict):
                    data = data.get("content", data.get("text", ""))
                if isinstance(data, list):
                    data = "\n".join(part.get("text", "") for part in data if isinstance(part, dict))
                if isinstance(data, str):
                    header = data.split("\nresult:", 1)[0]
                    completed.update(re.findall(
                        r"(?:^|\n)subagent_id: ([\w-]+)\nstatus: completed(?:\n|$)", header,
                    ))
            self.enable_acceptance_budget(completed)
        elif name.endswith("subagent_send_input"):
            try:
                args = json.loads(arguments) if isinstance(arguments, str) else arguments
            except ValueError:
                args = None
            child = args.get("subagent_id") if isinstance(args, dict) else None
            if child is None:
                # OpenCode can publish STARTED before arguments are available;
                # the completed product result retains the resolved child ID.
                resolved = set(re.findall(r"\b[\w-]+_sub_[\w-]+\b", json.dumps(result, default=str)))
                if len(resolved) == 1:
                    child = resolved.pop()
            self.parent_revision_targets.append(child)

    def observe_parent_read(self, tool, arguments, result):
        name = tool.rsplit(".", 1)[-1].lower()
        if name not in {"read_file", "read", "exec_command", "bash", "shell"}:
            return
        args = json.dumps(arguments, default=str)
        output = json.dumps(result, default=str)
        def read_path(name):
            # A completed OpenCode read includes its actual path even if the
            # earlier projected STARTED event still had empty arguments.
            return name in args or f"<path>{self.root / name}</path>" in output
        for source in ("source-a.md", "source-b.md"):
            original = (self.root / source).read_text().splitlines()
            if read_path(source) and all(line in output for line in original[2:]):
                self.parent_source_reads.add(source)
                self.mark("parent_source_read", source=source, tool=tool)
        if read_path("research-report.md") and "## Sources" in output:
            elapsed = round(time.monotonic() - self.started, 3)
            self.parent_report_reads.append(elapsed)
            self.mark("parent_report_read", tool=tool)

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
            for name in ("research-report.md", "research-evidence.json", "research-review-input.json", "research-audit.json"):
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
            "research-review-input.json",
            "research-audit.json",
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
                    "first_delivery_budget_seconds": _FIRST_DELIVERY_BUDGET_S,
                    "acceptance_budget_seconds": _ACCEPTANCE_BUDGET_S,
                    "first_delivery_seconds": self.first_delivery_seconds,
                    "cleanup_budget_seconds": _CLEANUP_BUDGET_S,
                    "report_word_budget": 300,
                    "child_reply_word_budget": 40,
                    "child_providers": self.child_providers,
                    "parent_source_reads": sorted(self.parent_source_reads),
                    "parent_report_reads": self.parent_report_reads,
                    "parent_revision_count": self.parent_revision_count,
                    "spawned_child_ids": sorted(self.spawned_child_ids),
                    "parent_revision_targets": self.parent_revision_targets,
                    "events": self.events,
                },
                indent=2,
            )
        )
        # Only the synthetic source/draft and public tool result are captured.
        # This observer neither changes tool feedback nor supplies correct answers.
        (output / "review-calls.json").write_text(
            json.dumps(self.reviews, ensure_ascii=False, indent=2)
        )
        (output / "parent-final.txt").write_text(self.parent_final_text)
        (output / "model-request-contracts.json").write_text(
            json.dumps(self.model_request_contracts, indent=2)
        )


def _observe_reviews(monkeypatch, trace):
    from jiuwenswarm.agents.harness.work import research_review

    original = research_review.review_research_report

    @functools.wraps(original)
    def observed(sources, claims, question=None):
        result = original(sources, claims, question)
        trace.reviews.append(copy.deepcopy({
            "sources": sources, "claims": claims, "question": question,
            "result": result,
        }))
        trace.mark("research_review", structural_valid=result["structural_valid"])
        return result

    monkeypatch.setattr(research_review, "review_research_report", observed)


def _record_native_request(trace, messages, tools):
    """Check the final model boundary, never archive prompts or model reasoning."""
    from jiuwenswarm.agents.harness.work.research_parent import work_research_parent_instructions

    def field(value, name, default=None):
        return value.get(name, default) if isinstance(value, dict) else getattr(value, name, default)

    system = "\n".join(str(field(message, "content", "")) for message in messages
                       if field(message, "role") == "system")
    names = {field(tool, "name") or field(field(tool, "function", {}), "name") for tool in tools or []}
    child = "## Evidence-to-report procedure" in system or "review_research_report" in names
    scope = "child" if child else "parent"
    present = (
        "## Evidence-to-report procedure" in system
        and "Copy the full `input_fingerprint` directly" in system
        and "review_research_report" in names
    ) if child else any(work_research_parent_instructions(lang).strip() in system for lang in ("en", "cn"))
    trace.model_request_contracts.append({
        "scope": scope, "system_sha256": hashlib.sha256(system.encode()).hexdigest(),
        "system_characters": len(system), "policy_present": present,
        "tool_names": sorted(name for name in names if isinstance(name, str)),
    })
    assert present, f"Native {scope} research policy missing at final model request boundary"


def _observe_native_model_requests(monkeypatch, trace):
    from openjiuwen.core.foundation.llm import Model

    invoke, stream = Model.invoke, Model.stream

    @functools.wraps(invoke)
    async def observed_invoke(self, messages, **kwargs):
        _record_native_request(trace, messages, kwargs.get("tools"))
        return await invoke(self, messages, **kwargs)

    @functools.wraps(stream)
    async def observed_stream(self, messages, **kwargs):
        _record_native_request(trace, messages, kwargs.get("tools"))
        async for chunk in stream(self, messages, **kwargs):
            yield chunk

    monkeypatch.setattr(Model, "invoke", observed_invoke)
    monkeypatch.setattr(Model, "stream", observed_stream)


def _check_review_delivery(root, report, reviews, max_reviews=3):
    """Bind the final artifact to actual review calls and original fixture bytes."""
    assert 1 <= len(reviews) <= max_reviews, "Review calls exceeded the bounded delivery budget"
    final = reviews[-1]
    assert final["result"]["structural_valid"] is True
    from tests.unit_tests.test_work_research_delivery_fidelity import matches_reviewed_report

    assert matches_reviewed_report(report, final["result"]["rendered_markdown"], final["sources"]), (
        "Final report differs from the reviewed rendering"
    )
    assert len(report.split()) <= 300, "Report exceeds the requested word budget"
    seen = set()
    for source in final["sources"]:
        name = Path(source["id"]).name
        assert name in {"source-a.md", "source-b.md"}
        assert name not in seen
        seen.add(name)
        original = (root / name).read_text().splitlines()
        supplied = source["text"].splitlines()
        first = source["start_line"] - 1
        assert supplied == original[first:first + len(supplied)], (
            "Review input does not match the original source"
        )
        if source["complete"]:
            assert first == 0 and supplied == original
    assert seen == {"source-a.md", "source-b.md"}


def _check_parent_acceptance(trace):
    assert trace.parent_revision_count <= 1, "Parent exceeded one same-child revision"
    assert len(trace.spawned_child_ids) == 1, "Expected one original research child"
    assert all(child in trace.spawned_child_ids for child in trace.parent_revision_targets)
    assert trace.parent_source_reads == {"source-a.md", "source-b.md"}, (
        "Parent did not independently read both original source files"
    )
    last_review = max(e["seconds"] for e in trace.events if e["stage"] == "research_review")
    assert trace.parent_report_reads and trace.parent_report_reads[-1] >= last_review, (
        "Parent did not read the final reviewed report"
    )
    final = trace.reviews[-1]
    saved = json.loads((trace.root / "research-review-input.json").read_text())
    assert saved["sources"] == final["sources"]
    assert saved["claims"] == final["claims"]
    assert saved.get("question") == final.get("question")
    audit = json.loads((trace.root / "research-audit.json").read_text())
    assert set(audit["checked_claim_ids"]) == {c["id"] for c in final["claims"]}
    assert audit["input_fingerprint"] == final["result"]["input_fingerprint"]
    assert audit["unresolved_issues"] == []


def _check_report(root: Path):
    report = (root / "research-report.md").read_text()
    _check_evidence(root, report)
    _check_report_citation_coverage(root, report)
    _check_single_record_citations(report)
    _check_exclusive_network_claims(report)
    _check_condition_comparison_citations(report)
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
    tmp_path: Path, provider: str, monkeypatch
):
    if provider == "codex":
        pytest.importorskip("openai_codex")
    root = tmp_path / "workspace"
    _write_sources(root)
    trace = _ResearchTrace(provider, root)
    _observe_reviews(monkeypatch, trace)
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
            "startup_source_roots": [str(root), str(codex_home / "skills")],
            "mcp_required": True,
            "model": model,
            # Do not silently retry a stalled turn and multiply the test budget.
            "turn_idle_timeout_s": 100,
            "turn_idle_retries": 0,
        }
    else:
        model["max_output_tokens"] = 8192
        trace.mark("model_configured", max_output_tokens=model["max_output_tokens"])
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
    query = "Use the research agent for this assignment: " + _RESEARCH_TASK
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
    parent_calls = {}
    try:
        trace.mark("construction_start")
        await adapter.create_instance(mode="agent")
        runtime = adapter._subagent_runtime
        watcher = asyncio.create_task(trace.watch(runtime))
        adapter.select_execution_for_request(request)
        trace.mark("execution_start")
        trace.execution_started = asyncio.get_running_loop().time()
        async with asyncio.timeout(_FIRST_DELIVERY_BUDGET_S) as deadline:
            trace.execution_timeout = deadline
            async for chunk in adapter.process_message_stream_impl(
                request, {"query": query}
            ):
                payload = chunk.payload or {}
                event = payload.get("event_type")
                if event == "chat.tool_call":
                    call = payload.get("tool_call") or {}
                    parent_calls[call.get("tool_call_id")] = call
                    if call.get("name", "").endswith("subagent_send_input"):
                        trace.parent_revision_count += 1
                    trace.mark(
                        "parent_tool_start",
                        tool=(payload.get("tool_call") or {}).get("name", ""),
                    )
                elif event == "chat.tool_result":
                    trace.mark("parent_tool_end", tool=payload.get("tool_name", ""))
                    call = parent_calls.get(payload.get("tool_call_id"), {})
                    trace.observe_parent_read(
                        payload.get("tool_name", ""), call.get("arguments", call.get("args")),
                        payload.get("result"),
                    )
                    trace.observe_parent_control(
                        payload.get("tool_name", ""), call.get("arguments", call.get("args")),
                        payload.get("result"),
                    )
                elif event == "chat.delta":
                    parent_text.append(payload.get("content", ""))
                elif event == "chat.final":
                    terminal = payload.get("terminal_status")
                    trace.mark("parent_terminal", terminal=terminal)
        assert terminal == "completed", (
            "Provider stream closed without successful terminal"
        )
        trace.parent_final_text = "".join(parent_text)
        assert "research-report.md" in "".join(parent_text)
        assert trace.child_providers == [provider], (
            "Research child did not use the parent's Provider"
        )
        report = _check_report(root)
        _check_review_delivery(root, report, trace.reviews, max_reviews=6)
        _check_parent_acceptance(trace)
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
        trace.parent_final_text = "".join(parent_text)
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
async def test_work_research_real_native_cited_artifact(tmp_path: Path, monkeypatch):
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
    _observe_reviews(monkeypatch, trace)
    _observe_native_model_requests(monkeypatch, trace)
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
                assert {"subagent_spawn", "subagent_send_input", "read_file"} <= names
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
            if self.scope == "parent" and ctx.inputs.tool_name == "subagent_send_input":
                trace.parent_revision_count += 1
            trace.mark("tool_start", scope=self.scope, tool=ctx.inputs.tool_name)

        async def after_tool_call(self, ctx):
            trace.mark("tool_end", scope=self.scope, tool=ctx.inputs.tool_name)
            if self.scope == "parent":
                trace.observe_parent_read(ctx.inputs.tool_name, ctx.inputs.tool_args, ctx.inputs.tool_result)
                trace.observe_parent_control(ctx.inputs.tool_name, ctx.inputs.tool_args, ctx.inputs.tool_result)
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
        trace.execution_started = asyncio.get_running_loop().time()
        async with asyncio.timeout(_FIRST_DELIVERY_BUDGET_S) as deadline:
            trace.execution_timeout = deadline
            result = None
            async for chunk in Runner.run_agent_streaming(
                parent,
                {"query": "Use the research agent for this assignment: " + _RESEARCH_TASK},
                session="r1-12-native-session",
            ):
                # The product Native path streams. EOF is not a successful
                # terminal: core can also emit errors in an answer envelope.
                kind = chunk.get("type") if isinstance(chunk, dict) else getattr(chunk, "type", None)
                payload = chunk.get("payload") if isinstance(chunk, dict) else getattr(chunk, "payload", None)
                if kind == "llm_output" and isinstance(payload, dict):
                    trace.parent_final_text += str(payload.get("content", ""))
                if kind == "answer" and isinstance(payload, dict):
                    assert result is None, "Unexpected duplicate Native terminal"
                    result = payload
                    trace.parent_final_text = str(payload.get("output", ""))
                    trace.mark("parent_terminal", terminal=payload.get("result_type"))
        assert not child_failures, child_failures
        assert isinstance(result, dict) and result.get("result_type") == "answer", (
            result
        )
        assert "research-report.md" in str(result)
        report = _check_report(root)
        _check_review_delivery(root, report, trace.reviews, max_reviews=6)
        _check_parent_acceptance(trace)
        for name in (
            "subagent_spawn",
            "subagent_wait",
            "read_file",
            "write_file",
            "review_research_report",
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
