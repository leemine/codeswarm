# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Browser output discovery and existing Artifact projection bridge."""

from __future__ import annotations

import asyncio
from functools import partial
from pathlib import Path

import pytest

from openjiuwen.harness.tools.browser_move.playwright_runtime import (
    BrowserBackend,
    BrowserExecutionFileRoots,
    BrowserExecutionIdentity,
    BrowserInstanceIdentity,
    BrowserProfileIdentity,
    BrowserTaskIdentity,
    project_browser_output_artifact,
)
from openjiuwen.harness_protocol import (
    ToolExecutionResult,
    ToolInvocation,
)

from jiuwenswarm.runtime.harness.external_browser_artifacts import (
    ExternalBrowserArtifactGateway,
)


def _identity(workspace: Path) -> BrowserExecutionIdentity:
    profile = BrowserProfileIdentity(
        profile_id="profile-1",
        owner_subject_id="alice",
        backend=BrowserBackend.MANAGED,
        policy_revision="test-v1",
    )
    instance = BrowserInstanceIdentity(
        instance_id="instance-1",
        profile_id=profile.profile_id,
        profile_generation=profile.generation,
        parent_session_id="parent-1",
        subagent_id="parent-1_sub_browser_1",
        workspace=str(workspace),
    )
    task = BrowserTaskIdentity(
        task_id="task-1",
        instance_id=instance.instance_id,
        instance_generation=instance.generation,
        child_turn_id="child-turn-1",
        request_id="browser-request-1",
        capability_fingerprint="sha256:" + "a" * 64,
    )
    return BrowserExecutionIdentity(profile=profile, instance=instance, task=task)


class _Gateway:
    def __init__(
        self,
        identity: BrowserExecutionIdentity,
        invoke,
    ) -> None:
        self.execution_identity = identity
        self.closed = False
        self._invoke = invoke

    async def definitions(self):
        return ()

    async def invoke(self, invocation):
        return await self._invoke(invocation)

    async def close(self) -> None:
        self.closed = True


def _wrapper(tmp_path: Path, invoke, sink):
    workspace = (tmp_path / "workspace").resolve()
    outputs = workspace / "outputs" / "browser" / "task-1"
    download_state = workspace / ".internal" / "download-state" / "task-1"
    outputs.mkdir(parents=True)
    identity = _identity(workspace)
    roots = BrowserExecutionFileRoots(
        workspace=str(workspace),
        uploads_root=str(workspace / "uploads"),
        outputs_root=str(outputs),
        audit_root=str(workspace / "audit"),
    )
    projector = partial(
        project_browser_output_artifact,
        execution_identity=identity,
        file_roots=roots,
    )
    return (
        ExternalBrowserArtifactGateway(
            _Gateway(identity, invoke),
            workspace_root=workspace,
            uploads_root=Path(roots.uploads_root),
            outputs_root=outputs,
            download_state_root=download_state,
            project_artifact=projector,
            page_state=lambda: {"url": "https://example.test/private?token=secret"},
            decision_id_for=lambda _identity, _invocation: "decision-1",
            sink=sink,
        ),
        outputs,
    )


@pytest.mark.asyncio
async def test_projects_only_changed_controlled_outputs_and_sanitizes_source(
    tmp_path: Path,
) -> None:
    delivered = []

    async def sink(artifact, path) -> None:
        delivered.append((artifact, path))

    async def invoke(_invocation):
        internal.write_text("- snapshot", encoding="utf-8")
        generated.write_bytes(b"pdf")
        return ToolExecutionResult(content={"ok": True})

    wrapper, outputs = _wrapper(tmp_path, invoke, sink)
    preexisting = outputs / "preexisting.txt"
    preexisting.write_text("old", encoding="utf-8")
    generated = outputs / "artifacts" / "report.pdf"
    generated.parent.mkdir()
    internal = outputs / "page-2026-09-29T00-00-00-000Z.yml"

    result = await wrapper.invoke(
        ToolInvocation("call-pdf", "browser_pdf_save", {})
    )

    assert result.is_error is False
    assert len(delivered) == 1
    artifact, path = delivered[0]
    assert path == generated
    assert artifact.metadata["kind"] == "pdf"
    assert artifact.metadata["permission_decision_id"] == "decision-1"
    assert artifact.metadata["source_url"] == "https://example.test/private"
    assert artifact.metadata["workspace_relative_path"].endswith(
        "/artifacts/report.pdf"
    )
    assert preexisting not in [item[1] for item in delivered]
    assert internal not in [item[1] for item in delivered]
    assert result.content["ok"] is True
    receipt = result.content["browser_artifacts"][0]
    assert receipt["artifact_id"] == artifact.artifactId
    assert receipt["delivery_status"] == "delivered"
    assert receipt["workspace_relative_path"].endswith("/artifacts/report.pdf")
    assert str(tmp_path) not in str(receipt)


@pytest.mark.asyncio
async def test_ignores_in_progress_download_until_renamed(tmp_path: Path) -> None:
    delivered = []
    phase = 0

    async def sink(artifact, path) -> None:
        delivered.append((artifact, path))

    async def invoke(_invocation):
        nonlocal phase
        phase += 1
        if phase == 1:
            temporary.write_bytes(b"incomplete")
        elif phase == 2:
            temporary.write_bytes(b"still growing")
            completed.write_bytes(b"finished")
        else:
            temporary.rename(renamed)
        return ToolExecutionResult(content={"ok": True})

    wrapper, outputs = _wrapper(tmp_path, invoke, sink)
    temporary = outputs / "background.crdownload"
    completed = outputs / "result.txt"
    renamed = outputs / "background.txt"
    for call_id, expected in (("snapshot", []), ("click", [completed]),
                              ("rename", [completed, renamed])):
        result = await wrapper.invoke(ToolInvocation(call_id, "browser_snapshot", {}))
        assert result.is_error is False
        assert [path for _, path in delivered] == expected


@pytest.mark.asyncio
async def test_completed_guid_filename_remains_a_download(tmp_path: Path) -> None:
    delivered = []

    async def sink(artifact, path) -> None:
        delivered.append((artifact, path))

    async def invoke(_invocation):
        generated.write_bytes(b"download")
        return ToolExecutionResult(content={"ok": True})

    wrapper, outputs = _wrapper(tmp_path, invoke, sink)
    generated = outputs / "2ed12580-13bd-4757-bb09-4e6f3e1efb81"
    wrapper._download_state_root.mkdir(parents=True)
    (wrapper._download_state_root / "transfer.completed").write_text(generated.name)

    result = await wrapper.invoke(ToolInvocation("call-1", "browser_click", {}))

    assert result.is_error is False
    assert len(delivered) == 1
    assert delivered[0][0].metadata["kind"] == "download"


@pytest.mark.asyncio
async def test_rejects_next_call_while_a_download_is_pending(tmp_path: Path) -> None:
    calls = []

    async def sink(_artifact, _path) -> None:
        return None

    async def invoke(invocation):
        calls.append(invocation)
        return ToolExecutionResult(content={"ok": True})

    wrapper, _outputs = _wrapper(tmp_path, invoke, sink)
    state = wrapper._download_state_root
    state.mkdir(parents=True)
    (state / "download-1.pending").write_text("", encoding="utf-8")

    result = await wrapper.invoke(ToolInvocation("call-1", "browser_click", {}))

    assert result.is_error is True
    assert result.content == "Browser download is still pending"
    assert calls == []


@pytest.mark.asyncio
async def test_waits_for_download_completion_before_projecting(tmp_path: Path) -> None:
    delivered = []
    completion = None

    async def sink(artifact, path) -> None:
        delivered.append((artifact, path))

    async def invoke(_invocation):
        nonlocal completion
        state.mkdir(parents=True)
        pending.write_text("", encoding="utf-8")

        async def finish() -> None:
            await asyncio.sleep(0.05)
            generated.write_bytes(b"complete download")
            pending.unlink()

        completion = asyncio.create_task(finish())
        return ToolExecutionResult(content={"ok": True})

    wrapper, outputs = _wrapper(tmp_path, invoke, sink)
    state = wrapper._download_state_root
    pending = state / "download-1.pending"
    generated = outputs / "download.txt"

    result = await wrapper.invoke(ToolInvocation("call-1", "browser_click", {}))
    assert completion is not None
    await completion

    assert result.is_error is False
    assert len(delivered) == 1
    assert delivered[0][1] == generated
    assert delivered[0][0].metadata["kind"] == "download"


@pytest.mark.asyncio
async def test_download_failure_is_a_stable_tool_error(tmp_path: Path) -> None:
    delivered = []
    failure = None

    async def sink(artifact, path) -> None:
        delivered.append((artifact, path))

    async def invoke(_invocation):
        nonlocal failure
        state.mkdir(parents=True)
        pending.write_text("", encoding="utf-8")

        async def fail() -> None:
            await asyncio.sleep(0.05)
            pending.rename(failed)

        failure = asyncio.create_task(fail())
        return ToolExecutionResult(content={"ok": True})

    wrapper, _outputs = _wrapper(tmp_path, invoke, sink)
    state = wrapper._download_state_root
    pending = state / "download-1.pending"
    failed = state / "download-1.failed"

    result = await wrapper.invoke(ToolInvocation("call-1", "browser_click", {}))
    assert failure is not None
    await failure

    assert result.is_error is True
    assert result.content == "Browser download failed"
    assert delivered == []


@pytest.mark.asyncio
async def test_failed_browser_call_does_not_publish_created_file(tmp_path: Path) -> None:
    delivered = []

    async def sink(artifact, path) -> None:
        delivered.append((artifact, path))

    async def invoke(_invocation):
        generated.write_text("partial", encoding="utf-8")
        return ToolExecutionResult(content="failed", is_error=True)

    wrapper, outputs = _wrapper(tmp_path, invoke, sink)
    generated = outputs / "partial.txt"

    result = await wrapper.invoke(ToolInvocation("call-1", "browser_click", {}))

    assert result.is_error is True
    assert delivered == []


@pytest.mark.asyncio
async def test_artifact_delivery_failure_is_a_stable_tool_error(tmp_path: Path) -> None:
    async def sink(_artifact, _path) -> None:
        raise OSError("sensitive delivery detail")

    async def invoke(_invocation):
        generated.write_text("result", encoding="utf-8")
        return ToolExecutionResult(content={"ok": True})

    wrapper, outputs = _wrapper(tmp_path, invoke, sink)
    generated = outputs / "export.txt"

    result = await wrapper.invoke(ToolInvocation("call-1", "browser_run_code", {}))

    assert result.is_error is True
    assert result.content == "Browser Artifact projection failed: OSError"
    assert "sensitive delivery detail" not in str(result.content)


@pytest.mark.asyncio
async def test_rejects_output_filename_outside_task_root_before_browser_call(
    tmp_path: Path,
) -> None:
    calls = []

    async def sink(_artifact, _path) -> None:
        return None

    async def invoke(invocation):
        calls.append(invocation)
        return ToolExecutionResult(content={"ok": True})

    wrapper, _outputs = _wrapper(tmp_path, invoke, sink)

    result = await wrapper.invoke(
        ToolInvocation(
            "call-1",
            "browser_take_screenshot",
            {"filename": "outside.png"},
        )
    )

    assert result.is_error is True
    assert result.content == "Browser file scope validation failed"
    assert calls == []


@pytest.mark.asyncio
async def test_upload_accepts_only_regular_files_in_controlled_input_root(
    tmp_path: Path,
) -> None:
    calls = []

    async def sink(_artifact, _path) -> None:
        return None

    async def invoke(invocation):
        calls.append(invocation)
        return ToolExecutionResult(content={"ok": True})

    wrapper, outputs = _wrapper(tmp_path, invoke, sink)
    workspace = outputs.parents[2]
    upload = workspace / "uploads" / "allowed.txt"
    upload.parent.mkdir()
    upload.write_text("allowed", encoding="utf-8")
    outside = workspace / "outside.txt"
    outside.write_text("denied", encoding="utf-8")

    denied = await wrapper.invoke(
        ToolInvocation(
            "call-denied",
            "browser_file_upload",
            {"paths": [str(outside)]},
        )
    )
    allowed = await wrapper.invoke(
        ToolInvocation(
            "call-allowed",
            "browser_file_upload",
            {"paths": [str(upload)]},
        )
    )

    assert denied.is_error is True
    assert denied.content == "Browser file scope validation failed"
    assert allowed.is_error is False
    assert [call.call_id for call in calls] == ["call-allowed"]


@pytest.mark.asyncio
async def test_rejects_symlinks_for_browser_input_and_output_paths(
    tmp_path: Path,
) -> None:
    calls = []

    async def sink(_artifact, _path) -> None:
        return None

    async def invoke(invocation):
        calls.append(invocation)
        return ToolExecutionResult(content={"ok": True})

    wrapper, outputs = _wrapper(tmp_path, invoke, sink)
    workspace = outputs.parents[2]
    upload = workspace / "uploads" / "linked.txt"
    upload.parent.mkdir()
    outside = workspace / "outside.txt"
    outside.write_text("denied", encoding="utf-8")
    upload.symlink_to(outside)
    output_link = outputs / "linked"
    output_link.symlink_to(workspace, target_is_directory=True)

    denied_input = await wrapper.invoke(
        ToolInvocation(
            "call-input",
            "browser_file_upload",
            {"paths": [str(upload)]},
        )
    )
    denied_output = await wrapper.invoke(
        ToolInvocation(
            "call-output",
            "browser_take_screenshot",
            {"filename": str(output_link / "escape.png")},
        )
    )

    assert denied_input.is_error is True
    assert denied_output.is_error is True
    assert calls == []


@pytest.mark.asyncio
async def test_close_delegates_after_artifact_invocations(tmp_path: Path) -> None:
    async def sink(_artifact, _path) -> None:
        return None

    async def invoke(_invocation):
        return ToolExecutionResult(content={"ok": True})

    wrapper, _outputs = _wrapper(tmp_path, invoke, sink)

    await wrapper.close()

    assert wrapper.closed is True


@pytest.mark.asyncio
async def test_failed_delivery_retries_before_another_browser_action(tmp_path: Path) -> None:
    calls, delivered = [], []
    fail = True

    async def sink(artifact, _path):
        if fail:
            raise RuntimeError("offline")
        delivered.append(artifact.artifactId)

    async def invoke(invocation):
        calls.append(invocation.call_id)
        (outputs / "report.pdf").write_bytes(b"pdf")
        return ToolExecutionResult(content={"ok": True})

    wrapper, outputs = _wrapper(tmp_path, invoke, sink)
    assert (await wrapper.invoke(ToolInvocation("first", "browser_pdf_save", {}))).is_error
    assert (await wrapper.invoke(ToolInvocation("blocked", "browser_snapshot", {}))).is_error
    assert calls == ["first"]
    with pytest.raises(RuntimeError, match="offline"):
        await wrapper.close()
    assert not wrapper.closed, "pending Artifact delivery must retain cleanup ownership"
    fail = False
    await wrapper.close()
    await wrapper.close()
    assert len(delivered) == 1
    assert calls == ["first"]


@pytest.mark.asyncio
async def test_close_keeps_pending_download_until_exit_confirmed(tmp_path: Path) -> None:
    async def unused(*_args):
        return ToolExecutionResult(content={"ok": True})

    wrapper, _outputs = _wrapper(tmp_path, unused, unused)
    state = wrapper._download_state_root
    state.mkdir(parents=True)
    marker = state / "guid.pending"
    marker.touch()
    original_close = wrapper._gateway.close

    async def fail_close():
        raise RuntimeError("exit unconfirmed")

    wrapper._gateway.close = fail_close
    with pytest.raises(RuntimeError, match="exit unconfirmed"):
        await wrapper.close()
    assert marker.exists()
    assert not wrapper.closed
    wrapper._gateway.close = original_close
    await wrapper.close()
    assert not marker.exists()
    assert (state / "guid.canceled").exists()


@pytest.mark.asyncio
async def test_raw_chrome_guid_is_not_an_artifact_before_final_name(tmp_path: Path) -> None:
    delivered = []

    async def sink(_artifact, path):
        delivered.append(path.name)

    async def invoke(invocation):
        if invocation.call_id == "snapshot":
            raw.write_bytes(b"internal Chrome transfer")
        else:
            raw.rename(outputs / "result.txt")
        return ToolExecutionResult(content={"ok": True})

    wrapper, outputs = _wrapper(tmp_path, invoke, sink)
    raw = outputs / "435642be-256e-4a30-9020-b27e942b9cf9"
    result = await wrapper.invoke(ToolInvocation("snapshot", "browser_snapshot", {}))
    assert not result.is_error
    assert delivered == [], "a raw GUID without a finalized filename is not a user Artifact"
    result = await wrapper.invoke(ToolInvocation("finalized", "browser_snapshot", {}))
    assert not result.is_error
    assert delivered == ["result.txt"]


@pytest.mark.asyncio
async def test_completion_marker_promotes_unchanged_guid_but_symlink_does_not(tmp_path: Path) -> None:
    delivered = []

    async def sink(_artifact, path):
        delivered.append(path.name)

    async def invoke(invocation):
        if invocation.call_id == "complete":
            marker.unlink()
            marker.write_text(raw.name)
        return ToolExecutionResult(content={"ok": True})

    wrapper, outputs = _wrapper(tmp_path, invoke, sink)
    raw = outputs / "435642be-256e-4a30-9020-b27e942b9cf9"
    raw.write_bytes(b"finished")
    wrapper._download_state_root.mkdir(parents=True)
    outside = tmp_path / "foreign"
    outside.write_text(raw.name)
    marker = wrapper._download_state_root / "transfer.completed"
    marker.symlink_to(outside)
    assert not (await wrapper.invoke(ToolInvocation("snapshot", "browser_snapshot", {}))).is_error
    assert delivered == []
    assert not (await wrapper.invoke(ToolInvocation("complete", "browser_snapshot", {}))).is_error
    assert delivered == [raw.name]
    assert not (await wrapper.invoke(ToolInvocation("again", "browser_snapshot", {}))).is_error
    assert delivered == [raw.name]
