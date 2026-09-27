# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""The original Goal tools composed into the original product gateway."""

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest
from openjiuwen.harness.execution_subject import (
    ExecutionSubject,
    execution_subject_scope,
)
from openjiuwen.harness.goal.manager import GoalManager
from openjiuwen.harness.goal.schema import GoalAssessmentStatus, GoalRecord
from openjiuwen.harness.tools.goal import GoalReportSink, SubmitGoalReportTool
from openjiuwen.harness_protocol import ToolInvocation

from jiuwenswarm.runtime.harness.goal_evidence import GoalAttemptIdentity
from jiuwenswarm.runtime.harness.goal_tools import GoalReportScope, build_goal_tools
from jiuwenswarm.runtime.harness.tool_gateway import (
    ProductToolGateway,
    ProductToolScope,
)


@pytest.fixture
def setup(tmp_path):
    scope = ProductToolScope("agent", "host", str(tmp_path))
    parent = SimpleNamespace(get_session_id=lambda: "host")
    manager = SimpleNamespace(peek=lambda: None)
    sink = GoalReportSink()
    sink.begin_attempt("host", "goal", 2, 1)
    active = [
        GoalReportScope(
            GoalAttemptIdentity("goal", 2, 1, "owner", 7), scope, turn_id="turn"
        )
    ]
    tools = build_goal_tools(
        manager,
        sink,
        scope=scope,
        parent_session=parent,
        current_attempt=lambda: active[0],
        language="en",
    )
    gateway = ProductToolGateway(tools, scope=scope, invoke_kwargs={"session": parent})
    return SimpleNamespace(
        scope=scope,
        parent=parent,
        manager=manager,
        sink=sink,
        active=active,
        tools=tools,
        gateway=gateway,
    )


def report(setup, **changes):
    args = dict(
        setup.active[0].arguments(), status="complete", evidence="observed result"
    )
    args.update(changes)
    return ToolInvocation("call", "submit_goal_report", args)


@pytest.mark.asyncio
async def test_original_report_schema_normalization_sink_and_last_write(setup):
    original = SubmitGoalReportTool(GoalReportSink()).card.input_params
    definitions = await setup.gateway.definitions()
    schema = next(
        item.input_schema for item in definitions if item.name == "submit_goal_report"
    )
    assert set(original["required"]) <= set(schema["required"])
    assert set(setup.active[0].arguments()) <= set(schema["required"])
    accepted = await setup.gateway.invoke(report(setup))
    assert not accepted.is_error
    assert setup.sink.report.status == GoalAssessmentStatus.COMPLETE
    normalized = await setup.gateway.invoke(report(setup, status="unknown"))
    assert not normalized.is_error
    assert setup.sink.consume().status == GoalAssessmentStatus.CONTINUE
    assert setup.sink.report is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes",
    [
        {"attempt_token": "stale"},
        {"goal_id": "other"},
        {"revision": 3},
        {"attempt_index": 2},
        {"revision": True},
        {"attempt_index": True},
        {"subject_id": "other"},
        {"workspace": "/other"},
        {"status": None},
        {"evidence": {}},
        {"next_instruction": []},
    ],
)
async def test_report_rejects_wrong_identity_and_routing_arguments(setup, changes):
    result = await setup.gateway.invoke(report(setup, **changes))
    assert result.is_error
    assert setup.sink.report is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state", ["ordinary", "unbound", "workspace", "sink", "next_attempt"]
)
async def test_report_rejects_retired_unbound_cross_scope_or_replaced_attempt(
    setup, state
):
    invocation = report(setup)
    if state == "ordinary":
        setup.active[0] = None
    elif state == "unbound":
        setup.active[0] = replace(setup.active[0], turn_id=None)
    elif state == "workspace":
        setup.active[0] = replace(
            setup.active[0], scope=replace(setup.scope, workspace="/other")
        )
    elif state == "sink":
        setup.sink.begin_attempt("host", "goal", 2, 2)
    else:
        setup.active[0] = replace(setup.active[0], token="new-generation-token")
    assert (await setup.gateway.invoke(invocation)).is_error
    assert setup.sink.report is None


@pytest.mark.asyncio
async def test_report_rejects_child_and_different_parent_objects(setup):
    subject = ExecutionSubject(
        subject_id="agent",
        display_name="agent",
        kind="agent",
        session_id="host",
        parent_subject_id="parent",
    )
    with (
        execution_subject_scope(subject),
        pytest.raises(ValueError, match="admitted parent"),
    ):
        await setup.tools[1].invoke(report(setup).arguments, session=setup.parent)
    gateway = ProductToolGateway(
        setup.tools,
        scope=setup.scope,
        invoke_kwargs={"session": SimpleNamespace(get_session_id=lambda: "host")},
    )
    assert (await gateway.invoke(report(setup))).is_error
    assert setup.sink.report is None


@pytest.mark.asyncio
async def test_read_uses_original_renderer_without_waiting_for_manager_control_lock(
    setup,
):
    setup.active[0] = None
    result = await setup.gateway.invoke(ToolInvocation("read", "get_current_goal", {}))
    assert not result.is_error
    assert result.content == "No persistent goal is set for this session."
    assert (
        await setup.gateway.invoke(
            ToolInvocation("read", "get_current_goal", {"session_id": "other"})
        )
    ).is_error


def test_factory_rejects_wrong_parent_session(setup):
    with pytest.raises(ValueError, match="does not match"):
        build_goal_tools(
            setup.manager,
            setup.sink,
            scope=setup.scope,
            parent_session=SimpleNamespace(get_session_id=lambda: "other"),
            current_attempt=lambda: None,
        )


@pytest.mark.asyncio
async def test_read_can_finish_while_control_lock_waits_for_provider_stop(setup):
    record = GoalRecord.create(session_id="host", objective="retained objective")
    lock = asyncio.Lock()
    manager = GoalManager(
        store=SimpleNamespace(load=lambda: record),
        control_lock=lock,
        execution=SimpleNamespace(),
    )
    tools = build_goal_tools(
        manager,
        setup.sink,
        scope=setup.scope,
        parent_session=setup.parent,
        current_attempt=lambda: None,
        language="en",
    )
    gateway = ProductToolGateway(
        tools, scope=setup.scope, invoke_kwargs={"session": setup.parent}
    )
    async with lock:
        result = await asyncio.wait_for(
            gateway.invoke(ToolInvocation("read", "get_current_goal", {})), timeout=0.2
        )
    assert not result.is_error
    assert "retained objective" in result.content
