"""Mandatory Native slices use the actual callback chain and task lifetime."""

import asyncio
from types import SimpleNamespace

import pytest

from openjiuwen.core.runner import Runner
from openjiuwen.core.runner.callback import AsyncCallbackFramework
from openjiuwen.core.runner.callback.enums import HookType
from openjiuwen.core.runner.callback.errors import AbortError
from openjiuwen.core.single_agent.agent_callback_manager import AgentCallbackManager
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext, AgentCallbackEvent

from jiuwenswarm.agents.harness.common.rails.permissions.resource_authority_rail import NativeExecutionScopeRail
from jiuwenswarm.governance import tool_context as scopes
from jiuwenswarm.governance.resources import ResourceAccessDenied


@pytest.fixture
def callback_framework(monkeypatch):
    framework = AsyncCallbackFramework()
    monkeypatch.setattr(Runner, 'callback_framework', framework)
    token = scopes._NATIVE_SLICE.set(None)
    try:
        yield framework
    finally:
        scopes._NATIVE_SLICE.reset(token)


async def callback_context():
    agent = SimpleNamespace(agent_callback_manager=AgentCallbackManager('native-slice-fixture'))
    rail = NativeExecutionScopeRail()
    await agent.agent_callback_manager.register_rail(rail, agent)
    return AgentCallbackContext(agent=agent, inputs=SimpleNamespace(), session=None)


def fresh_slice(_):
    return scopes.NativeExecutionSlice(object(), None, None, None)


@pytest.mark.asyncio
async def test_scope_admission_denial_aborts_real_callback_chain_and_masks_inherited_scope(callback_framework):
    ctx = await callback_context()
    reached = []

    async def later(_):
        reached.append(True)

    await ctx.agent.agent_callback_manager.register_callback(AgentCallbackEvent.BEFORE_INVOKE, later, priority=0)
    with scopes.native_authority_source_scope(lambda: None, slice_source=fresh_slice):
        parent = scopes.begin_native_execution_slice(object())
        inherited = scopes.current_native_execution_slice()

        def invalid_child(_):
            raise ResourceAccessDenied('wrong synthetic owner')

        try:
            with scopes.native_authority_source_scope(lambda: None, slice_source=invalid_child):
                with pytest.raises(ResourceAccessDenied):
                    await ctx.fire(AgentCallbackEvent.BEFORE_INVOKE)
            assert scopes.current_native_execution_slice() is not inherited
            assert not scopes.current_native_execution_slice().active
            assert reached == []
            await ctx.fire(AgentCallbackEvent.AFTER_INVOKE)
            assert not scopes.current_native_execution_slice().active
        finally:
            scopes.end_native_execution_slice(parent, restore=False)


@pytest.mark.asyncio
async def test_duplicate_scope_never_restores_live_previous_even_after_release(callback_framework):
    ctx = await callback_context()
    with scopes.native_authority_source_scope(lambda: None, slice_source=fresh_slice):
        parent = scopes.begin_native_execution_slice(object())
        ancestor = scopes.current_native_execution_slice()
        try:
            await ctx.fire(AgentCallbackEvent.BEFORE_INVOKE)
            original = scopes.current_native_execution_slice()
            with pytest.raises(ResourceAccessDenied):
                await ctx.fire(AgentCallbackEvent.BEFORE_INVOKE)
            assert not original.active
            assert ancestor.active  # A failed child does not revoke its owner.
            assert scopes.current_native_execution_slice() is not ancestor
            assert not scopes.current_native_execution_slice().active
            await ctx.fire(AgentCallbackEvent.AFTER_INVOKE)
            assert not scopes.current_native_execution_slice().active
        finally:
            scopes.end_native_execution_slice(parent, restore=False)


@pytest.mark.asyncio
@pytest.mark.parametrize('phase', ['before', 'after'])
@pytest.mark.parametrize('failure', ['abort', 'cancel', 'error_hook_cancel'])
async def test_owner_task_expiry_denies_inherited_work_when_after_is_skipped(callback_framework, phase, failure):
    ctx = await callback_context()
    event = (AgentCallbackEvent.BEFORE_INVOKE if phase == 'before' else AgentCallbackEvent.AFTER_INVOKE)
    children, installed = [], []
    release = asyncio.Event()

    async def inherited_work():
        await release.wait()
        return scopes.current_native_execution_slice().active

    async def fail(_):
        installed.append(scopes.current_native_execution_slice())
        children.append(asyncio.create_task(inherited_work()))
        if failure == 'abort':
            raise AbortError('fixture abort', cause=ResourceAccessDenied('fixture denied'))
        if failure == 'cancel':
            raise asyncio.CancelledError()
        raise RuntimeError('fixture ordinary callback failure')

    async def cancel_error_hook(*_):
        raise asyncio.CancelledError()

    await ctx.agent.agent_callback_manager.register_callback(event, fail, priority=50)
    if failure == 'error_hook_cancel':
        callback_framework.add_hook(
            ctx.agent.agent_callback_manager._get_agent_event(event), HookType.ERROR, cancel_error_hook,
        )

    async def owner():
        with scopes.native_authority_source_scope(lambda: None, slice_source=fresh_slice):
            await ctx.fire(AgentCallbackEvent.BEFORE_INVOKE)
            await ctx.fire(AgentCallbackEvent.AFTER_INVOKE)

    task = asyncio.create_task(owner())
    try:
        with pytest.raises(ResourceAccessDenied if failure == 'abort' else asyncio.CancelledError):
            await task
        assert '_native_execution_slice' in ctx.extra  # Release did not run.
        release.set()
        assert await children[0] is False
        assert installed[0].active is False
    finally:
        release.set()
        await asyncio.gather(*children, return_exceptions=True)
        NativeExecutionScopeRail._end(ctx)


@pytest.mark.asyncio
async def test_pending_owner_cancellation_denies_before_task_completion(callback_framework):
    ready, cancellation_seen, finish, check = (asyncio.Event() for _ in range(4))
    children = []

    async def inherited_work():
        await check.wait()
        return scopes.current_native_execution_slice().active

    async def owner():
        ctx = await callback_context()
        with scopes.native_authority_source_scope(lambda: None, slice_source=fresh_slice):
            await ctx.fire(AgentCallbackEvent.BEFORE_INVOKE)
            children.append(asyncio.create_task(inherited_work()))
            ready.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancellation_seen.set()
                await finish.wait()  # Existing cleanup has not completed yet.

    task = asyncio.create_task(owner())
    try:
        await ready.wait()
        task.cancel()
        await cancellation_seen.wait()
        assert not task.done()
        check.set()
        assert await children[0] is False
    finally:
        finish.set()
        await task


@pytest.mark.asyncio
async def test_normal_nested_and_foreign_task_release_do_not_reset_another_scope(callback_framework):
    with scopes.native_authority_source_scope(lambda: None, slice_source=fresh_slice):
        outer = scopes.begin_native_execution_slice(object())
        parent = scopes.current_native_execution_slice()
        inner = scopes.begin_native_execution_slice(object())
        child = scopes.current_native_execution_slice()
        scopes.end_native_execution_slice(inner)
        scopes.end_native_execution_slice(inner)
        assert scopes.current_native_execution_slice() is parent and parent.active
        assert not child.active

        async def foreign_close():
            scopes.end_native_execution_slice(outer)

        await asyncio.create_task(foreign_close())
        assert scopes.current_native_execution_slice() is parent and not parent.active
        scopes.end_native_execution_slice(outer)
        assert scopes.current_native_execution_slice() is None
