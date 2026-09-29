import asyncio
from concurrent.futures import Future
from unittest.mock import patch

import pytest

from jiuwenswarm.agents.harness.common.tools import send_file_to_user as sfu
from jiuwenswarm.agents.harness.common.tools.web_file_download import (
    build_file_download_info,
    validate_file_download_token,
)


@pytest.fixture(autouse=True)
def _isolate_send_file_state(monkeypatch):
    async def direct_history(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    monkeypatch.setattr(sfu, "run_history_io", direct_history)
    sfu._SENT_FILE_PATHS_BY_SESSION.clear()
    yield
    sfu._SENT_FILE_PATHS_BY_SESSION.clear()


@pytest.fixture
def artifact_history(monkeypatch):
    from jiuwenswarm.server.runtime.session import session_history as history
    rows = []

    def persist(**kwargs):
        receipt = Future()
        fresh = not any(row.get("delivery_id") == kwargs.get("delivery_id") for row in rows)
        if fresh:
            row = {**kwargs, **(kwargs.get("extra") or {})}
            rows.append(row)
        receipt.set_result(fresh)
        return receipt

    monkeypatch.setattr(history, "append_history_record_durable", persist)
    monkeypatch.setattr(history, "load_history_records", lambda _session: list(rows))
    return rows


def test_partition_and_mark_sent_files():
    new_paths, skipped = sfu._partition_sent_files("s1", [r"C:\tmp\a.md", r"C:\tmp\b.md"])
    assert new_paths == [r"C:\tmp\a.md", r"C:\tmp\b.md"]
    assert skipped == []

    sfu._mark_files_sent("s1", [r"C:\tmp\a.md"])
    new_paths, skipped = sfu._partition_sent_files("s1", [r"C:\tmp\a.md", r"C:\tmp\b.md"])
    assert new_paths == [r"C:\tmp\b.md"]
    assert skipped == [r"C:\tmp\a.md"]

    sfu.clear_sent_files_for_session("s1")
    new_paths, skipped = sfu._partition_sent_files("s1", [r"C:\tmp\a.md"])
    assert new_paths == [r"C:\tmp\a.md"]
    assert skipped == []


def test_download_info_includes_agentos_user_id_in_url(tmp_path):
    file_path = tmp_path / "report.txt"
    file_path.write_text("hello", encoding="utf-8")

    info = build_file_download_info(
        str(file_path), "report.txt", "session-1", user_id="user-1"
    )

    assert "user_id=user-1" in info["download_url"]
    payload = validate_file_download_token(info["download_token"])
    assert payload is not None
    assert "exp" not in payload


def test_send_file_skips_duplicate_after_success(tmp_path):
    file_path = tmp_path / "handoff.md"
    file_path.write_text("hello", encoding="utf-8")

    toolkit = sfu.SendFileToolkit(
        request_id="r1",
        session_id="sess-1",
        channel_id="web",
    )
    pushed: list[dict] = []

    async def _push(message: dict) -> bool:
        pushed.append(message)
        return True

    with patch.object(sfu, "send_runtime_push", _push), patch(
        "jiuwenswarm.server.runtime.session.session_history.append_history_record",
    ):
        first = asyncio.run(toolkit.send_file(str(file_path)))
        second = asyncio.run(toolkit.send_file(str(file_path)))

    assert "成功发送" in first
    assert "最终交付文件已位于当前项目目录" not in first
    assert "跳过重复投递" in second
    assert len(pushed) == 1


def test_projected_artifact_reuses_file_push_and_history_with_metadata(tmp_path, artifact_history):
    file_path = tmp_path / "browser-output.pdf"
    file_path.write_bytes(b"pdf")
    toolkit = sfu.SendFileToolkit(
        request_id="browser-request",
        session_id="browser-session",
        channel_id="web",
    )
    artifact = {
        "artifactId": "browser-output-1",
        "name": file_path.name,
        "metadata": {
            "permission_decision_id": "decision-1",
            "workspace_relative_path": "outputs/browser-output.pdf",
        },
    }
    pushed: list[dict] = []

    async def _push(message: dict) -> bool:
        pushed.append(message)
        return True

    with patch.object(sfu, "send_runtime_push", _push):
        result = asyncio.run(
            toolkit.deliver_projected_artifact(file_path, artifact)
        )

    assert "成功发送" in result
    file_payload = pushed[0]["payload"]["files"][0]
    assert file_payload["artifact"] == artifact
    assert file_payload["download_token"]
    assert artifact_history[0]["files"][0] == file_payload
    assert artifact_history[0]["delivery_id"] == (
        "browser-artifact:"
        "9a459d245c808ba7a58c48fb21553f5cb05d14a4e01ad87bccd4eeb9f8dfc5f1"
    )


def test_projected_artifact_metadata_follows_team_materialization(tmp_path, artifact_history):
    team_root = tmp_path / ".agent_teams" / "browser" / "team-workspace"
    project_root = tmp_path / "project"
    file_path = team_root / "outputs" / "browser-output.pdf"
    file_path.parent.mkdir(parents=True)
    file_path.write_bytes(b"pdf")
    toolkit = sfu.SendFileToolkit(
        request_id="browser-request",
        session_id="browser-session",
        channel_id="web",
        project_dir=str(project_root),
        team_workspace_root=str(team_root),
    )
    artifact = {
        "artifactId": "browser-output-1",
        "metadata": {
            "workspace_relative_path": "outputs/browser-output.pdf",
        },
    }
    pushed: list[dict] = []

    async def _push(message: dict) -> bool:
        pushed.append(message)
        return True

    with patch.object(sfu, "send_runtime_push", _push):
        result = asyncio.run(
            toolkit.deliver_projected_artifact(file_path, artifact)
        )

    assert "成功发送" in result
    file_payload = pushed[0]["payload"]["files"][0]
    assert file_payload["path"] == str(project_root / "outputs/browser-output.pdf")
    assert file_payload["artifact"] == artifact


def test_history_failure_after_push_does_not_duplicate_delivery(tmp_path):
    file_path = tmp_path / "delivered.md"
    file_path.write_text("hello", encoding="utf-8")
    toolkit = sfu.SendFileToolkit(
        request_id="r-history",
        session_id="sess-history",
        channel_id="web",
    )
    pushed: list[dict] = []

    async def _push(message: dict) -> bool:
        pushed.append(message)
        return True

    with patch.object(sfu, "send_runtime_push", _push), patch(
        "jiuwenswarm.server.runtime.session.session_history.append_history_record",
        side_effect=OSError("history unavailable"),
    ):
        first = asyncio.run(toolkit.send_file(str(file_path)))
        second = asyncio.run(toolkit.send_file(str(file_path)))

    assert "成功发送" in first
    assert "跳过重复投递" in second
    assert len(pushed) == 1


def test_send_file_materializes_team_workspace_files_in_project(tmp_path):
    team_root = tmp_path / "team-workspace"
    project_root = tmp_path / "project"
    source = team_root / "reports" / "poem-gu.txt"
    source.parent.mkdir(parents=True)
    source.write_text("古诗", encoding="utf-8")

    toolkit = sfu.SendFileToolkit(
        request_id="r2",
        session_id="sess-2",
        channel_id="web",
        project_dir=str(project_root),
        team_workspace_root=str(team_root),
    )
    pushed: list[dict] = []

    async def _push(message: dict) -> bool:
        pushed.append(message)
        return True

    with patch.object(sfu, "send_runtime_push", _push), patch(
        "jiuwenswarm.server.runtime.session.session_history.append_history_record",
    ) as append_history:
        result = asyncio.run(toolkit.send_file(str(source)))

    delivered = project_root / "reports" / "poem-gu.txt"
    assert "成功发送" in result
    assert delivered.read_text(encoding="utf-8") == "古诗"
    payload = pushed[0]["payload"]["files"]
    assert payload[0]["path"] == str(delivered)
    history_extra = append_history.call_args.kwargs["extra"]
    assert history_extra["files"][0]["path"] == str(delivered)


def test_send_file_does_not_move_non_team_files(tmp_path):
    team_root = tmp_path / "team-workspace"
    project_root = tmp_path / "project"
    source = tmp_path / "downloads" / "existing.txt"
    source.parent.mkdir(parents=True)
    source.write_text("existing", encoding="utf-8")

    toolkit = sfu.SendFileToolkit(
        request_id="r3",
        session_id="sess-3",
        channel_id="web",
        project_dir=str(project_root),
        team_workspace_root=str(team_root),
    )

    assert toolkit._materialize_team_deliverable(str(source)) == str(source)
    assert not project_root.exists()


def test_send_file_does_not_overwrite_different_project_file(tmp_path):
    team_root = tmp_path / "team-workspace"
    project_root = tmp_path / "project"
    source = team_root / "result.txt"
    destination = project_root / "result.txt"
    source.parent.mkdir(parents=True)
    destination.parent.mkdir(parents=True)
    source.write_text("new", encoding="utf-8")
    destination.write_text("user-owned", encoding="utf-8")

    toolkit = sfu.SendFileToolkit(
        request_id="r4",
        session_id="sess-4",
        channel_id="web",
        project_dir=str(project_root),
        team_workspace_root=str(team_root),
    )

    with pytest.raises(FileExistsError):
        toolkit._materialize_team_deliverable(str(source))
    assert destination.read_text(encoding="utf-8") == "user-owned"


def test_send_file_resolves_project_from_session_and_infers_team_root(tmp_path):
    team_root = tmp_path / ".agent_teams" / "writers" / "team-workspace"
    project_root = tmp_path / "project"
    source = team_root / "poem.txt"
    source.parent.mkdir(parents=True)
    source.write_text("poem", encoding="utf-8")
    toolkit = sfu.SendFileToolkit(
        request_id="r5",
        session_id="sess-5",
        channel_id="web",
    )

    with patch(
        "jiuwenswarm.server.runtime.session.session_metadata.get_session_metadata",
        return_value={"project_dir": str(project_root)},
    ):
        delivered = toolkit._materialize_team_deliverable(str(source))

    assert delivered == str(project_root / "poem.txt")
    assert (project_root / "poem.txt").read_text(encoding="utf-8") == "poem"


def test_send_file_keeps_worktree_file_in_worktree(tmp_path):
    project_root = tmp_path / "project"
    worktree_root = tmp_path / ".worktrees" / "member-1"
    source = worktree_root / "src" / "feature.py"
    source.parent.mkdir(parents=True)
    source.write_text("feature = True", encoding="utf-8")
    toolkit = sfu.SendFileToolkit(
        request_id="r6",
        session_id="sess-6",
        channel_id="web",
        project_dir=str(project_root),
        team_workspace_root=str(tmp_path / ".agent_teams" / "team" / "team-workspace"),
    )

    assert toolkit._materialize_team_deliverable(str(source)) == str(source)
    assert not project_root.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["history", "push", "ack"])
async def test_artifact_crash_boundaries_replay_without_browser(tmp_path, monkeypatch, artifact_history, failure):
    from jiuwenswarm.server.runtime.session import session_history as history
    path = tmp_path / "artifact.txt"
    path.write_text("artifact")
    toolkit = sfu.SendFileToolkit(request_id="original", session_id="session", channel_id="web")
    artifact = {"artifactId": "stable", "metadata": {}}
    persist = history.append_history_record_durable
    pushes = []
    broken = True

    def faulty_persist(**kwargs):
        if broken and ((failure == "history" and kwargs["event_type"] == "chat.file")
                       or (failure == "ack" and kwargs["event_type"] == "harness.artifact_delivery")):
            receipt = Future()
            receipt.set_exception(OSError("injected persistence failure"))
            return receipt
        return persist(**kwargs)

    async def push(msg):
        assert artifact_history and artifact_history[0]["event_type"] == "chat.file"
        pushes.append(msg)
        if broken and failure == "push":
            raise OSError("uncertain push")
        return True

    monkeypatch.setattr(history, "append_history_record_durable", faulty_persist)
    monkeypatch.setattr(sfu, "send_runtime_push", push)
    with pytest.raises(OSError):
        await toolkit.deliver_projected_artifact(path, artifact)
    if failure == "history":
        assert pushes == [] and artifact_history == []
    broken = False
    replacement = sfu.SendFileToolkit(request_id="new-request", session_id="session", channel_id="web")
    if failure == "history":
        await replacement.deliver_projected_artifact(path, artifact)
    else:
        await replacement.replay_projected_artifacts()
        assert pushes[-1]["request_id"] == "original"
    count = len(pushes)
    await replacement.deliver_projected_artifact(path, artifact)
    await replacement.replay_projected_artifacts()
    assert len(pushes) == count
    assert len([r for r in artifact_history if r["event_type"] == "chat.file"]) == 1
    assert len({msg["payload"]["delivery_id"] for msg in pushes}) == 1


@pytest.mark.asyncio
async def test_artifact_replay_uses_real_durable_history_after_owner_recreation(tmp_path, monkeypatch):
    from jiuwenswarm.server.runtime.session import lifecycle, session_history as history

    sessions = tmp_path / "sessions"
    sessions.mkdir()
    monkeypatch.setattr(history, "get_agent_sessions_dir", lambda: sessions)
    monkeypatch.setattr(lifecycle, "get_agent_sessions_dir", lambda: sessions)
    path = tmp_path / "real-artifact.txt"
    path.write_text("durable artifact")
    artifact = {"artifactId": "real-durable-artifact", "metadata": {}}
    pushes = []

    async def unavailable(msg):
        rows = history.load_history_records("durable-artifact-session")
        assert any(row.get("event_type") == "chat.file" for row in rows)
        return False

    monkeypatch.setattr(sfu, "send_runtime_push", unavailable)
    toolkit = sfu.SendFileToolkit(request_id="original-request", session_id="durable-artifact-session", channel_id="web")
    with pytest.raises(RuntimeError, match="not accepted"):
        await toolkit.deliver_projected_artifact(path, artifact)

    async def accepted(msg):
        from jiuwenswarm.common.e2a.wire_codec import parse_agent_server_wire_chunk
        from jiuwenswarm.gateway.message_handler.message_handler import MessageHandler
        from jiuwenswarm.server.gateway_push.wire import build_server_push_wire

        chunk = parse_agent_server_wire_chunk(build_server_push_wire(msg))
        routed = MessageHandler._chunk_to_message(chunk, msg["session_id"])
        assert routed.payload["delivery_id"] == msg["payload"]["delivery_id"]
        assert routed.payload["files"][0]["artifact"] == artifact
        pushes.append(msg)
        return True

    monkeypatch.setattr(sfu, "send_runtime_push", accepted)
    replacement = sfu.SendFileToolkit(request_id="replacement", session_id="durable-artifact-session", channel_id="tui")
    await replacement.replay_projected_artifacts()
    await replacement.replay_projected_artifacts()
    assert len(pushes) == 1
    assert pushes[0]["request_id"] == "original-request"
    assert pushes[0]["channel_id"] == "web"
    rows = history.load_history_records("durable-artifact-session")
    assert len([r for r in rows if r.get("event_type") == "chat.file"]) == 1
    assert len([r for r in rows if r.get("event_type") == "harness.artifact_delivery"]) == 1
    assert history.flush_pending_writes()


@pytest.mark.asyncio
async def test_legacy_transport_acceptance_cannot_suppress_durable_gateway_replay(artifact_history, monkeypatch):
    artifact_history.extend([
        {'event_type': 'chat.file', 'delivery_id': 'browser-artifact:legacy',
         'request_id': 'original', 'channel_id': 'web', 'files': [{'name': 'legacy.txt'}],
         'artifact_route_metadata': {'app_id': 'original-app'}},
        {'event_type': 'harness.artifact_delivery', 'artifact_delivery_id': 'browser-artifact:legacy',
         'delivery_id': 'browser-artifact:legacy:accepted'},
    ])
    pushes = []
    async def push(msg):
        pushes.append(msg)
        return True
    monkeypatch.setattr(sfu, 'send_runtime_push', push)
    toolkit = sfu.SendFileToolkit(request_id='replacement', session_id='session', channel_id='tui')
    await toolkit.replay_projected_artifacts()
    await toolkit.replay_projected_artifacts()
    assert len(pushes) == 1
    assert pushes[0]['metadata'] == {'app_id': 'original-app'}
    assert any(row.get('acceptance') == 'gateway_durable_v1' for row in artifact_history)


@pytest.mark.asyncio
async def test_artifact_durable_route_omits_unrelated_request_secrets(tmp_path, artifact_history, monkeypatch):
    async def push(msg):
        assert msg['metadata'] == {'app_id': 'app'}
        return True
    monkeypatch.setattr(sfu, 'send_runtime_push', push)
    path = tmp_path / 'artifact.txt'
    path.write_text('artifact')
    toolkit = sfu.SendFileToolkit(request_id='r', session_id='s', channel_id='web',
                                  metadata={'app_id': 'app', 'access_token': 'private-secret'})
    await toolkit.deliver_projected_artifact(path, {'artifactId': 'private-route', 'metadata': {}})
    assert 'private-secret' not in str(artifact_history)
