"""Original owner authority at rewind consumption, synthetic core only."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from jiuwenswarm.agents.harness.common import session_ops_service as ops
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.governance.session_boundary import admit_session_request
from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer
from jiuwenswarm.server.runtime.session import session_history, session_metadata
from jiuwenswarm.server.runtime.session.rewind_authority import capture_rewind_authority
from tests.unit_tests.runtime import test_continuation_transaction as tx_tests

setup = tx_tests.setup
transaction = tx_tests.transaction
METHODS = (
    "session.rewind",
    "session.rewind_context",
    "session.rewind_and_restore",
    "session.rewind_compact",
)


@pytest.fixture
async def rewind(transaction, monkeypatch):
    tx = transaction
    tx.setup.access.replace_acl(
        tx.setup.target.project_id,
        "admin",
        acl={"bob": ["read", "execute", "write"]},
        expected_revision=2,
    )
    result = await tx_tests.create(tx)
    sid = result.session_id
    path = tx.root / sid / "history.jsonl"
    rows = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "answer"},
        {"role": "user", "content": "second"},
        {"role": "assistant", "content": "answer2"},
    ]
    before = "".join(json.dumps(row) + "\n" for row in rows).encode()
    path.write_bytes(before)
    live = SimpleNamespace(
        get_session_id=lambda: sid, update_state=Mock(), commit=AsyncMock()
    )
    engine = SimpleNamespace(get_context=lambda **kw: None)
    react = SimpleNamespace(context_engine=engine)
    deep = SimpleNamespace(react_agent=react, _interaction_session=live)
    child = SimpleNamespace(_instance=deep)
    adapter = SimpleNamespace(
        _get_cached_session_adapter=lambda value: child if value == sid else None
    )
    agent = SimpleNamespace(
        _adapter=adapter, has_session_runtime=lambda value: value == sid
    )
    tx.manager.agents = {"web": {"owner": agent}}
    server = object.__new__(AgentWebSocketServer)
    server._agent_manager = tx.manager
    server._execution_runtime = lambda: tx.runtime
    server._organization_session_host = tx.setup.host
    server._resolve_trusted_identity = lambda request: tx_tests.BOB
    import jiuwenswarm.governance.organization_auth as auth

    monkeypatch.setattr(auth, "configured_authenticator", lambda: object())

    def request(method="session.rewind_context"):
        return AgentRequest(
            request_id="rewind",
            channel_id="web",
            session_id=sid,
            req_method=ReqMethod(method),
            params={"session_id": sid, "turn_index": 2},
        )

    def capture(req):
        permit = admit_session_request(
            req.req_method.value,
            req.params,
            identity_resolver=lambda: tx_tests.BOB,
            host=tx.setup.host,
            envelope_session=sid,
        )
        return capture_rewind_authority(server, req, permit)

    def revoke():
        tx.setup.host.store.revoke(
            tx.request.share_id, identity=tx_tests.ALICE, expected_revision=1
        )

    yield SimpleNamespace(**locals())


@pytest.mark.asyncio
async def test_resolver_wait_revoke_does_not_truncate_actual_history(
    rewind, monkeypatch
):
    r = rewind
    request = r.request()
    authority = r.capture(request)
    entered, release = asyncio.Event(), asyncio.Event()

    async def resolve(*args, **kwargs):
        entered.set()
        await release.wait()
        return r.deep, r.react

    monkeypatch.setattr(r.server, "_resolve_rewind_agent", resolve)
    context = AsyncMock(return_value=True)
    monkeypatch.setattr(ops, "rewind_session_context", context)
    ws = SimpleNamespace(send=AsyncMock())
    task = asyncio.create_task(
        r.server._handle_session_rewind_context(
            ws, request, asyncio.Lock(), authorization_check=authority
        )
    )
    await entered.wait()
    r.revoke()
    release.set()
    with pytest.raises(PermissionError):
        await task
    assert r.path.read_bytes() == r.before
    context.assert_not_awaited()
    ws.send.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("method", METHODS)
async def test_before_chat_hook_revoke_rejects_all_four_entries(
    rewind, monkeypatch, method
):
    r = rewind
    import jiuwenswarm.server.agent_ws_server as module
    import jiuwenswarm.governance.project_boundary as project

    request = r.request(method)
    monkeypatch.setattr(
        module.E2AEnvelope, "from_dict", Mock(side_effect=ValueError("legacy fixture"))
    )
    monkeypatch.setattr(module, "_payload_to_request", lambda _: request)
    original_authorize = project.authorize_resource_request
    monkeypatch.setattr(
        project,
        "authorize_resource_request",
        lambda *a, **kw: original_authorize(*a, **kw, access_store=r.tx.setup.access),
    )
    for name in (
        "_handle_gateway_cron_callback",
        "_handle_lifecycle_request",
        "_dispatch_gateway_adapter_request",
    ):
        monkeypatch.setattr(r.server, name, AsyncMock(return_value=False))
    entered, release = asyncio.Event(), asyncio.Event()

    async def hook(_request):
        entered.set()
        await release.wait()

    monkeypatch.setattr(r.server, "_trigger_before_chat_request_hook", hook)
    full = AsyncMock()
    context = AsyncMock()
    monkeypatch.setattr(r.server, "_handle_session_rewind_full", full)
    monkeypatch.setattr(r.server, "_handle_session_rewind_context", context)
    ws = SimpleNamespace(send=AsyncMock())
    task = asyncio.create_task(
        r.server._handle_authenticated_message(ws, "{}", asyncio.Lock())
    )
    await entered.wait()
    r.revoke()
    release.set()
    await task
    full.assert_not_awaited()
    context.assert_not_awaited()
    assert r.path.read_bytes() == r.before
    assert ws.send.await_count == 1
    assert "FORBIDDEN" in ws.send.call_args.args[0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        "wire",
        "binding",
        "agent",
        "child",
        "engine",
        "session",
        "generation",
        "write_acl",
    ],
)
async def test_original_objects_binding_and_generation_cannot_be_reselected(
    rewind, change
):
    r = rewind
    request = r.request()
    guard = r.capture(request)
    guard()
    if change == "wire":
        request.params["turn_index"] = 1
    elif change == "binding":
        from jiuwenswarm.server.runtime.session import lifecycle

        metadata = lifecycle.raw_metadata(r.sid)
        metadata["channel_id"] = "tui"
        (r.tx.root / r.sid / "metadata.json").write_text(json.dumps(metadata))
    elif change == "agent":
        r.tx.manager.agents["web"]["owner"] = SimpleNamespace()
    elif change == "child":
        r.adapter._get_cached_session_adapter = lambda _: SimpleNamespace(
            _instance=r.deep
        )
    elif change == "engine":
        r.react.context_engine = SimpleNamespace()
    elif change == "session":
        r.deep._interaction_session = SimpleNamespace(get_session_id=lambda: r.sid)
    elif change == "generation":
        await r.tx.runtime._session_coordinator.close_session(r.sid)
    else:
        r.tx.setup.access.replace_acl(
            r.tx.setup.target.project_id,
            "admin",
            acl={"bob": ["read", "execute"]},
            expected_revision=3,
        )
    with pytest.raises(PermissionError):
        guard()


@pytest.mark.asyncio
async def test_exact_cached_resolver_never_allocates_or_uses_wire_default(rewind):
    r = rewind
    request = r.request()
    guard = r.capture(request)
    assert await r.server._resolve_rewind_agent(
        "web", session_id=r.sid, authorization_check=guard
    ) == (r.deep, r.react)
    with pytest.raises(PermissionError):
        await r.server._resolve_rewind_agent(
            "tui", session_id=r.sid, authorization_check=guard
        )
    with pytest.raises(PermissionError):
        await r.server._resolve_rewind_agent("web", session_id=r.sid)


@pytest.mark.asyncio
@pytest.mark.parametrize("writer", ["truncate", "metadata", "compact_append"])
async def test_current_guard_at_actual_file_consumption(rewind, writer):
    r = rewind
    guard = r.capture(r.request())
    r.revoke()
    with pytest.raises(PermissionError):
        if writer == "truncate":
            session_history.truncate_history_records(
                session_id=r.sid, cut_index=2, authorization_check=guard
            )
        elif writer == "metadata":
            session_metadata.update_session_metadata(
                session_id=r.sid,
                set_message_count=0,
                sync_write=True,
                cache_bust=True,
                authorization_check=guard,
            )
        else:
            session_history.append_history_record(
                session_id=r.sid,
                request_id="compact",
                channel_id="web",
                role="assistant",
                content="summary",
                event_type="context.compact_summary",
                timestamp=1,
                authorization_check=guard,
            )
    assert r.path.read_bytes() == r.before


@pytest.mark.asyncio
@pytest.mark.parametrize("waitpoint", ["clear", "create", "save", "commit"])
async def test_context_await_denial_stops_next_mutation(waitpoint):
    allowed = True
    events = []
    current = SimpleNamespace(get_messages=lambda: [])

    def check():
        if not allowed:
            raise PermissionError("revoked")

    async def point(name):
        nonlocal allowed
        events.append(name)
        await asyncio.sleep(0)
        if waitpoint == name:
            allowed = False

    async def clear(**kw):
        nonlocal current
        current = None
        await point("clear")

    async def create(**kw):
        nonlocal current
        current = object()
        await point("create")
        return current

    async def save(*args):
        await point("save")

    async def commit():
        await point("commit")

    engine = SimpleNamespace(
        get_context=lambda **kw: current,
        clear_context=clear,
        create_context=create,
        save_contexts=save,
    )
    react = SimpleNamespace(context_engine=engine, _context_processors=[])
    live = SimpleNamespace(
        get_session_id=lambda: "s",
        update_state=lambda _: events.append("update"),
        commit=commit,
    )
    deep = SimpleNamespace(
        react_agent=react,
        _interaction_session=live,
        save_state=lambda _: events.append("state"),
    )
    with pytest.raises(PermissionError):
        await ops._apply_rewound_context(
            deep_agent=deep,
            react_agent=react,
            session_id="s",
            turn_index=1,
            context_messages=[],
            skipped=0,
            authorization_check=check,
        )
    if waitpoint == "clear":
        assert events == ["clear"]
    elif waitpoint == "create":
        assert "save" not in events
    elif waitpoint == "save":
        assert "state" not in events and "commit" not in events
    else:
        assert events[-1] == "commit"


@pytest.mark.asyncio
@pytest.mark.parametrize("writer", ["history", "metadata"])
async def test_revoke_during_staging_does_not_publish_file(rewind, monkeypatch, writer):
    r = rewind
    from pathlib import Path

    guard = r.capture(r.request())
    original = Path.write_text
    before = (r.tx.root / r.sid / "metadata.json").read_bytes()
    if writer == "metadata":

        def write(path, *args, **kwargs):
            result = original(path, *args, **kwargs)
            if path.name.startswith("metadata.json.") and path.suffix == ".tmp":
                r.revoke()
            return result

        monkeypatch.setattr(Path, "write_text", write)
        with pytest.raises(PermissionError):
            session_metadata.update_session_metadata(
                session_id=r.sid,
                set_message_count=0,
                sync_write=True,
                cache_bust=True,
                authorization_check=guard,
            )
        assert (r.tx.root / r.sid / "metadata.json").read_bytes() == before
    else:
        original_sync = session_history.os.fsync
        fired = False

        def fsync(fd):
            nonlocal fired
            original_sync(fd)
            if not fired:
                fired = True
                r.revoke()

        monkeypatch.setattr(session_history.os, "fsync", fsync)
        with pytest.raises(PermissionError):
            session_history.truncate_history_records(
                session_id=r.sid, cut_index=2, authorization_check=guard
            )
        assert r.path.read_bytes() == r.before


@pytest.mark.asyncio
async def test_valid_guarded_writes_keep_existing_format_and_synchronous_metadata(
    rewind,
):
    r = rewind
    guard = r.capture(r.request())
    result = ops.rewind_session(
        session_id=r.sid, turn_index=2, authorization_check=guard
    )
    assert result["remaining_records"] == 2
    rows = [json.loads(line) for line in r.path.read_text().splitlines()]
    assert [row["content"] for row in rows] == ["first", "answer"]
    session_history.append_history_record(
        session_id=r.sid,
        request_id="compact",
        channel_id="web",
        role="assistant",
        content="summary",
        event_type="context.compact_summary",
        timestamp=1,
        authorization_check=guard,
    )
    assert json.loads(r.path.read_text().splitlines()[-1])["content"] == "summary"
    assert (
        json.loads((r.tx.root / r.sid / "metadata.json").read_text())["message_count"]
        == 3
    )


def test_restore_revalidates_each_file_and_does_not_swallow_denial(
    tmp_path, monkeypatch
):
    from jiuwenswarm.server.utils import diff_service

    first, second = tmp_path / "first", tmp_path / "second"
    first.write_text("new")
    second.write_text("new")
    service = SimpleNamespace(
        get_files_to_restore=lambda *a, **kw: {
            str(first): {"action": "write", "restore_content": "old"},
            str(second): {"action": "delete"},
        }
    )
    monkeypatch.setattr(diff_service, "get_diff_service", lambda: service)

    def check():
        if first.read_text() == "old":
            raise PermissionError("revoked after first write")

    with pytest.raises(PermissionError):
        ops.restore_session_files(
            session_id="s",
            turn_index=1,
            project_dir=str(tmp_path),
            authorization_check=check,
        )
    assert first.read_text() == "old"
    assert second.read_text() == "new"


@pytest.mark.asyncio
async def test_context_object_replacement_after_save_is_not_committed():
    original, replacement = object(), object()
    current = original

    async def save(_):
        nonlocal current
        await asyncio.sleep(0)
        current = replacement

    session = SimpleNamespace(commit=AsyncMock())
    deep = SimpleNamespace(save_state=Mock())
    engine = SimpleNamespace(save_contexts=save)

    def check():
        if current is not original:
            raise PermissionError("context replaced")

    with pytest.raises(PermissionError):
        await ops._persist_rewound_session(
            deep_agent=deep,
            session=session,
            context_engine=engine,
            session_id="s",
            is_live_session=True,
            authorization_check=check,
        )
    deep.save_state.assert_not_called()
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("method", METHODS)
async def test_hook_context_replacement_cannot_be_selected(rewind, monkeypatch, method):
    r = rewind
    request = r.request(method)
    guard = r.capture(request)
    r.engine.get_context = lambda **kw: object()
    handler = (
        r.server._handle_session_rewind_context
        if method == "session.rewind_context"
        else r.server._handle_session_rewind_full
    )
    ws = SimpleNamespace(send=AsyncMock())
    with pytest.raises(PermissionError):
        await handler(ws, request, asyncio.Lock(), authorization_check=guard)
    assert r.path.read_bytes() == r.before


@pytest.mark.asyncio
async def test_real_core_clear_event_revoke_blocks_later_rebuild(monkeypatch):
    # Actual core clears synchronously BEFORE its after-event await. The host
    # must block later actions; it cannot undo an earlier authorized clear.
    import openjiuwen.core.context_engine.context_engine as module

    engine = module.ContextEngine()
    context = SimpleNamespace(
        context_id=lambda: "default_context_id",
        session_id=lambda: "s",
        get_messages=lambda: [],
    )
    engine._context_pool["s_default_context_id"] = context
    allowed = True
    events = []

    async def event(*args, **kwargs):
        nonlocal allowed
        assert not engine._context_pool
        events.append("cleared-before-event")
        await asyncio.sleep(0)
        allowed = False

    monkeypatch.setattr(module, "trigger", event)

    def check():
        if not allowed:
            raise PermissionError("revoked in after-event")

    react = SimpleNamespace(context_engine=engine)
    live = SimpleNamespace(
        get_session_id=lambda: "s", update_state=Mock(), commit=AsyncMock()
    )
    deep = SimpleNamespace(react_agent=react, _interaction_session=live)
    create = AsyncMock()
    monkeypatch.setattr(engine, "create_context", create)
    with pytest.raises(PermissionError):
        await ops._apply_rewound_context(
            deep_agent=deep,
            react_agent=react,
            session_id="s",
            turn_index=1,
            context_messages=[],
            skipped=0,
            authorization_check=check,
        )
    assert events == ["cleared-before-event"]
    create.assert_not_awaited()
    live.update_state.assert_not_called()
    live.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_guarded_context_rebuild_can_replace_its_own_context_and_commit(rewind):
    r = rewind
    current = SimpleNamespace(get_messages=lambda: [])
    r.engine.get_context = lambda **kw: current
    guard = r.capture(r.request())

    async def clear(**kw):
        nonlocal current
        current = None

    async def create(**kw):
        nonlocal current
        current = object()
        return current

    r.engine.clear_context = clear
    r.engine.create_context = create
    r.engine.save_contexts = AsyncMock()
    r.deep.save_state = Mock()
    assert await ops._apply_rewound_context(
        deep_agent=r.deep,
        react_agent=r.react,
        session_id=r.sid,
        turn_index=2,
        context_messages=[],
        skipped=0,
        authorization_check=guard,
    )
    r.engine.save_contexts.assert_awaited_once_with(r.live)
    r.deep.save_state.assert_called_once_with(r.live)
    r.live.commit.assert_awaited_once()
