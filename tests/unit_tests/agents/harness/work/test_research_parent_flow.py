# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Scripted research acceptance over real product tools and subagent control.

Only the child execution and the parent's decisions are scripted. The gateway,
control, serialized turns, scoped parent filesystem reads and events are real.
The scripted child writes fixture bytes directly within its test workspace.
This verifies the existing correction channel, NOT a model's ability to discover
an unsupported claim or the causal effect of a prompt. Live semantic acceptance
is a separate gate. The original failing report remains immutable evidence.
"""
from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

import pytest

from openjiuwen.core.sys_operation import (
    LocalWorkConfig,
    OperationMode,
    SysOperation,
    SysOperationCard,
)
from openjiuwen.harness.subagent_runtime import SubagentTurnResult
from jiuwenswarm.runtime.harness.external_subagents import ExternalSubagentRuntime
from tests.system_tests.test_work_research_remote import _write_sources
from tests.unit_tests.runtime.harness.test_external_subagent_runtime import (
    _install_factory,
    _invoke,
    _route,
)

_FIXTURE = Path(__file__).parent / "fixtures/native-structured-partial-citation.md"
_FIXTURE_SHA256 = "167870d26b74e4c94f238a09a2059fc883396774ca5926c91d2ff09bced294ad"
_BAD = (
    "The two runs used different conditions (offline vs connected), so they are "
    "not a controlled benchmark (source-b.md:4)."
)
_REPAIRED = (
    "The two runs used different conditions (offline vs connected), so they are "
    "not a controlled benchmark (source-a.md:3; source-b.md:3-4)."
)
_CORRECTION = (
    "Recheck Limitations L1 against the original sources: its offline/connected "
    "clause currently cites only source-b.md:4, which does not describe those "
    "modes. Inspect source-a.md:3 and source-b.md:3 as well as the benchmark "
    "warning at source-b.md:4. Correct that claim's supporting ranges, preserve "
    "the original scope, and save the revised report to the same admitted path."
)


class _ScriptedChild:
    def __init__(self, factory, subagent_id):
        self.factory = factory
        self.subagent_id = subagent_id

    async def run_turn(self, request, *, on_chunk=None, on_result):
        del on_chunk
        self.factory.turns.append((self.subagent_id, request.task_id, request.query))
        turn = len(self.factory.turns)
        if turn == 1:
            text = self.factory.original
        else:
            assert turn == 2, "The bounded scripted correction must not loop"
            assert request.query == _CORRECTION
            text = (
                self.factory.original.replace(_BAD, _REPAIRED)
                if self.factory.outcome == "repaired"
                else self.factory.original
            )
        if self.factory.outcome != "missing":
            self.factory.report.write_text(text)
        await on_result(SubagentTurnResult(output=str(self.factory.report)))

    async def close(self, reason):
        self.factory.closed.append((self.subagent_id, reason))


class _ScriptedFactory:
    def __init__(self, report, original, outcome):
        self.report = report
        self.original = original
        self.outcome = outcome
        self.created = []
        self.turns = []
        self.closed = []

    async def create(self, request, context):
        self.created.append((request, context))
        return _ScriptedChild(self, request.subagent_id)

    async def can_restore(self, request, context):
        return False


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_id", ["codex", "opencode"])
@pytest.mark.parametrize("child_outcome", ["repaired", "unchanged", "missing"])
async def test_scripted_parent_reads_and_corrects_same_child_through_real_tools(
    tmp_path, monkeypatch, provider_id, child_outcome,
):
    from jiuwenswarm.agents.harness.work.research_parent import (
        work_research_parent_instructions,
    )

    # The shared policy is the scenario context, not a simulated truth oracle.
    # Its actual model-visible Native/External assembly is tested separately.
    policy = work_research_parent_instructions(language="en")
    assert "subagent_send_input" in policy and "subagent_wait" in policy
    original_bytes = _FIXTURE.read_bytes()
    assert hashlib.sha256(original_bytes).hexdigest() == _FIXTURE_SHA256
    original = original_bytes.decode()
    assert original.count(_BAD) == 1

    route = _route(tmp_path, provider_id=provider_id)
    root = route.runtime_paths.runtime_workspace_root
    source_fixture = tmp_path / "source-fixture"
    _write_sources(source_fixture)
    for name in ("source-a.md", "source-b.md"):
        (root / name).write_bytes((source_fixture / name).read_bytes())
    operation = SysOperation(SysOperationCard(
        id=f"parent-flow-{provider_id}-{child_outcome}",
        mode=OperationMode.LOCAL,
        work_config=LocalWorkConfig(
            sandbox_root=[str(root)], restrict_to_sandbox=True,
        ),
    ))
    report = root / "research-report.md"
    factory = _ScriptedFactory(report, original, child_outcome)
    _install_factory(monkeypatch, factory)
    events, calls, reads = [], [], []

    async def publish(chunk):
        events.append(chunk)

    runtime = ExternalSubagentRuntime(
        route, write_output=publish, work_research_enabled=True,
    )

    async def invoke(name, arguments):
        calls.append((name, arguments))
        result = await _invoke(runtime, name, arguments)
        assert not result.is_error, result.content
        return result

    async def read(path):
        reads.append(path)
        return await operation.fs().read_file(str(path), only_read=True)

    decision = None
    try:
        async with asyncio.timeout(15):
            await invoke("subagent_spawn", {
                "subagent_type": "research_agent",
                "task_description": "Compare the two supplied retrieval logs.",
                "display_name": "Researcher",
                "role": "Produce a cited comparison within the admitted workspace",
            })
            control = runtime._parent_host._subagent_controls["parent-a"]
            child_id, = [item.subagent_id for item in control.list_live()]
            first_wait = await invoke("subagent_wait", {
                "subagent_ids": [child_id], "timeout_ms": 10_000,
            })
            assert str(report) in first_wait.content
            candidate = await read(report)
            if child_outcome == "missing":
                assert candidate.code != 0
                decision = "partial: child completed without the reported artifact"
            else:
                assert candidate.code == 0 and candidate.data.content == original
                sources = []
                for name in ("source-a.md", "source-b.md"):
                    source = await read(root / name)
                    assert source.code == 0
                    sources.append(source.data.content.splitlines())
                # Deliberately scripted diagnosis from the preserved counterexample.
                # It is not a general semantic validator or a mocked tool result.
                assert "offline retrieval" in sources[0][2]
                assert "connected retrieval" in sources[1][2]
                assert "offline" not in sources[1][3]
                await invoke("subagent_send_input", {
                    "subagent_id": child_id, "query": _CORRECTION,
                })
                second_wait = await invoke("subagent_wait", {
                    "subagent_ids": [child_id], "timeout_ms": 10_000,
                })
                assert str(report) in second_wait.content
                revised = await read(report)
                assert revised.code == 0
                if child_outcome == "repaired":
                    assert _REPAIRED in revised.data.content
                    assert _BAD not in revised.data.content
                    decision = "accepted: supporting ranges corrected and report reread"
                else:
                    assert revised.data.content == original
                    decision = "partial: citation issue remains after one correction"

            # Even with a completed child, neither a missing artifact nor an
            # unsuccessful correction is represented as accepted.
            assert decision.startswith("accepted:") == (child_outcome == "repaired")
            assert len(factory.created) == 1
            assert factory.created[0][0].subagent_id == child_id
            assert factory.created[0][1].parent_subject_id == "owner-parent-a"
            assert factory.created[0][1].parent_session_id == "parent-a"
            assert {item[0] for item in factory.turns} == {child_id}
            expected_turns = 1 if child_outcome == "missing" else 2
            assert len(factory.turns) == expected_turns
            assert len({item[1] for item in factory.turns}) == expected_turns
            assert sum(name == "subagent_spawn" for name, _ in calls) == 1
            assert sum(name == "subagent_send_input" for name, _ in calls) == expected_turns - 1
            assert all(path.is_relative_to(root) for path in reads)
            assert reads.count(report) == expected_turns
            assert {event.type for event in events} >= {"subagent_updated", "subagent_message"}
            outside = tmp_path / "unrelated-source.md"
            outside.write_text("not admitted")
            denied = await operation.fs().read_file(str(outside), only_read=True)
            assert denied.code != 0
    finally:
        await runtime.close("test_complete")
    assert len(factory.closed) == 1
    assert _FIXTURE.read_bytes() == original_bytes
