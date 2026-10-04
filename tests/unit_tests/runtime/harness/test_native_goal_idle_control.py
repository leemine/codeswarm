"""Actual Native/Goal exits and host controller bridge; synthetic SDK IO only."""
import asyncio
from dataclasses import replace

import pytest

from jiuwenswarm.runtime.harness.native_goal_idle_control import capture_idle_goal_control
from tests.unit_tests.runtime.harness import test_native_goal_readmission_host as fixtures

goal_case = fixtures.goal_case
persisted = fixtures.persisted
submit = fixtures.submit


@pytest.mark.asyncio
@pytest.mark.parametrize('hot', [False, True])
@pytest.mark.parametrize('action', ['pause', 'clear'])
async def test_idle_control_does_not_allocate_or_execute(goal_case, hot, action):
    c = goal_case
    record = persisted(c)
    old = None
    if hot:
        owner, _, _ = await submit(c, record)
        old = owner.bound[0]
        owner.live = False  # Old authority is not the new controller.
        record = c.outer.goal_manager.peek()
    seen = (len(c.http), len(c.side_effects), c.native._native._first_managed_turn,
            c.outer._interaction_output, dict(c.native._requests))
    cap = capture_idle_goal_control(c.native, expected_record=record,
                                   previous=old, check_current=lambda: None)
    result = await getattr(cap, action)()
    cap.check_result()
    assert result.goal_id == record.goal_id
    if action == 'clear':
        assert c.outer.goal_manager.peek() is None
    else:
        assert c.outer.goal_manager.peek() is not None
    assert (len(c.http), len(c.side_effects), c.native._native._first_managed_turn,
            c.outer._interaction_output, dict(c.native._requests)) == seen
    assert not c.outer._interaction_emit_tasks


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['engine', 'binding', 'manager', 'tool_owner', 'inner', 'session', 'harness'])
async def test_controller_callback_cannot_retarget_original_host(goal_case, change):
    c = goal_case
    record = persisted(c)
    n = c.native
    originals = (n.engine, n.engine.binding, c.outer.goal_manager, n._tool_owner,
                 c.outer.react_agent, n._native._agent_session, n._native)
    changed = []
    def checker():
        if changed:
            return
        changed.append(True)
        if change == 'engine':
            n.engine = object()
        elif change == 'binding':
            object.__setattr__(n.engine, 'binding', replace(n.engine.binding))
        elif change == 'manager':
            c.outer.goal_manager = object()
        elif change == 'tool_owner':
            n._tool_owner = tuple(list(n._tool_owner))
        elif change == 'inner':
            c.outer._react_agent = object()
        elif change == 'session':
            from types import SimpleNamespace
            n._native._agent_session = SimpleNamespace(get_session_id=c.session.get_session_id)
        else:
            n._native = object()
    try:
        with pytest.raises(PermissionError):
            capture_idle_goal_control(n, expected_record=record, previous=None, check_current=checker)
        assert not c.http and not c.side_effects
    finally:
        n.engine = originals[0]
        object.__setattr__(n.engine, 'binding', originals[1])
        c.outer.goal_manager, n._tool_owner = originals[2:4]
        c.outer._react_agent = originals[4]
        n._native = originals[6]
        n._native._agent_session = originals[5]


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['clone', 'missing_terminal', 'missing_notification', 'missing_bound', 'confirmed', 'cleanup'])
async def test_hot_control_requires_actual_retained_host_receipt(goal_case, change):
    c = goal_case
    owner, _, _ = await submit(c, persisted(c))
    old = owner.bound[0]
    entry, barrier = old._entry, old._pending._exit
    saved = (entry.terminal_notified, entry.bound_notified, barrier.confirmed, barrier.cleanup)
    if change == 'clone':
        old = replace(old)
    elif change == 'missing_terminal':
        entry.terminal_event.clear()
    elif change == 'missing_notification':
        entry.terminal_notified = False
    elif change == 'missing_bound':
        entry.bound_notified = False
    elif change == 'confirmed':
        barrier.confirmed = asyncio.get_running_loop().create_future()
    else:
        barrier.cleanup = asyncio.get_running_loop().create_future()
    try:
        with pytest.raises(PermissionError):
            capture_idle_goal_control(c.native, expected_record=c.outer.goal_manager.peek(),
                                      previous=old, check_current=lambda: None)
        assert c.native._requests == {} and c.side_effects == ['goal']
    finally:
        entry.terminal_event.set()
        entry.terminal_notified, entry.bound_notified, barrier.confirmed, barrier.cleanup = saved


@pytest.mark.asyncio
async def test_idle_commit_and_final_ack_recheck_new_controller(goal_case, monkeypatch):
    c = goal_case
    record = persisted(c)
    live = [True]
    def checker():
        if not live[0]:
            raise PermissionError('new controller revoked')
    cap = capture_idle_goal_control(c.native, expected_record=record, previous=None, check_current=checker)
    await cap.pause()
    live[0] = False
    with pytest.raises(PermissionError):
        cap.check_result()
    assert not c.http and not c.side_effects


@pytest.mark.asyncio
async def test_stop_invalidates_previously_captured_idle_controller(goal_case):
    c = goal_case
    cap = capture_idle_goal_control(c.native, expected_record=persisted(c),
                                   previous=None, check_current=lambda: None)
    await c.native.stop()
    with pytest.raises(PermissionError):
        await cap.clear()
    assert not c.http


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['controller', 'binding', 'terminal', 'backing'])
async def test_actual_commit_wait_revalidates_fixed_host_facts(goal_case, monkeypatch, change):
    from types import SimpleNamespace
    c = goal_case
    owner, _, _ = await submit(c, persisted(c))
    old = owner.bound[0]
    entered, release = asyncio.Event(), asyncio.Event()
    live = [True]
    binding = c.native.engine.binding
    store = c.outer.goal_manager._store
    original_commit = store.commit
    original_session = store._session
    def check():
        if not live[0]:
            raise PermissionError('temporary controller expired')
    async def commit():
        await original_commit()
        entered.set()
        await release.wait()
    monkeypatch.setattr(store, 'commit', commit)
    running = None
    try:
        cap = capture_idle_goal_control(c.native, expected_record=c.outer.goal_manager.peek(),
                                       previous=old, check_current=check)
        running = asyncio.create_task(cap.clear())
        await asyncio.wait_for(entered.wait(), 1)
        if change == 'controller':
            live[0] = False
        elif change == 'binding':
            object.__setattr__(c.native.engine, 'binding', replace(binding))
        elif change == 'terminal':
            old._entry.terminal_notified = False
        else:
            store._session = SimpleNamespace(get_session_id=original_session.get_session_id)
        release.set()
        with pytest.raises(PermissionError):
            await running
        assert c.side_effects == ['goal'] and c.native._requests == {}
    finally:
        release.set()
        if running is not None:
            await asyncio.gather(running, return_exceptions=True)
        object.__setattr__(c.native.engine, 'binding', binding)
        old._entry.terminal_notified = True
        store._session = original_session


@pytest.mark.asyncio
async def test_host_caller_cancel_preserves_one_original_idle_commit(goal_case, monkeypatch):
    c = goal_case
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []
    async def commit():
        calls.append(True)
        entered.set()
        await release.wait()
    monkeypatch.setattr(c.outer.goal_manager._store, 'commit', commit)
    cap = capture_idle_goal_control(c.native, expected_record=persisted(c),
                                   previous=None, check_current=lambda: None)
    running = asyncio.create_task(cap.clear())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        task = cap.selector._run.task
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
        assert not task.done() and not task.cancelling()
        release.set()
        result = await cap.clear()
        cap.check_result()
        assert cap.selector._run.task is task and len(calls) == 1
        assert result.goal_id and c.outer.goal_manager.peek() is None
        assert not c.http and not c.native._requests
    finally:
        release.set()
        await asyncio.gather(running, return_exceptions=True)


@pytest.mark.asyncio
async def test_missing_original_pending_never_falls_back_to_latest_host_map(goal_case):
    c = goal_case
    owner, _, _ = await submit(c, persisted(c))
    assert owner.bound[0]._entry.terminal_notified and c.native._requests == {}
    with pytest.raises(RuntimeError):
        capture_idle_goal_control(c.native, expected_record=c.outer.goal_manager.peek(),
                                  previous=None, check_current=lambda: None)
    assert c.side_effects == ['goal']
