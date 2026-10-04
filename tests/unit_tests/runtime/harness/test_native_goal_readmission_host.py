"""Fresh host admission + real Native/Goal/TaskLoop, synthetic model transport.

Core 3e935631 source overlay is required. No real Provider negative probes.
"""

import asyncio
from dataclasses import replace

import pytest

from openjiuwen.harness.goal.schema import GoalRecord
from openjiuwen.harness.schema.interaction import SendInputRequest
from openjiuwen.harness_protocol import TurnEventKind
from jiuwenswarm.governance.tool_context import tool_authority_scope
from tests.unit_tests.runtime.harness import (
    test_native_goal_source as goal_source_tests,
)

from tests.unit_tests.runtime.harness.test_native_request_origin import _Admission

goal_case = goal_source_tests.goal_case


def persisted(c):
    record = GoalRecord.create(
        session_id=c.session.get_session_id(), objective="saved", max_attempts=1
    )
    c.outer.goal_manager._store.save(record)
    assert c.outer.goal_manager._execution_origin is None
    return record


async def submit(
    c, record, *, action="attach", previous=None, owner=None, checker=None
):
    owner = owner or _Admission()
    request = SendInputRequest(
        "fresh-" + str(id(owner)), {"query": "resume", "nested": {"value": 1}}
    )
    seen = []

    def factory(native, actual):
        assert native is c.native
        seen.append(actual)
        return owner.lifecycle

    bundle = replace(c.bundle, native_lifecycle_factory=factory)
    with tool_authority_scope(None, provider_authorizers=bundle):
        receipt, result = await c.native.submit_goal_readmission(
            request=request,
            action=action,
            expected_record=record,
            previous=previous,
            check_current=checker or owner.check,
        )
    control = await asyncio.wait_for(result, 4)
    await asyncio.wait_for(owner.bound[0]._entry.terminal_event.wait(), 4)
    assert owner.terminal == [(owner.bound[0], TurnEventKind.FINISHED)]
    assert seen[0] is owner.bound[0]._entry.request and seen[0] is not request
    with pytest.raises(TypeError):
        seen[0].inputs["nested"]["value"] = 2
    return owner, control, receipt


@pytest.mark.asyncio
async def test_cold_attach_real_goal_uses_fresh_source_and_original_consumers(
    goal_case,
):
    c = goal_case
    record = persisted(c)
    owner, control, receipt = await submit(c, record)
    assert c.error is None and c.side_effects == ["goal"] and len(c.http) == 2
    assert control["goal"]["goal_id"] == record.goal_id
    assert owner.bound[0]._pending._origin.host_value is owner
    assert owner.bound[0].turn_id == receipt.turn_id
    assert c.native._requests == {} and owner.bound[0]._pending._exit.confirmed.done()


@pytest.mark.asyncio
async def test_hot_resume_uses_explicit_retained_pending_and_new_resources(goal_case):
    c = goal_case
    c.attempts = 2

    async def pause_first(_):
        if not c.side_effects:
            await c.outer.goal_manager.pause()

    c.before = pause_first
    record = persisted(c)
    record.max_attempts = 3
    c.outer.goal_manager._store.save(record)
    first, _, _ = await submit(c, record)
    old = first.bound[0]
    assert (
        c.native._requests == {}
    )  # only Runtime's retained receipt can identify old Pending
    current = c.outer.goal_manager.peek()
    assert current.status.value == "paused"
    # Old credential can expire: previous is proof of exit, never new authority.
    first.live = False
    second, _, _ = await submit(c, current, action="resume", previous=old)
    assert second.source is not first.source
    assert second.bound[0]._pending._origin is not old._pending._origin
    assert c.side_effects == ["goal", "goal"] and c.error is None


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["binding", "manager", "owner", "source"])
async def test_checker_reentry_cannot_retarget_new_admission(goal_case, mutation):
    c = goal_case
    record = persisted(c)
    owner = _Admission()
    old_binding, old_manager, old_owner = (
        c.native.engine.binding,
        c.outer.goal_manager,
        c.native._tool_owner,
    )

    def checker():
        if mutation == "binding":
            object.__setattr__(c.native.engine, "binding", replace(old_binding))
        elif mutation == "manager":
            c.outer.goal_manager = object()
        elif mutation == "owner":
            c.native._tool_owner = tuple(list(old_owner))
        else:
            owner.live = False

    try:
        with pytest.raises(PermissionError):
            await submit(c, record, owner=owner, checker=checker)
        assert not c.side_effects and not c.native._requests
    finally:
        object.__setattr__(c.native.engine, "binding", old_binding)
        c.outer.goal_manager = old_manager
        c.native._tool_owner = old_owner


@pytest.mark.asyncio
async def test_missing_new_lifecycle_factory_fails_without_old_source_fallback(
    goal_case,
):
    c = goal_case
    record = persisted(c)
    with pytest.raises(PermissionError):
        await c.native.submit_goal_readmission(
            request=SendInputRequest("new", {"query": ""}),
            action="attach",
            expected_record=record,
            previous=None,
            check_current=lambda: None,
        )
    assert not c.side_effects and not c.native._requests


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change", ["same_producer", "same_host_value", "unconfirmed", "foreign_native"]
)
async def test_original_exit_and_fresh_producer_are_both_required(goal_case, change):
    from openjiuwen.core.controller.schema.execution_origin import ExecutionOrigin

    c = goal_case
    c.attempts = 2

    async def pause_first(_):
        if not c.side_effects:
            await c.outer.goal_manager.pause()

    c.before = pause_first
    record = persisted(c)
    record.max_attempts = 3
    c.outer.goal_manager._store.save(record)
    first, _, _ = await submit(c, record)
    old = first.bound[0]
    current = c.outer.goal_manager.peek()
    owner = _Admission()
    confirmed = old._pending._exit.confirmed
    previous = old
    if change == "same_producer":
        owner = first
    elif change == "same_host_value":
        owner.source = ExecutionOrigin(first, _checker=owner.check)
        owner.lifecycle = replace(owner.lifecycle, source=owner.source)
    elif change == "unconfirmed":
        old._pending._exit.confirmed = asyncio.get_running_loop().create_future()
    else:
        previous = replace(old, _native=object())
    try:
        with pytest.raises(PermissionError):
            await submit(c, current, action="resume", previous=previous, owner=owner)
        assert c.side_effects == ["goal"] and c.native._requests == {}
    finally:
        old._pending._exit.confirmed = confirmed


@pytest.mark.asyncio
async def test_original_frozen_request_does_not_adopt_mutation_in_factory(goal_case):
    c = goal_case
    record = persisted(c)
    owner = _Admission()
    request = SendInputRequest("fresh", {"query": "original", "nested": {"value": 1}})
    captured = []

    def factory(native, actual):
        captured.append(actual)
        request.inputs["nested"]["value"] = 9
        return owner.lifecycle

    bundle = replace(c.bundle, native_lifecycle_factory=factory)
    with tool_authority_scope(None, provider_authorizers=bundle):
        _, result = await c.native.submit_goal_readmission(
            request=request,
            action="attach",
            expected_record=record,
            previous=None,
            check_current=owner.check,
        )
    await asyncio.wait_for(result, 4)
    await asyncio.wait_for(owner.bound[0]._entry.terminal_event.wait(), 4)
    assert captured[0].inputs["nested"]["value"] == 1
    assert captured[0] is owner.bound[0]._entry.request
    assert c.side_effects == ["goal"]


@pytest.mark.asyncio
async def test_removed_request_entry_notifies_only_original_non_admission(goal_case):
    c = goal_case
    record = persisted(c)
    owner = _Admission()

    def checker():
        c.native._requests.clear()

    with pytest.raises(PermissionError):
        await submit(c, record, owner=owner, checker=checker)
    assert owner.rejected == [True] and owner.bound == []
    assert c.native._requests == {} and c.side_effects == []


@pytest.mark.asyncio
async def test_checker_cannot_replace_new_lifecycle_source(goal_case):
    from openjiuwen.core.controller.schema.execution_origin import ExecutionOrigin

    c = goal_case
    record = persisted(c)
    owner = _Admission()
    old = owner.lifecycle.source

    def checker():
        object.__setattr__(owner.lifecycle, "source", ExecutionOrigin(object()))

    try:
        with pytest.raises(PermissionError):
            await submit(c, record, owner=owner, checker=checker)
        assert owner.rejected == [True] and not c.side_effects
    finally:
        object.__setattr__(owner.lifecycle, "source", old)
