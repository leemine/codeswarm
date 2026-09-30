# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Typed Surface projection over normalized External harness events."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from openjiuwen.core.session.stream.base import OutputSchema
from openjiuwen.harness_protocol import (
    HarnessEvent,
    ItemEventKind,
    ItemLifecycleEvent,
    TurnEventKind,
    TurnLifecycleEvent,
    TurnResult,
    TurnStatus,
    TurnTermination,
    TurnTerminationKind,
)
from openjiuwen.harness_providers.io_adapter import ProjectedOutput

from jiuwenswarm.runtime.harness.event_projection import ExternalEventProjection
from jiuwenswarm.runtime.harness.surface_projection import (
    SurfaceActivityKind,
    SurfaceResultProjection,
    classify_surface_activity,
)


def _event(
    event: ItemLifecycleEvent | TurnLifecycleEvent,
    *,
    sequence: int,
    turn_id: str = "turn-1",
    item_id: str | None = None,
) -> HarnessEvent:
    return HarnessEvent(
        sequence=sequence,
        timestamp=float(sequence),
        event=event,
        host_session_id="session-1",
        agent_id="external-1",
        turn_id=turn_id,
        item_id=item_id,
    )


def _terminal(kind: TurnEventKind) -> TurnLifecycleEvent:
    if kind is TurnEventKind.FINISHED:
        result = TurnResult(status=TurnStatus.COMPLETED)
    elif kind is TurnEventKind.ABORTED:
        result = TurnResult(
            status=TurnStatus.INTERRUPTED,
            termination=TurnTermination(TurnTerminationKind.USER_ABORT),
        )
    else:
        raise AssertionError(f"unsupported test terminal: {kind}")
    return TurnLifecycleEvent(kind, result)


@pytest.mark.parametrize(
    ("name", "arguments", "expected"),
    [
        ("apply_patch", {"path": "src/app.py"}, SurfaceActivityKind.FILE_CHANGE),
        ("read_file", {"path": "src/app.py"}, SurfaceActivityKind.CODE_NAVIGATION),
        ("shell", {"command": "pytest -q"}, SurfaceActivityKind.TEST),
        ("shell", {"command": "git diff --check"}, SurfaceActivityKind.DIFF),
        ("review_changes", {}, SurfaceActivityKind.REVIEW),
        ("browser_open", {}, SurfaceActivityKind.BROWSER),
        ("web_search", {}, SurfaceActivityKind.WEB),
        ("subagent_spawn", {}, SurfaceActivityKind.SUBAGENT),
        ("send_file_to_user", {}, SurfaceActivityKind.ARTIFACT),
        ("shell", {"command": "pwd"}, SurfaceActivityKind.TERMINAL),
    ],
)
def test_classifies_normalized_surface_activity(name, arguments, expected) -> None:
    assert classify_surface_activity(name, arguments) is expected


@pytest.mark.asyncio
async def test_code_file_history_waits_for_successful_provider_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenswarm.runtime.harness import surface_projection as module

    async def immediate_history_io(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    monkeypatch.setattr(module, "run_history_io", immediate_history_io)
    target = tmp_path / "app.py"
    target.write_text("old\n", encoding="utf-8")
    artifacts = []

    async def sink(artifact, path) -> None:
        artifacts.append((artifact, path))

    projection = SurfaceResultProjection(
        "session-1",
        work_mode="code",
        workspace_root=tmp_path,
        cwd=tmp_path,
        outputs_dir=None,
        provider_id="codex",
        artifact_sink=sink,
    )
    projection.register_turn("turn-1")
    await projection.observe(
        _event(
            ItemLifecycleEvent(
                ItemEventKind.STARTED,
                "tool",
                {"name": "apply_patch", "arguments": {"path": "app.py"}},
            ),
            sequence=1,
            item_id="item-1",
        )
    )
    target.write_text("new\n", encoding="utf-8")
    await projection.observe(
        _event(
            ItemLifecycleEvent(
                ItemEventKind.COMPLETED,
                "tool",
                {
                    "name": "apply_patch",
                    "arguments": {"path": "app.py"},
                    "status": "completed",
                },
            ),
            sequence=2,
            item_id="item-1",
        )
    )

    history_path = tmp_path / ".agent_history" / "file_ops_external_session-1.json"
    assert not history_path.exists()

    await projection.observe(
        _event(_terminal(TurnEventKind.FINISHED), sequence=3)
    )

    history = json.loads(history_path.read_text(encoding="utf-8"))
    from jiuwenswarm.server.utils.diff_service import DiffService

    assert DiffService._is_valid_file_ops_file(
        history_path.name,
        "session-1",
        require_session=True,
    )
    assert history[str(target)][0]["action"] == "edit"
    assert history[str(target)][0]["old_content"] == "old\n"
    assert history[str(target)][0]["new_content"] == "new\n"
    assert projection.summary("turn-1").record() == {
        "schema_version": 1,
        "status": "confirmed",
        "activities": 1,
        "file_operations": 1,
        "artifacts": 0,
    }
    assert artifacts == []


@pytest.mark.asyncio
async def test_abort_discards_staged_code_change(tmp_path: Path) -> None:
    target = tmp_path / "app.py"
    target.write_text("old\n", encoding="utf-8")

    async def sink(_artifact, _path) -> None:
        raise AssertionError("aborted Code turn must not publish an Artifact")

    projection = SurfaceResultProjection(
        "session-1",
        work_mode="code",
        workspace_root=tmp_path,
        cwd=tmp_path,
        outputs_dir=None,
        provider_id="opencode",
        artifact_sink=sink,
    )
    projection.register_turn("turn-1")
    await projection.observe(
        _event(
            ItemLifecycleEvent(
                ItemEventKind.STARTED,
                "tool",
                {"name": "write_file", "arguments": {"path": "app.py"}},
            ),
            sequence=1,
            item_id="item-1",
        )
    )
    target.write_text("late\n", encoding="utf-8")
    await projection.observe(
        _event(
            ItemLifecycleEvent(
                ItemEventKind.COMPLETED,
                "tool",
                {
                    "name": "write_file",
                    "arguments": {"path": "app.py"},
                    "status": "completed",
                },
            ),
            sequence=2,
            item_id="item-1",
        )
    )
    await projection.observe(
        _event(_terminal(TurnEventKind.ABORTED), sequence=3)
    )

    assert not (tmp_path / ".agent_history").exists()
    assert projection.summary("turn-1").status == "discarded"


@pytest.mark.asyncio
async def test_work_output_becomes_existing_artifact_only_after_finish(
    tmp_path: Path,
) -> None:
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    published = []

    async def sink(artifact, path) -> None:
        published.append((artifact, path))

    projection = SurfaceResultProjection(
        "session-1",
        work_mode="work",
        workspace_root=tmp_path,
        cwd=tmp_path,
        outputs_dir=outputs,
        provider_id="codex",
        artifact_sink=sink,
    )
    projection.register_turn("turn-1")
    report = outputs / "report.md"
    report.write_text("# Result\n", encoding="utf-8")
    await projection.observe(
        _event(
            ItemLifecycleEvent(
                ItemEventKind.COMPLETED,
                "tool",
                {"name": "write_file", "arguments": {"path": str(report)}},
            ),
            sequence=1,
            item_id="item-1",
        )
    )
    assert published == []

    await projection.observe(
        _event(_terminal(TurnEventKind.FINISHED), sequence=2)
    )

    assert len(published) == 1
    artifact, path = published[0]
    assert path == report
    assert artifact.parts[0].url == "outputs/report.md"
    assert artifact.metadata["producer"] == "surface_projection"
    assert projection.summary("turn-1").artifacts == 1


@pytest.mark.asyncio
async def test_external_projection_enriches_existing_tool_and_terminal_payloads(
    tmp_path: Path,
) -> None:
    projection = ExternalEventProjection(
        "session-1",
        workspace_root=tmp_path,
        work_mode="code",
        cwd=tmp_path,
        provider_id="codex",
    )
    projection.register_turn(
        "turn-1",
        request_id="request-1",
        channel_id="web",
        mode="agent.code.normal",
    )
    await projection.observe(
        _event(
            ItemLifecycleEvent(
                ItemEventKind.STARTED,
                "tool",
                {"name": "shell", "arguments": {"command": "pytest -q"}},
            ),
            sequence=1,
            item_id="item-1",
        )
    )
    call = projection.owned_payload(
        ProjectedOutput(
            "turn-1",
            chunk=OutputSchema(
                type="tool_call",
                index=0,
                payload={
                    "name": "shell",
                    "arguments": '{"command":"pytest -q"}',
                    "tool_call_id": "item-1",
                },
            ),
        )
    )
    assert call["tool_call"]["surface_projection"]["kind"] == "test"

    await projection.observe(
        _event(
            ItemLifecycleEvent(
                ItemEventKind.COMPLETED,
                "tool",
                {
                    "name": "shell",
                    "arguments": {"command": "pytest -q"},
                    "status": "completed",
                },
            ),
            sequence=2,
            item_id="item-1",
        )
    )
    await projection.observe(
        _event(_terminal(TurnEventKind.FINISHED), sequence=3)
    )
    result = projection.owned_payload(
        ProjectedOutput(
            "turn-1",
            chunk=OutputSchema(
                type="tool_result",
                index=1,
                payload={
                    "tool_name": "shell",
                    "result": "1 passed",
                    "tool_call_id": "item-1",
                },
            ),
        )
    )
    terminal = projection.owned_payload(
        ProjectedOutput("turn-1", terminal=TurnEventKind.FINISHED)
    )

    assert result["surface_projection"]["kind"] == "test"
    assert result["success"] is True
    assert terminal["surface_projection"]["status"] == "confirmed"


@pytest.mark.asyncio
async def test_unconfirmed_surface_persistence_cannot_emit_success_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenswarm.runtime.harness import surface_projection as module

    async def rejected_history_io(_fn, *_args, **_kwargs):
        raise OSError("disk unavailable")

    monkeypatch.setattr(module, "run_history_io", rejected_history_io)
    target = tmp_path / "app.py"
    target.write_text("old\n", encoding="utf-8")
    projection = ExternalEventProjection(
        "session-1",
        workspace_root=tmp_path,
        work_mode="code",
        cwd=tmp_path,
        provider_id="opencode",
    )
    projection.register_turn(
        "turn-1",
        request_id="request-1",
        channel_id="web",
        mode="agent.code.normal",
    )
    await projection.observe(
        _event(
            ItemLifecycleEvent(
                ItemEventKind.STARTED,
                "tool",
                {"name": "write_file", "arguments": {"path": "app.py"}},
            ),
            sequence=1,
            item_id="item-1",
        )
    )
    target.write_text("new\n", encoding="utf-8")
    await projection.observe(
        _event(
            ItemLifecycleEvent(
                ItemEventKind.COMPLETED,
                "tool",
                {
                    "name": "write_file",
                    "arguments": {"path": "app.py"},
                    "status": "completed",
                },
            ),
            sequence=2,
            item_id="item-1",
        )
    )
    await projection.observe(
        _event(_terminal(TurnEventKind.FINISHED), sequence=3)
    )

    terminal = projection.owned_payload(
        ProjectedOutput("turn-1", terminal=TurnEventKind.FINISHED)
    )

    assert terminal["event_type"] == "chat.error"
    assert terminal["code"] == "SURFACE_PERSISTENCE_UNCONFIRMED"
    assert terminal["provider_terminal_status"] == "completed"
    assert terminal["surface_projection"]["status"] == "unconfirmed"


@pytest.mark.asyncio
async def test_surface_activity_budget_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenswarm.runtime.harness import surface_projection as module

    monkeypatch.setattr(module, "_MAX_ACTIVITIES_PER_TURN", 1)

    async def sink(_artifact, _path) -> None:
        raise AssertionError("Code activity must not publish an Artifact")

    projection = SurfaceResultProjection(
        "session-1",
        work_mode="code",
        workspace_root=tmp_path,
        cwd=tmp_path,
        outputs_dir=None,
        provider_id="codex",
        artifact_sink=sink,
    )
    projection.register_turn("turn-1")
    for sequence, item_id in ((1, "item-1"), (2, "item-2")):
        await projection.observe(
            _event(
                ItemLifecycleEvent(
                    ItemEventKind.STARTED,
                    "tool",
                    {"name": "shell", "arguments": {"command": "pwd"}},
                ),
                sequence=sequence,
                item_id=item_id,
            )
        )
    await projection.observe(
        _event(_terminal(TurnEventKind.FINISHED), sequence=3)
    )

    summary = projection.summary("turn-1")
    assert summary.status == "unconfirmed"
    assert summary.activities == 1
    assert summary.errors == ("surface_projection_budget_exhausted",)
