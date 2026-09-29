# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Product interaction and audit contract for External Browser actions."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from openjiuwen.harness.tools.browser_move.playwright_runtime import (
    BrowserBackend,
    BrowserExecutionIdentity,
    BrowserInstanceIdentity,
    BrowserProfileIdentity,
    BrowserTaskIdentity,
)
from openjiuwen.harness_protocol import ToolInvocation

from jiuwenswarm.common.runtime_workspace import RuntimeWorkspacePaths
from jiuwenswarm.runtime.harness.external_browser_admission import (
    ExternalBrowserAdmission,
    browser_runtime_enabled,
)


def _paths(tmp_path: Path) -> RuntimeWorkspacePaths:
    root = (tmp_path / "workspace").resolve()
    root.mkdir()
    return RuntimeWorkspacePaths(root, root, root, root)


def _identity(paths: RuntimeWorkspacePaths) -> BrowserExecutionIdentity:
    workspace = str(paths.runtime_workspace_root)
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
        workspace=workspace,
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


def _admission(
    paths: RuntimeWorkspacePaths,
    published: list[tuple[dict, str]],
    *,
    timeout_s: float = 1.0,
) -> ExternalBrowserAdmission:
    async def publish(payload: dict, delivery_id: str) -> None:
        published.append((payload, delivery_id))

    return ExternalBrowserAdmission(
        parent_subject_id="alice",
        parent_session_id="parent-1",
        runtime_paths=paths,
        publish=publish,
        timeout_s=timeout_s,
    )


def _audit_records(paths: RuntimeWorkspacePaths) -> list[dict]:
    path = (
        paths.runtime_workspace_root
        / ".jiuwenswarm/browser-audit/task-1/permission_audit/auto_permission.jsonl"
    )
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


@pytest.mark.asyncio
@pytest.mark.parametrize("recreate", [True, False])
async def test_old_answer_cannot_authorize_a_new_attempt(tmp_path, monkeypatch, recreate):
    from jiuwenswarm.runtime.harness import external_browser_admission as module

    monkeypatch.setattr(module, "_MAX_COMPLETED", 2)
    paths = _paths(tmp_path)
    published = []
    admission = _admission(paths, published)
    identity = _identity(paths)
    call = ToolInvocation("reused-call", "browser_click", {"ref": "e1"})
    first = asyncio.create_task(admission(identity, call))
    while not published:
        await asyncio.sleep(0)
    old_id = published[-1][0]["request_id"]
    await admission.answer({"request_id": old_id, "answers": [{"selected_options": ["reject"]}]})
    assert await first is False
    if recreate:
        await admission.close()
        admission = _admission(paths, published)
    else:
        for index in range(2):
            await admission(identity, ToolInvocation(f"observe-{index}", "browser_snapshot", {}))
    published.clear()
    second = asyncio.create_task(admission(identity, call))
    try:
        while not published:
            await asyncio.sleep(0)
        new_id = published[-1][0]["request_id"]
        assert new_id != old_id
        assert not await admission.answer({"request_id": old_id, "answers": [{"selected_options": ["allow_once"]}]})
        assert not second.done()
        assert await admission.answer({"request_id": new_id, "answers": [{"selected_options": ["allow_once"]}]})
        assert await second is True
        assert admission.decision_id_for(identity, call) == new_id
    finally:
        await admission.close()
        await second


@pytest.mark.asyncio
@pytest.mark.parametrize("storage_fails", [False, True])
async def test_browser_marker_precedes_publication_and_survives_answer(tmp_path, storage_fails):
    paths = _paths(tmp_path)
    events = []

    class Recovery:
        async def mark_pending_browser_task(self, task_id):
            events.append(("mark", task_id))
            if storage_fails:
                raise OSError("unavailable")

        async def clear_pending_browser_task(self, task_id):
            events.append(("clear", task_id))

    async def publish(payload, _delivery_id):
        events.append(("publish", payload["request_id"]))

    admission = ExternalBrowserAdmission(
        parent_subject_id="alice", parent_session_id="parent-1",
        runtime_paths=paths, publish=publish, recovery=Recovery(),
    )
    identity = _identity(paths)
    pending = asyncio.create_task(admission(identity, ToolInvocation("call", "browser_click", {})))
    for _ in range(100):
        if pending.done() or len(events) >= 2:
            break
        await asyncio.sleep(0)
    if storage_fails:
        assert await pending is False
        assert events == [("mark", "task-1")]
    else:
        assert events[0] == ("mark", "task-1")
        assert events[1][0] == "publish"
        await admission.answer({"request_id": events[1][1], "answers": [{"selected_options": ["allow_once"]}]})
        assert await pending is True
    await admission.close()
    assert not any(event[0] == "clear" for event in events)
    await admission.release_task(identity)
    assert events[-1] == ("clear", "task-1")


@pytest.mark.asyncio
async def test_observation_is_allowed_without_prompt_and_persisted_without_args(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    published: list[tuple[dict, str]] = []
    admission = _admission(paths, published)
    invocation = ToolInvocation(
        "call-observe",
        "browser_snapshot",
        {"secret": "cookie=do-not-persist"},
    )

    assert await admission(_identity(paths), invocation) is True
    assert published == []
    records = _audit_records(paths)
    assert len(records) == 1
    assert records[0]["authorization_outcome"] == "allow"
    assert records[0]["browser_task_id"] == "task-1"
    assert records[0]["browser_request_id"] == "browser-request-1"
    assert records[0]["tool_call_id"] == "call-observe"
    assert "cookie=do-not-persist" not in json.dumps(records)


@pytest.mark.asyncio
async def test_hover_requires_approval_because_page_handlers_can_mutate(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    published: list[tuple[dict, str]] = []
    admission = _admission(paths, published)
    invocation = ToolInvocation("call-hover", "browser_hover", {"ref": "e1"})

    pending = asyncio.create_task(admission(_identity(paths), invocation))
    for _ in range(20):
        if published:
            break
        await asyncio.sleep(0)

    assert pending.done() is False
    assert len(published) == 1
    request_id = published[0][0]["request_id"]
    assert await admission.answer(
        {
            "request_id": request_id,
            "answers": [{"selected_options": ["reject"]}],
        }
    )
    assert await pending is False


@pytest.mark.asyncio
async def test_sensitive_action_waits_for_exact_allow_once_answer(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    published: list[tuple[dict, str]] = []
    admission = _admission(paths, published)
    invocation = ToolInvocation(
        "call-upload",
        "browser_file_upload",
        {"paths": ["/private/upload.txt"]},
    )

    pending = asyncio.create_task(admission(_identity(paths), invocation))
    for _ in range(20):
        if published:
            break
        await asyncio.sleep(0)
    assert pending.done() is False
    payload, delivery_id = published[0]
    decision_id = payload["request_id"]
    assert payload["source"] == "browser_permission"
    assert payload["questions"][0]["tool_payload"] == "[REDACTED]"
    assert "/private/upload.txt" not in json.dumps(payload)
    assert decision_id in delivery_id

    assert await admission.answer(
        {
            "request_id": decision_id,
            "answers": [{"selected_options": ["allow_once"]}],
        }
    ) is True
    assert await pending is True
    assert await admission.answer(
        {
            "request_id": decision_id,
            "answers": [{"selected_options": ["allow_once"]}],
        }
    ) is False
    records = _audit_records(paths)
    assert [record["record_kind"] for record in records] == [
        "browser_permission_pending",
        "browser_permission_terminal",
    ]
    assert records[-1]["authorization_outcome"] == "allow_once"
    assert records[-1]["grant_id"] == decision_id
    assert await admission(_identity(paths), invocation) is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "arguments", "sensitive_value"),
    [
        (
            "browser_cookie_set",
            {"name": "session", "value": "storage-secret"},
            "storage-secret",
        ),
        (
            "browser_network_state_set",
            {"offline": True, "headers": {"authorization": "network-secret"}},
            "network-secret",
        ),
        (
            "browser_run_code_unsafe",
            {"code": "return 'code-secret'"},
            "code-secret",
        ),
        (
            "browser_file_upload",
            {"paths": ["/private/upload-secret.txt"]},
            "upload-secret.txt",
        ),
        (
            "browser_click",
            {"element": "Sign in", "target": "submit-secret"},
            "submit-secret",
        ),
    ],
)
async def test_sensitive_capability_groups_require_redacted_manual_admission(
    tmp_path: Path,
    tool_name: str,
    arguments: dict,
    sensitive_value: str,
) -> None:
    paths = _paths(tmp_path)
    published: list[tuple[dict, str]] = []
    admission = _admission(paths, published)
    invocation = ToolInvocation("call-sensitive", tool_name, arguments)

    pending = asyncio.create_task(admission(_identity(paths), invocation))
    for _ in range(20):
        if published:
            break
        await asyncio.sleep(0)
    assert pending.done() is False
    payload = published[0][0]
    assert payload["questions"][0]["tool_name"] == tool_name
    assert payload["questions"][0]["tool_payload"] == "[REDACTED]"
    assert sensitive_value not in json.dumps(payload)
    assert await admission.answer(
        {
            "request_id": payload["request_id"],
            "answers": [{"selected_options": ["reject"]}],
        }
    )
    assert await pending is False
    assert sensitive_value not in json.dumps(_audit_records(paths))


@pytest.mark.asyncio
async def test_concurrent_answers_settle_exactly_once(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    published: list[tuple[dict, str]] = []
    admission = _admission(paths, published)
    pending = asyncio.create_task(
        admission(
            _identity(paths),
            ToolInvocation("call-click", "browser_click", {"ref": "e1"}),
        )
    )
    for _ in range(20):
        if published:
            break
        await asyncio.sleep(0)
    decision_id = published[0][0]["request_id"]
    params = {
        "request_id": decision_id,
        "answers": [{"selected_options": ["allow_once"]}],
    }

    results = await asyncio.gather(admission.answer(params), admission.answer(params))

    assert sorted(results) == [False, True]
    assert await pending is True


@pytest.mark.asyncio
async def test_timeout_and_late_answer_fail_closed(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    published: list[tuple[dict, str]] = []
    admission = _admission(paths, published, timeout_s=0.01)
    invocation = ToolInvocation("call-click", "browser_click", {"ref": "e1"})

    assert await admission(_identity(paths), invocation) is False
    decision_id = published[0][0]["request_id"]
    assert await admission.answer(
        {
            "request_id": decision_id,
            "answers": [{"selected_options": ["allow_once"]}],
        }
    ) is False
    assert _audit_records(paths)[-1]["authorization_outcome"] == "timeout"


@pytest.mark.asyncio
async def test_close_denies_pending_action(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    published: list[tuple[dict, str]] = []
    admission = _admission(paths, published)
    pending = asyncio.create_task(
        admission(
            _identity(paths),
            ToolInvocation("call-nav", "browser_navigate", {"url": "https://example.test"}),
        )
    )
    for _ in range(20):
        if published:
            break
        await asyncio.sleep(0)

    await admission.close()

    assert await pending is False
    assert _audit_records(paths)[-1]["authorization_outcome"] == "cancelled"


@pytest.mark.asyncio
async def test_cancelled_invocation_releases_pending_decision(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    published: list[tuple[dict, str]] = []
    admission = _admission(paths, published)
    pending = asyncio.create_task(
        admission(
            _identity(paths),
            ToolInvocation("call-fill", "browser_fill_form", {"fields": []}),
        )
    )
    for _ in range(20):
        if published:
            break
        await asyncio.sleep(0)
    decision_id = published[0][0]["request_id"]

    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending

    assert await admission.answer(
        {
            "request_id": decision_id,
            "answers": [{"selected_options": ["allow_once"]}],
        }
    ) is False
    assert _audit_records(paths)[-1]["authorization_outcome"] == "cancelled"


@pytest.mark.asyncio
async def test_identity_scope_mismatch_is_rejected_before_publish(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    published: list[tuple[dict, str]] = []
    admission = ExternalBrowserAdmission(
        parent_subject_id="mallory",
        parent_session_id="parent-1",
        runtime_paths=paths,
        publish=lambda _payload, _delivery_id: asyncio.sleep(0),
    )

    with pytest.raises(ValueError, match="owner_subject_id"):
        await admission(
            _identity(paths),
            ToolInvocation("call-1", "browser_click", {"ref": "e1"}),
        )
    assert published == []


def test_external_browser_runtime_uses_existing_opt_in_switch() -> None:
    assert browser_runtime_enabled({}) is False
    assert browser_runtime_enabled({"PLAYWRIGHT_RUNTIME_MCP_ENABLED": "yes"}) is True
    assert browser_runtime_enabled({"BROWSER_RUNTIME_MCP_ENABLED": "ON"}) is True
    assert browser_runtime_enabled({"PLAYWRIGHT_RUNTIME_MCP_ENABLED": "0"}) is False
