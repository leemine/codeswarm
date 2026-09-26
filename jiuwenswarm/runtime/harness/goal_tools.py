# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Scope existing Goal tools to a host-authorized External attempt."""

from __future__ import annotations

import copy
import hmac
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from openjiuwen.harness.execution_subject import current_execution_subject
from openjiuwen.harness.tools.goal import (
    GetCurrentGoalTool,
    GoalReportSink,
    SubmitGoalReportTool,
)

from jiuwenswarm.runtime.harness.goal_evidence import GoalAttemptIdentity
from jiuwenswarm.runtime.harness.tool_gateway import ProductToolScope


@dataclass(frozen=True, slots=True)
class GoalReportScope:
    """Ephemeral capability delivered only to its bound root Goal attempt."""

    identity: GoalAttemptIdentity
    scope: ProductToolScope
    token: str = field(default_factory=lambda: secrets.token_urlsafe(24))
    turn_id: str | None = None

    def arguments(self) -> dict[str, Any]:
        return {
            "goal_id": self.identity.goal_id,
            "revision": self.identity.revision,
            "attempt_index": self.identity.attempt_index,
            "attempt_token": self.token,
        }


class _PeekGoalReader:
    """Avoid a control-lock wait from an in-flight Provider tool callback."""

    def __init__(self, manager: Any) -> None:
        self._manager = manager

    async def get(self):
        return self._manager.peek()


class _ScopedGoalTool:
    def __init__(self, delegate, *, scope, parent_session) -> None:
        self._delegate = delegate
        self._scope = scope
        self._parent_session = parent_session
        self.card = copy.deepcopy(delegate.card)

    def _check_parent(self, kwargs: Mapping[str, Any]) -> None:
        subject = current_execution_subject()
        if (
            kwargs.get("session") is not self._parent_session
            or self._parent_session.get_session_id() != self._scope.host_session_id
            or subject is None
            or subject.subject_id != self._scope.subject_id
            or subject.session_id != self._scope.host_session_id
            or subject.parent_subject_id
            or subject.kind != "agent"
        ):
            raise ValueError("Goal tool requires its admitted parent Session")

    def render_for_llm(self, output):
        return self._delegate.render_for_llm(output)


class _ReadGoalTool(_ScopedGoalTool):
    async def invoke(self, inputs, **kwargs):
        self._check_parent(kwargs)
        if not isinstance(inputs, Mapping) or inputs:
            raise ValueError("get_current_goal takes no routing arguments")
        return await self._delegate.invoke({}, **kwargs)


class _ReportGoalTool(_ScopedGoalTool):
    def __init__(self, delegate, *, scope, parent_session, sink, current_attempt):
        super().__init__(delegate, scope=scope, parent_session=parent_session)
        self._sink = sink
        self._current_attempt = current_attempt
        schema = copy.deepcopy(self.card.input_params)
        identity_fields = {
            "goal_id": {"type": "string"},
            "revision": {"type": "integer"},
            "attempt_index": {"type": "integer", "minimum": 1},
            "attempt_token": {"type": "string"},
        }
        schema["properties"].update(identity_fields)
        schema["required"] = [*schema.get("required", []), *identity_fields]
        schema["additionalProperties"] = False
        self.card.input_params = schema

    async def invoke(self, inputs, **kwargs):
        self._check_parent(kwargs)
        current = self._current_attempt()
        if current is None or current.scope != self._scope or not current.turn_id:
            raise ValueError("No bound Goal attempt accepts a report")
        if not isinstance(inputs, Mapping):
            raise ValueError("Goal report must be an object")
        allowed = set(self.card.input_params["properties"])
        if set(inputs) - allowed:
            raise ValueError("Goal report cannot override routing scope")
        expected = current.arguments()
        if (
            not isinstance(inputs.get("attempt_token"), str)
            or not hmac.compare_digest(inputs["attempt_token"], current.token)
            or any(
                inputs.get(key) != value
                for key, value in expected.items()
                if key != "attempt_token"
            )
            or any(
                type(inputs.get(key)) is not int
                for key in ("revision", "attempt_index")
            )
        ):
            raise ValueError("Goal report is stale or belongs to another attempt")
        identity = current.identity
        if (
            self._sink.goal_id != identity.goal_id
            or self._sink.revision != identity.revision
            or self._sink.attempt_index != identity.attempt_index
        ):
            raise ValueError("Goal report sink does not match its attempt")
        # Keep original report normalization and GoalAssessment construction.
        # The sink's original within-attempt last-write-wins contract remains.
        report = {key: value for key, value in inputs.items() if key not in expected}
        if not isinstance(report.get("status"), str) or not isinstance(
            report.get("evidence"), str
        ):
            raise ValueError("Goal report requires string status and evidence")
        for key in ("remaining_work", "next_instruction"):
            if report.get(key) is not None and not isinstance(report[key], str):
                raise ValueError("Goal report optional fields must be strings")
        return await self._delegate.invoke(report, **kwargs)


def build_goal_tools(
    manager,
    sink: GoalReportSink,
    *,
    scope: ProductToolScope,
    parent_session: Any,
    current_attempt: Callable[[], GoalReportScope | None],
    language: str = "cn",
) -> list:
    """Compose tools into the existing gateway; never create a sink or store.

    ``current_attempt`` is a synchronous host ownership check. It must return
    None for ordinary turns, retired generations, or attempts being assessed.
    Bind the returned tools only into the parent gateway, never into children.
    The existing gateway supplies the actual ExecutionSubject and parent Session.
    """
    if parent_session.get_session_id() != scope.host_session_id:
        raise ValueError("Goal tools parent Session does not match scope")
    return [
        _ReadGoalTool(
            GetCurrentGoalTool(_PeekGoalReader(manager), language),
            scope=scope,
            parent_session=parent_session,
        ),
        _ReportGoalTool(
            SubmitGoalReportTool(sink, language),
            scope=scope,
            parent_session=parent_session,
            sink=sink,
            current_attempt=current_attempt,
        ),
    ]
