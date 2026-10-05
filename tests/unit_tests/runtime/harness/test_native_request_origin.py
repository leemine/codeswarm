"""Native host source over real core queue/IO/TaskLoop; synthetic model/session IO."""
import asyncio
import copy
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from openjiuwen.core.controller.config import ControllerConfig
from openjiuwen.core.controller.modules.task_manager import TaskManager
from openjiuwen.core.controller.modules.task_scheduler import TaskScheduler
from openjiuwen.core.controller.schema.event import EventType
from openjiuwen.core.controller.schema.execution_origin import ExecutionOrigin
from openjiuwen.core.single_agent.schema.agent_card import AgentCard
from openjiuwen.harness.deep_agent import DeepAgent
from openjiuwen.harness.schema.config import DeepAgentConfig
from openjiuwen.harness.schema.interaction import InputDispatchMode, SendInputRequest
from openjiuwen.harness.task_loop.loop_coordinator import LoopCoordinator
from openjiuwen.harness.task_loop.loop_queues import LoopQueues
from openjiuwen.harness.task_loop.task_loop_controller import TaskLoopController
from openjiuwen.harness.task_loop.task_loop_event_executor import DEEP_TASK_TYPE, build_deep_executor
from openjiuwen.harness.task_loop.task_loop_event_handler import TaskLoopEventHandler
from openjiuwen.harness_protocol import AgentExecutionSpec, HarnessContext, HarnessInput, TurnEventKind

from jiuwenswarm.governance.tool_context import (
    ExecutionResourceAuthorities, submitted_native_lifecycle_factory, tool_authority_scope,
)
from jiuwenswarm.runtime.harness.binding_store import ExecutionBindingStore
from jiuwenswarm.runtime.harness.config_source import ExecutionConfigSource
from jiuwenswarm.runtime.harness.native_session import NativeExecutionSession, NativeRequestLifecycle


class _Session:
    def __init__(self):
        self.state = {}
        self.write_stream = AsyncMock()

    def get_session_id(self):
        return "origin-session"

    def get_state(self, key=None):
        return dict(self.state) if key is None else self.state.get(key)

    def update_state(self, data):
        self.state.update(data)


@pytest.fixture
async def native_case(tmp_path, monkeypatch):
    agent = DeepAgent(AgentCard(name="origin-test", description="test"))
    agent.configure(DeepAgentConfig(enable_task_loop=True))
    calls = []

    async def invoke(inputs, session=None, **kwargs):
        calls.append(inputs)
        return {"output": "synthetic:" + inputs["query"]}

    react = SimpleNamespace(invoke=invoke, register_callback=AsyncMock(),
                            agent_callback_manager=SimpleNamespace(execute=AsyncMock(), unregister_rail=AsyncMock()))
    agent.set_react_agent(react, initialized=True)
    agent._interaction_started = True
    session = _Session()
    agent._interaction_session = session
    agent._loop_coordinator = LoopCoordinator()
    agent._loop_coordinator.reset()
    handler = TaskLoopEventHandler(agent)
    handler.interaction_queues = LoopQueues()
    config = ControllerConfig(schedule_interval=60)
    manager = TaskManager(config)
    loop = TaskLoopController()
    loop._card = agent.card
    loop._event_handler = handler
    agent._loop_controller = loop
    handler.task_manager = manager

    async def publish(_id, actual_session, event):
        inputs = SimpleNamespace(event=event, session=actual_session)
        method = {EventType.INPUT: handler.handle_input,
                  EventType.TASK_COMPLETION: handler.handle_task_completion,
                  EventType.TASK_FAILED: handler.handle_task_failed}[event.event_type]
        return await method(inputs)

    queue = SimpleNamespace(publish_event=publish)
    loop._event_queue = queue
    scheduler = TaskScheduler(config, manager, Mock(), Mock(), queue, agent.card)
    manager.set_on_task_submitted(scheduler._submit_event.set)
    scheduler._sessions[session.get_session_id()] = session
    scheduler._task_executor_registry.add_task_executor(DEEP_TASK_TYPE, build_deep_executor(agent))
    scheduler._ensure_session_completion_signal = AsyncMock()
    loop._task_scheduler = scheduler
    handler.task_scheduler = scheduler
    monkeypatch.setattr(agent, "prepare_interaction_task_loop", AsyncMock(return_value=(agent.loop_coordinator, loop)))
    monkeypatch.setattr(agent, "_emit_round_boundary", AsyncMock(return_value=False))
    monkeypatch.setattr(agent, "_write_round_result_to_stream", AsyncMock())
    monkeypatch.setattr(agent, "_has_remaining_tasks", lambda *_: False)
    await scheduler.start()
    bound = ExecutionBindingStore().bind(
        ExecutionConfigSource(explicit=AgentExecutionSpec("native", "test")),
        subject_id="bob", host_session_id=session.get_session_id(), workspace=str(tmp_path))
    native = NativeExecutionSession(bound, agent_factory=lambda _: agent,
                                    session_factory=AsyncMock(return_value=session), require_execution_origin=True)

    async def opened(_):
        native._native._agent = agent
        native._native._agent_session = session
        return session.get_session_id()

    async def closed():
        for task in (agent._interaction_supervisor_task, agent._interaction_forwarder_task):
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        await scheduler.stop()
        native._native._agent = None
        native._native._agent_session = None

    monkeypatch.setattr(native._native, "_open_session", opened)
    monkeypatch.setattr(native._native, "_close_session", closed)
    await native.start(HarnessContext(agent_name="native", agent_id="a", host_session_id=session.get_session_id(),
                                      cwd=str(tmp_path), system_prompt=""))
    case = SimpleNamespace(**locals())
    try:
        yield case
    finally:
        await asyncio.wait_for(native.stop(), 3)


class _Admission:
    def __init__(self):
        self.live = True
        self.bound = []
        self.terminal = []
        self.rejected = []
        self.source = ExecutionOrigin(self, _checker=self.check)
        self.lifecycle = NativeRequestLifecycle(self.source, self.bound.append,
                                               lambda owned, kind: self.terminal.append((owned, kind)),
                                               lambda: self.rejected.append(True))

    def check(self):
        if not self.live:
            raise PermissionError("original credential revoked")

    @contextmanager
    def scope(self):
        with tool_authority_scope(None, provider_authorizers=ExecutionResourceAuthorities(
                {}, native_lifecycle_factory=lambda native, request: self.lifecycle)):
            yield


async def _done(owner):
    await asyncio.wait_for(owner.bound[0]._entry.terminal_event.wait(), 3)


@pytest.mark.asyncio
async def test_sequential_real_taskloop_sources_outlive_scope_without_switching(native_case):
    c = native_case
    first, second = _Admission(), _Admission()
    for index, owner in enumerate((first, second)):
        with owner.scope():
            receipt = await c.native.send_request(SendInputRequest(str(index), {"query": str(index)}))
        await _done(owner)
        assert owner.bound[0].turn_id == receipt.turn_id
        assert owner.bound[0]._pending._origin.host_value is owner
        assert owner.terminal == [(owner.bound[0], TurnEventKind.FINISHED)]
        assert copy.deepcopy(owner.lifecycle) is owner.lifecycle
        assert copy.deepcopy(owner.bound[0]) is owner.bound[0]
    assert [x["query"] for x in c.calls] == ["0", "1"]
    assert c.native._requests == {}


@pytest.mark.asyncio
async def test_exact_queued_abort_waits_original_observer_not_new_active(native_case):
    c = native_case
    entered, release = asyncio.Event(), asyncio.Event()
    original = c.react.invoke
    async def invoke(inputs, *args, **kwargs):
        if inputs["query"] == "A":
            entered.set()
            await release.wait()
        return await original(inputs, *args, **kwargs)
    c.react.invoke = invoke
    first, queued = _Admission(), _Admission()
    try:
        with first.scope():
            await c.native.send_request(SendInputRequest("A", {"query": "A"}))
        await asyncio.wait_for(entered.wait(), 2)
        with queued.scope():
            await c.native.send_request(SendInputRequest("B", {"query": "B"}))
        abort = asyncio.create_task(c.native.abort_owned_request_turn(queued.bound[0]))
        await asyncio.sleep(0)
        assert not abort.done()
        assert c.native._native.active_turn is first.bound[0]._pending
        release.set()
        await asyncio.wait_for(abort, 3)
        assert queued.terminal == [(queued.bound[0], TurnEventKind.ABORTED)]
        assert [x["query"] for x in c.calls] == ["A"]
        await c.native.abort_owned_request_turn(queued.bound[0])
        assert len(queued.terminal) == 1
    finally:
        release.set()


@pytest.mark.asyncio
async def test_fast_terminal_before_outer_send_receipt_preserves_entry(native_case, monkeypatch):
    c = native_case
    owner = _Admission()
    class Router:
        async def submit(self, send):
            receipt = await send()
            await _done(owner)
            return receipt
    # The real core IO/supervisor runs; only the outer mailbox wait is synthetic.
    c.native._output_router = Router()
    try:
        with owner.scope():
            receipt = await c.native.send_request(SendInputRequest("fast", {"query": "fast"}))
        assert owner.bound[0].turn_id == receipt.turn_id
        assert owner.terminal[0][1] is TurnEventKind.FINISHED
        assert len(owner.bound) == 1
    finally:
        c.native._output_router = None


@pytest.mark.asyncio
async def test_caller_cancel_after_core_receipt_keeps_original_owned_turn(native_case):
    c = native_case
    accepted, release = asyncio.Event(), asyncio.Event()
    owner = _Admission()
    class Router:
        async def submit(self, send):
            await send()
            accepted.set()
            await release.wait()
    c.native._output_router = Router()
    async def submit():
        with owner.scope():
            await c.native.send_request(SendInputRequest("cancel", {"query": "cancel"}))
    task = asyncio.create_task(submit())
    try:
        await asyncio.wait_for(accepted.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(owner.bound) == 1 and not owner.rejected
        await asyncio.wait_for(c.native.abort_owned_request_turn(owner.bound[0]), 3)
    finally:
        release.set()
        c.native._output_router = None


@pytest.mark.asyncio
async def test_original_source_rejected_before_core_admission(native_case):
    owner = _Admission()
    owner.live = False
    with owner.scope(), pytest.raises(PermissionError, match="revoked"):
        await native_case.native.send_request(SendInputRequest("bad", {"query": "bad"}))
    assert owner.rejected == [True]
    assert not owner.bound and not native_case.calls
    assert native_case.native._requests == {}


@pytest.mark.asyncio
async def test_core_hook_rejection_releases_only_known_unadmitted(native_case, monkeypatch):
    c = native_case
    owner = _Admission()
    original = c.native.io.send
    async def revoke(*args, **kwargs):
        owner.live = False
        return await original(*args, **kwargs)
    monkeypatch.setattr(c.native.io, "send", revoke)
    with owner.scope(), pytest.raises(PermissionError, match="revoked"):
        await c.native.send_request(SendInputRequest("bad", {"query": "bad"}))
    assert owner.rejected == [True]
    assert not owner.bound and c.native._requests == {}


@pytest.mark.asyncio
async def test_unknown_send_failure_is_not_non_admission(native_case, monkeypatch):
    c = native_case
    owner = _Admission()
    monkeypatch.setattr(c.native.io, "send", AsyncMock(side_effect=RuntimeError("unknown")))
    with owner.scope(), pytest.raises(RuntimeError, match="unknown"):
        await c.native.send_request(SendInputRequest("unknown", {"query": "unknown"}))
    assert not owner.rejected and not owner.bound
    assert len(c.native._requests) == 1


@pytest.mark.asyncio
async def test_scope_expiry_masks_inherited_factory_but_not_captured_origin():
    owner = _Admission()
    release = asyncio.Event()
    async def inherited():
        await release.wait()
        with pytest.raises(PermissionError, match="scope has ended"):
            submitted_native_lifecycle_factory()
        owner.source._check_current()
    with owner.scope():
        captured = submitted_native_lifecycle_factory()
        task = asyncio.create_task(inherited())
        with tool_authority_scope(None):
            assert submitted_native_lifecycle_factory() is None
    with pytest.raises(PermissionError, match="scope has ended"):
        captured(None, None)
    release.set()
    await task
    assert submitted_native_lifecycle_factory() is None


@pytest.mark.asyncio
async def test_managed_cannot_use_missing_factory_or_forged_token(native_case):
    c = native_case
    with pytest.raises(PermissionError, match="original host source"):
        await c.native.send_request(SendInputRequest("missing", {"query": "missing"}))
    with pytest.raises(PermissionError, match="source is unavailable"):
        c.native._capture_execution_origin(HarnessInput(content="x", metadata={"native.host_request": "forged"}))
    assert c.calls == []


@pytest.mark.asyncio
async def test_cached_legacy_rejects_new_managed_factory(tmp_path):
    bound = ExecutionBindingStore().bind(ExecutionConfigSource(explicit=AgentExecutionSpec("native", "test")),
                                        subject_id="bob", host_session_id="legacy", workspace=str(tmp_path))
    native = NativeExecutionSession(bound, agent_factory=Mock(), session_factory=AsyncMock())
    with _Admission().scope(), pytest.raises(PermissionError, match="Legacy Native"):
        await native.send_request(SendInputRequest("bad", {"query": "bad"}))
    assert native._requests == {}


@pytest.mark.asyncio
async def test_managed_steer_refuses_before_factory(native_case):
    from openjiuwen.harness_protocol import UnsupportedHarnessCapabilityError
    factory = Mock()
    with tool_authority_scope(None, provider_authorizers=ExecutionResourceAuthorities({}, native_lifecycle_factory=factory)):
        with pytest.raises(UnsupportedHarnessCapabilityError, match="STEER"):
            await native_case.native.send_request(SendInputRequest("steer", {"query": "new"}, mode=InputDispatchMode.STEER))
    factory.assert_not_called()
    assert not native_case.native._requests


@pytest.mark.asyncio
async def test_goal_controls_and_handoff_require_explicit_selector(native_case):
    from openjiuwen.harness_protocol import UnsupportedHarnessCapabilityError
    c = native_case
    c.native._goal_dispatcher = AsyncMock()
    with pytest.raises(UnsupportedHarnessCapabilityError, match="owner selector"):
        await c.native.control_goal("pause")
    with pytest.raises(UnsupportedHarnessCapabilityError, match="service admission"):
        await c.native._attach_active_goal_after_eof()
    c.native._goal_dispatcher.assert_not_called()


@pytest.mark.asyncio
async def test_active_goal_rejects_before_factory(native_case):
    from openjiuwen.harness_protocol import UnsupportedHarnessCapabilityError
    c = native_case
    entered, release = asyncio.Event(), asyncio.Event()
    async def invoke(*_, **__):
        entered.set()
        await release.wait()
        return {"output": "ok"}
    c.react.invoke = invoke
    c.native._goal_dispatcher = AsyncMock()
    owner = _Admission()
    factory = Mock()
    try:
        with owner.scope():
            await c.native.send_request(SendInputRequest("a", {"query": "a"}))
        await asyncio.wait_for(entered.wait(), 2)
        with tool_authority_scope(None, provider_authorizers=ExecutionResourceAuthorities({}, native_lifecycle_factory=factory)):
            with pytest.raises(UnsupportedHarnessCapabilityError, match="active Goal"):
                await c.native.submit_goal("set")
        factory.assert_not_called()
        c.native._goal_dispatcher.assert_not_called()
    finally:
        release.set()
        await _done(owner)


@pytest.mark.asyncio
async def test_answer_keeps_parent_origin_without_new_admission(native_case, monkeypatch):
    from openjiuwen.core.session.interaction.interactive_input import InteractiveInput
    c = native_case
    entered, release = asyncio.Event(), asyncio.Event()
    async def invoke(*_, **__):
        entered.set()
        await release.wait()
        return {"output": "ok"}
    c.react.invoke = invoke
    owner = _Admission()
    try:
        with owner.scope():
            await c.native.send_request(SendInputRequest("a", {"query": "a"}))
        await asyncio.wait_for(entered.wait(), 2)
        # Interaction payload routing is synthetic here; source selection remains
        # the original actual Native entry and no fresh lifecycle is requested.
        monkeypatch.setattr(c.native.io, "is_pending_interrupt_resume_valid", lambda _: True)
        monkeypatch.setattr(type(c.native.io), "pending_interrupt_ids", property(lambda _: ("q",)))
        monkeypatch.setattr(c.native.io, "send", AsyncMock(return_value=None))
        factory = Mock(side_effect=AssertionError("must not create a new admission"))
        answer = InteractiveInput()
        answer.update("q", "yes")
        request = SendInputRequest("control", {"query": answer})
        with tool_authority_scope(None, provider_authorizers=ExecutionResourceAuthorities({}, native_lifecycle_factory=factory)):
            assert await c.native.answer_request(request)
        factory.assert_not_called()
        assert owner.bound[0]._entry.lifecycle is owner.lifecycle
        owner.bound[0]._entry.answered.clear()
        owner.live = False
        with pytest.raises(PermissionError, match="revoked"):
            await c.native.answer_request(request)
        owner.live = True
    finally:
        release.set()
        await _done(owner)


@pytest.mark.asyncio
async def test_exact_abort_caller_cancel_and_timeout_keep_original_for_retry(native_case, monkeypatch):
    c = native_case
    entered, release = asyncio.Event(), asyncio.Event()
    original = c.scheduler._ensure_session_completion_signal
    async def tail(*args):
        entered.set()
        await release.wait()
        return await original(*args)
    c.scheduler._ensure_session_completion_signal = tail
    owner = _Admission()
    try:
        with owner.scope():
            await c.native.send_request(SendInputRequest("tail", {"query": "tail"}))
        await asyncio.wait_for(entered.wait(), 2)
        abort = asyncio.create_task(c.native.abort_owned_request_turn(owner.bound[0]))
        await asyncio.sleep(0)
        abort.cancel()
        with pytest.raises(asyncio.CancelledError):
            await abort
        assert not owner.terminal
        assert not owner.bound[0]._entry.terminal_event.is_set()
        release.set()
        await asyncio.wait_for(c.native.abort_owned_request_turn(owner.bound[0]), 3)
        assert len(owner.terminal) == 1
    finally:
        release.set()


@pytest.mark.asyncio
async def test_failed_rejection_notification_does_not_mask_original(native_case):
    owner = _Admission()
    owner.live = False
    owner.lifecycle = NativeRequestLifecycle(owner.source, owner.bound.append, lambda *_: None,
                                            Mock(side_effect=RuntimeError("callback-private-detail")))
    with owner.scope(), pytest.raises(PermissionError, match="credential revoked"):
        await native_case.native.send_request(SendInputRequest("bad", {"query": "bad"}))


@pytest.mark.asyncio
async def test_factory_type_failure_cannot_downgrade(native_case):
    with tool_authority_scope(None, provider_authorizers=ExecutionResourceAuthorities({}, native_lifecycle_factory=lambda *_: None)):
        with pytest.raises(TypeError, match="synchronous lifecycle"):
            await native_case.native.send_request(SendInputRequest("bad", {"query": "bad"}))
    assert not native_case.calls


@pytest.mark.asyncio
async def test_late_abort_of_completed_original_does_not_stop_successor(native_case):
    c = native_case
    first, second = _Admission(), _Admission()
    with first.scope():
        await c.native.send_request(SendInputRequest("a", {"query": "a"}))
    await _done(first)
    entered, release = asyncio.Event(), asyncio.Event()
    async def invoke(*_, **__):
        entered.set()
        await release.wait()
        return {"output": "b"}
    c.react.invoke = invoke
    try:
        with second.scope():
            await c.native.send_request(SendInputRequest("b", {"query": "b"}))
        await asyncio.wait_for(entered.wait(), 2)
        await c.native.abort_owned_request_turn(first.bound[0])
        assert not second.bound[0]._pending.abort_requested
        assert not second.terminal
    finally:
        release.set()
        await _done(second)


@pytest.mark.asyncio
async def test_terminal_event_without_core_exit_proof_is_not_confirmation(native_case):
    from jiuwenswarm.runtime.harness.execution_session import ExecutionExitUnconfirmedError
    c = native_case
    owner = _Admission()
    entered, release = asyncio.Event(), asyncio.Event()
    async def invoke(*_, **__):
        entered.set()
        await release.wait()
        return {"output": "a"}
    c.react.invoke = invoke
    try:
        with owner.scope():
            await c.native.send_request(SendInputRequest("a", {"query": "a"}))
        await asyncio.wait_for(entered.wait(), 2)
        with pytest.raises(ExecutionExitUnconfirmedError):
            c.native._check_terminal_exit(owner.bound[0]._entry, TurnEventKind.FAILED)
        assert not owner.terminal
    finally:
        release.set()
        await _done(owner)
