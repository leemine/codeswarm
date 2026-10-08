"""Code UI keeps one event consumer and restores only live controls."""
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.extensions.evaluation.backend.adapters.runtime_execution import RuntimeExecution
from jiuwenswarm.runtime.events import RuntimeEvent
from jiuwenswarm.runtime.session.interactions import project_interaction_state
from jiuwenswarm.runtime.session.model import SessionExecutionState as State


def execution(state=State.WAITING_FOR_CONTROL, generation=2, ids=("live",)):
    return NS(state=state, generation=generation, waiting_control_ids=ids,
              waiting_control_id=ids[0] if ids else None, created_at=10)


def test_reconnect_never_makes_old_resolved_or_cancelled_history_actionable():
    records = [dict(event_type="chat.ask_user_question", request_id=value,
                    timestamp=11, questions=[{"question": value}], source="ask_user_interrupt")
               for value in ["old", "resolved", "cancelled", "live"]]
    snapshot = NS(generation=2, executions=[execution(), execution(generation=1, ids=("old",)),
                  execution(State.CANCELLED, ids=("cancelled",)), execution(State.RUNNING, ids=())])
    projection = project_interaction_state(snapshot, records)
    assert projection["is_processing"] is True
    assert [q["request_id"] for q in projection["pending_interactions"]] == ["live"]
    assert projection["pending_interactions"][0]["session_generation"] == 2
    snapshot.executions = [execution(State.CANCELLED)]
    assert project_interaction_state(snapshot, records) == {"is_processing": False, "pending_interactions": []}


@pytest.mark.asyncio
async def test_single_observer_projects_original_wire_identity_and_controls(tmp_path):
    events = [RuntimeEvent("attempt", "web", "session", {"event_type": "chat.tool_call", "tool_call_id": "call"}),
              RuntimeEvent("attempt", "web", "session", {"event_type": "chat.ask_user_question", "request_id": "control", "questions": []})]
    pushes, observed, stream_calls = [], [], []

    async def stream(request, *, on_control_event):
        stream_calls.append(request)
        yield events[0]
        await on_control_event(events[1])

    async def push(value): pushes.append(value)
    async def observer(value): observed.append(value)
    port = RuntimeExecution(NS(stream=stream), send_push=push)
    await port.observe(session_id="session", request_id="attempt",
        definition=NS(model="m#0", execution_profile_id="native"),
        task=NS(instruction="ask first"), workspace=tmp_path, on_event=observer)
    assert len(stream_calls) == 1
    assert observed == events
    assert pushes[1]["payload"] is events[1].payload
    assert pushes[1]["payload"]["request_id"] == "control"
    assert pushes[1]["request_id"] == "attempt"
    assert stream_calls[0].params["work_mode"] == "code"


def test_read_projection_rechecks_session_authority(monkeypatch):
    from jiuwenswarm.runtime.service import AgentRuntime
    from jiuwenswarm.runtime.session_catalog import SessionGetInput
    from jiuwenswarm.server.runtime.session import session_history
    runtime = AgentRuntime.__new__(AgentRuntime)
    reads = iter([NS(), None])
    runtime.get_session = lambda request: next(reads)
    runtime._session_coordinator = NS(snapshot_session=lambda sid: NS(
        channel_id="web", generation=2, executions=[execution()]))
    monkeypatch.setattr(session_history, "load_history_records", lambda sid: [
        dict(event_type="chat.ask_user_question", request_id="live", timestamp=11, questions=[])])
    assert runtime.get_session_interaction_state(SessionGetInput(channel_id="web", session_id="s")) == {
        "is_processing": False, "pending_interactions": []}


@pytest.mark.asyncio
@pytest.mark.parametrize("confirmed", [True, False])
async def test_plugin_cancel_notifies_code_only_after_descendant_exit(confirmed):
    from jiuwenswarm.runtime.session.model import SessionExecutionHandle, SessionWorkKind

    root = SessionExecutionHandle("root", "session", "attempt", 1, SessionWorkKind.CHAT_STREAM,
                                  state=State.SUCCEEDED, finished_at=10)
    child = SessionExecutionHandle("child", "session", "answer", 1, SessionWorkKind.CONTROL_INPUT,
                                   parent_execution_id="root", state=State.WAITING_FOR_CONTROL)

    async def cancel(request):
        assert request.params["target_request_id"] == "answer"
        if confirmed:
            child.state, child.finished_at = State.CANCELLED, 20
        return NS(ok=True, payload={"success": True})

    push = AsyncMock()
    port = RuntimeExecution(NS(cancel_request=cancel, get_session_request_executions=
        lambda *args, **kwargs: (root.snapshot(), child.snapshot())), send_push=push)
    await port.cancel("session", "attempt")
    if confirmed:
        push.assert_awaited_once_with({
            "request_id": "attempt", "channel_id": "web", "session_id": "session",
            "payload": {"event_type": "chat.processing_status", "session_id": "session", "is_processing": False},
            "is_complete": True,
        })
    else:
        push.assert_not_awaited()
