# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Goal capability driven only by the existing Runtime stream producer."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace

from openjiuwen.harness.goal import (
    GoalAttemptDriver,
    GoalEvaluator,
    GoalManager,
    GoalStatus,
    SessionGoalStore,
)
from openjiuwen.harness.prompts.sections.goal import (
    build_goal_protocol_section,
    build_goal_task_query,
)
from openjiuwen.harness.tools.goal import GoalReportSink
from openjiuwen.harness_protocol import TurnEventKind

from jiuwenswarm.common.schema.agent import AgentResponseChunk
from jiuwenswarm.runtime.harness.goal_assessment import GoalTranscriptAssessor
from jiuwenswarm.runtime.harness.goal_evidence import (
    GoalAttemptEvidence,
    GoalAttemptIdentity,
)
from jiuwenswarm.runtime.harness.goal_tools import GoalReportScope, build_goal_tools
from jiuwenswarm.runtime.harness.tool_gateway import ProductToolScope
from jiuwenswarm.server.runtime.agent_adapter.goal_control import dispatch_goal_control
from jiuwenswarm.server.runtime.agent_adapter.goal_history import (
    flush_goal_set,
    record_goal_completed,
    record_goal_set,
)

_OWNER_KEY = "harness.goal.external_attempt"


class ExternalGoalRuntime:
    """One Manager and port on the parent's existing Session state.

    This object has no task, queue or event cursor. ``stream`` executes in the
    Runtime GOAL_STREAM producer; synchronous port methods only retain intent.
    """

    def __init__(self, adapter, parent_session, scope: ProductToolScope):
        self.adapter = adapter
        self.parent_session = parent_session
        self.scope = scope
        self.owner = None
        self.owner_done = asyncio.Event()
        self.owner_done.set()
        self.notification = None
        self.runtime = None
        self.attempt: GoalAttemptEvidence | None = None
        self.report_scope: GoalReportScope | None = None
        self.attempt_session = None
        self.revoked = False
        self.pending = None
        marker = parent_session.get_state(_OWNER_KEY)
        self.cold_unconfirmed = bool(marker)
        self.accounting_unknown = isinstance(marker, dict) and bool(
            marker.get("accounting_unknown")
        )
        self.assessor_factory = None
        self.sink = GoalReportSink()
        self.evaluator = GoalEvaluator()
        self.manager = GoalManager(
            store=SessionGoalStore(parent_session),
            control_lock=asyncio.Lock(),
            execution=self,
        )
        self.driver = GoalAttemptDriver(self.manager, self.evaluator)
        restored = self.manager.peek()
        if (
            restored is not None
            and restored.attempt_count > restored.last_assessed_attempt
        ):
            safe = (
                isinstance(marker, dict)
                and marker.get("safe_boundary") in {"exit_confirmed", "finished"}
                and (
                    marker.get("goal_id"),
                    marker.get("revision"),
                    marker.get("attempt_index"),
                )
                == (restored.goal_id, restored.revision, restored.attempt_count)
            )
            self.cold_unconfirmed = not safe or self.accounting_unknown

    def release_owner(self, owner):
        if self.owner is owner:
            self.owner = self.runtime = None
            self.owner_done.set()

    def tools(self):
        return build_goal_tools(
            self.manager,
            self.sink,
            scope=self.scope,
            parent_session=self.parent_session,
            current_attempt=lambda: self.report_scope if self.current() else None,
        )

    def is_available(self):
        return (
            self.owner is not None
            and not self.owner.cancellation_requested
            and not self.cold_unconfirmed
            and not self.accounting_unknown
            and not self.parent_session.state_write_failed
        )

    def ensure_work(self, record):
        self.pending = (record.goal_id, record.revision)
        return self.is_available()

    def discard_work(self, *, session_id, goal_id):
        if self.pending and self.pending[0] == goal_id:
            self.pending = None

    def has_running_attempt(self, *, goal_id):
        return (
            self.attempt is not None
            and self.attempt.identity.goal_id == goal_id
            and not self.revoked
        )

    def goal_updated(self, record):
        # The existing producer projects its committed manager snapshot outside
        # the control lock. No callback acquires a Manager lock or sends input.
        self.notification = {
            "event_type": "goal.updated",
            "goal": record.to_dict() if record else None,
        }

    async def cancel_attempt(self, *, goal_id, reason):
        if self.attempt is None or (
            self.attempt.identity.goal_id != goal_id and not self.revoked
        ):
            return
        self.revoked = True
        self.report_scope = None
        if self.attempt_session is not None:
            # Does not wait for the producer: that producer may be settling on
            # this very Manager control lock. Failure retains the exact owner.
            await self.adapter._stop_owned_execution_once(self.attempt_session)
        self.parent_session.update_state({_OWNER_KEY: None})
        if self.owner is not None:
            await self.runtime.request_external_execution_cancel(self.owner)

    def current(self):
        if (
            self.attempt is None
            or self.owner is None
            or self.revoked
            or self.owner.cancellation_requested
        ):
            return False
        record = self.manager.peek()
        identity = self.attempt.identity
        return (
            record is not None
            and record.goal_id == identity.goal_id
            and record.revision == identity.revision
            and record.attempt_count == identity.attempt_index
        )

    def observe(self, event, *, source_session):
        if self.attempt is not None and source_session is self.attempt_session:
            self.attempt.observe(event, generation=self.attempt.identity.generation)

    def bind_turn(self, turn_id):
        self.attempt.bind_turn(turn_id)
        self.report_scope = replace(self.report_scope, turn_id=turn_id)
        self.parent_session.update_state(
            {
                _OWNER_KEY: {
                    **self.report_scope.arguments(),
                    "attempt_token": None,
                    "execution_id": self.owner.execution_id,
                    "generation": self.owner.generation,
                    "turn_id": turn_id,
                }
            }
        )

    async def _account_provider_usage(self, *, propagate_cancel=True):
        evidence = self.attempt
        if evidence is None:
            return
        usage = evidence.take_usage_delta()
        if usage is not None:
            await self._account_usage(
                evidence.identity, usage, propagate_cancel=propagate_cancel
            )

    async def _account_usage(self, identity, usage, *, propagate_cancel=True):
        if usage is not None:
            accounting = asyncio.create_task(
                self.manager.accumulate_usage(
                    goal_id=identity.goal_id,
                    revision=identity.revision,
                    attempt_index=identity.attempt_index,
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                    cached_input_tokens=usage.cached_input_tokens,
                )
            )
            cancelled = False
            while True:
                try:
                    await asyncio.shield(accounting)
                    break
                except asyncio.CancelledError:
                    cancelled = True
                    if accounting.cancelled():
                        raise
            if cancelled and propagate_cancel:
                raise asyncio.CancelledError

    async def control(self, operation):
        if self.accounting_unknown and operation.get("action") in {"set", "resume"}:
            return {
                "result_type": "goal_error",
                "action": operation["action"],
                "error_code": "goal_usage_unavailable",
                "error": "Prior Goal usage is incomplete; resuming cannot preserve accounting. Delete this Session and create a new Session to start again.",
                "goal": self.payload(),
            }
        if (
            self.cold_unconfirmed or self.parent_session.state_write_failed
        ) and operation.get("action") in {"set", "resume"}:
            return {
                "result_type": "goal_error",
                "action": operation["action"],
                "error_code": "goal_recovery_unconfirmed",
                "error": "Prior Goal execution exit is unconfirmed; automatic resend is disabled. Delete this Session and create a new Session to start again.",
                "goal": self.payload(),
            }
        return await dispatch_goal_control(self.manager, **operation)

    def payload(self):
        record = self.manager.peek()
        return record.to_dict() if record else None

    @staticmethod
    def chunk(request, payload, *, complete=False, status=None):
        return AgentResponseChunk(
            request_id=request.request_id,
            channel_id=request.channel_id,
            payload=payload,
            metadata=dict(request.metadata or {}),
            is_complete=complete,
            runtime_completion=status,
        )

    async def stream(self, request, inputs, *, runtime, owner, operation):
        assessor_factory = getattr(
            request, "_goal_assessor_factory", self.assessor_factory
        )
        stored = self.manager.peek()
        idle_attach = (
            operation is None
            and stored is not None
            and stored.status is GoalStatus.ACTIVE
            and not self.cold_unconfirmed
            and not self.accounting_unknown
            and not self.parent_session.state_write_failed
        )
        mine = self.owner is None and (
            idle_attach
            or (operation is not None and operation.get("action") in {"set", "resume"})
        )
        owned_goal_id = stored.goal_id if idle_attach else None
        if mine:
            self.owner, self.runtime = owner, runtime
            self.owner_done.clear()
        try:
            if operation is not None:
                result = await self.control(operation)
                result_goal = result.get("goal")
                owned_goal_id = (
                    result_goal.get("goal_id")
                    if isinstance(result_goal, dict)
                    else None
                )
                await record_goal_set(
                    request,
                    action=result.get("action"),
                    result_type=result.get("result_type"),
                    goal_payload=result.get("goal"),
                    defer=True,
                )
                if result.get("result_type") in {"goal_error", "goal_confirm_required"}:
                    if result["result_type"] == "goal_confirm_required":
                        payload = {
                            "event_type": "goal.confirm_required",
                            "existing_goal": result.get("existing_goal"),
                            "requested_objective": result.get("requested_objective"),
                        }
                    else:
                        payload = {
                            "event_type": "chat.error",
                            "code": result.get("error_code"),
                            "error": result.get("error"),
                            "message": result.get("error"),
                        }
                    yield self.chunk(request, payload, complete=True)
                    return
            yield self.chunk(
                request,
                {
                    "event_type": "goal.snapshot",
                    "goal": self.payload(),
                    **(
                        {
                            "recovery_error": "goal_usage_unavailable"
                            if self.accounting_unknown
                            else "goal_recovery_unconfirmed"
                        }
                        if self.cold_unconfirmed
                        or self.accounting_unknown
                        or self.parent_session.state_write_failed
                        else {}
                    ),
                },
            )
            if (
                not mine
                and operation is not None
                and (
                    operation.get("action") == "set"
                    or (operation.get("action") == "resume" and self.attempt is None)
                )
            ):
                await self.owner_done.wait()
                current = self.manager.peek()
                if (
                    self.owner is None
                    and current is not None
                    and current.goal_id == owned_goal_id
                ):
                    mine = True
                    self.owner, self.runtime = owner, runtime
                    self.owner_done.clear()
            if not mine or self.cold_unconfirmed:
                return
            while self.is_available():
                record = self.manager.peek()
                if (
                    record is None
                    or record.goal_id != owned_goal_id
                    or record.status is not GoalStatus.ACTIVE
                ):
                    break
                await runtime.acquire_external_execution(owner, goal=True)
                continue_attempts = False
                assessor_pending = False
                assessor_usage_unknown = False
                try:
                    record = self.manager.peek()
                    if (
                        record is None
                        or record.goal_id != owned_goal_id
                        or record.status is not GoalStatus.ACTIVE
                    ):
                        break
                    record = await self.driver.begin(
                        goal_id=record.goal_id,
                        revision=record.revision,
                        attempt_index=record.attempt_count + 1,
                    )
                    if record is None:
                        continue
                    identity = GoalAttemptIdentity(
                        record.goal_id,
                        record.revision,
                        record.attempt_count,
                        owner.execution_id,
                        owner.generation,
                    )
                    self.revoked = False
                    context = self.adapter._external_context()
                    self.attempt = GoalAttemptEvidence(
                        identity,
                        host_session_id=owner.session_id,
                        agent_id=context.agent_id,
                    )
                    self.report_scope = GoalReportScope(identity, self.scope)
                    self.sink.begin_attempt(
                        owner.session_id,
                        identity.goal_id,
                        identity.revision,
                        identity.attempt_index,
                    )
                    self.parent_session.update_state(
                        {
                            _OWNER_KEY: {
                                "goal_id": identity.goal_id,
                                "revision": identity.revision,
                                "attempt_index": identity.attempt_index,
                                "generation": owner.generation,
                                "execution_id": owner.execution_id,
                                "turn_id": None,
                            }
                        }
                    )
                    self.attempt_session = self.adapter._require_session()
                    await flush_goal_set(owner.session_id)
                    query = (
                        build_goal_protocol_section("cn").content["cn"]
                        + "\n"
                        + build_goal_task_query(record)
                        + "\nsubmit_goal_report must include this exact attempt identity:\n"
                        + json.dumps(self.report_scope.arguments(), ensure_ascii=False)
                    )
                    failure = None
                    try:
                        async for chunk in self.adapter._process_message_stream_impl(
                            request,
                            {**inputs, "query": query},
                            goal_attempt=self,
                        ):
                            yield chunk
                            if self.notification is not None:
                                notification, self.notification = (
                                    {
                                        "event_type": "goal.updated",
                                        "goal": self.payload(),
                                    },
                                    None,
                                )
                                yield self.chunk(request, notification)
                    except Exception as exc:
                        failure = f"{type(exc).__name__}: {exc}"
                    evidence = self.attempt
                    self.report_scope = None
                    terminal = evidence.terminal
                    finished = (
                        terminal is not None and terminal.kind is TurnEventKind.FINISHED
                    )
                    if not finished:
                        await self.adapter._stop_heartbeat_execution(
                            self.attempt_session
                        )
                        failure = (
                            failure
                            or "Goal Provider did not confirm successful terminal"
                        )
                    await self._account_provider_usage()
                    if self.revoked or not self.current():
                        if evidence.turn_id:
                            await self.adapter._projection.finish_goal_turn(
                                evidence.turn_id
                            )
                        payload = {
                            "event_type": "chat.final",
                            "content": "",
                            "terminal_status": "cancelled",
                        }
                        yield self.chunk(
                            request, payload, complete=True, status="cancelled"
                        )
                        break
                    if terminal is not None and terminal.kind is TurnEventKind.ABORTED:
                        paused = await self.manager.pause()
                        yield self.chunk(
                            request,
                            {
                                "event_type": "goal.updated",
                                "goal": paused.to_dict() if paused else None,
                            },
                        )
                        payload = {
                            "event_type": "chat.final",
                            "content": "",
                            "terminal_status": "cancelled",
                        }
                        if evidence.turn_id:
                            await self.adapter._projection.finish_goal_turn(
                                evidence.turn_id, terminal_payload=payload
                            )
                        yield self.chunk(
                            request, payload, complete=True, status="cancelled"
                        )
                        break
                    usage = None
                    record = self.manager.peek()
                    report = self.sink.consume()
                    assessment = None
                    if not evidence.usage_complete:
                        self.accounting_unknown = True
                        failure = "goal_usage_unavailable: Provider input/output usage is incomplete"
                    if evidence.error:
                        failure = evidence.error
                    if failure is None:
                        assessor = GoalTranscriptAssessor(
                            self.evaluator, model_factory=assessor_factory
                        )
                        captured = []
                        try:
                            assessor_pending = True
                            assessment = await assessor.maybe_assess(
                                record,
                                report,
                                evidence.transcript,
                                is_current=self.current,
                                on_usage=captured.append,
                            )
                            assessor_pending = False
                        finally:
                            if captured:
                                assessor_pending = False
                                assessor_usage_unknown = not captured[
                                    -1
                                ].usage_available
                                await self._account_usage(identity, captured[-1].usage)
                        if not assessment.usage_available:
                            self.accounting_unknown = True
                        if assessment.error_code or not assessment.usage_available:
                            failure = (
                                assessment.error_code
                                or "goal_usage_unavailable: assessor usage is incomplete"
                            )
                    settled = await self.driver.finish(
                        goal_id=identity.goal_id,
                        revision=identity.revision,
                        attempt_index=identity.attempt_index,
                        outcome="failed" if failure else "completed",
                        agent_report=report,
                        transcript_response=assessment.transcript_response
                        if assessment
                        else None,
                        execution_error=failure,
                        usage=usage,
                    )
                    if not self.accounting_unknown:
                        self.parent_session.update_state({_OWNER_KEY: None})
                    if settled is not None:
                        yield self.chunk(
                            request,
                            {"event_type": "goal.updated", "goal": settled.to_dict()},
                        )
                        await record_goal_completed(
                            session_id=owner.session_id,
                            channel_id=request.channel_id,
                            channel_metadata=request.metadata,
                            mode=(request.params or {}).get("mode", "unknown"),
                            goal_payload=settled.to_dict(),
                        )
                    final = settled is None or settled.status is not GoalStatus.ACTIVE
                    status = (
                        "completed"
                        if settled is not None
                        and settled.status is GoalStatus.COMPLETED
                        else "cancelled"
                        if settled is None or settled.status is GoalStatus.PAUSED
                        else "failed"
                    )
                    payload = {
                        "event_type": "chat.final",
                        "content": "",
                        "terminal_status": status,
                    }
                    projected = (
                        await self.adapter._projection.finish_goal_turn(
                            evidence.turn_id,
                            terminal_payload=payload if final else None,
                        )
                        if evidence.turn_id
                        else None
                    )
                    continue_attempts = not final
                    if final:
                        yield self.chunk(
                            request, projected or payload, complete=True, status=status
                        )
                        break
                finally:
                    # Cancellation may land during send, output, assessment or
                    # control replacement. Never release the Runtime permit
                    # while its captured Provider can still execute.
                    if self.attempt_session is not None and (
                        self.attempt is None
                        or self.attempt.terminal is None
                        or self.attempt.terminal.kind is not TurnEventKind.FINISHED
                        or self.revoked
                    ):
                        await self.adapter._stop_heartbeat_execution(
                            self.attempt_session
                        )
                    await self._account_provider_usage(propagate_cancel=False)
                    if self.attempt is not None and self.attempt.turn_id:
                        await self.adapter._projection.finish_goal_turn(
                            self.attempt.turn_id
                        )
                    current = self.manager.peek()
                    evidence = self.attempt
                    if evidence is not None and (
                        not evidence.usage_complete
                        or assessor_pending
                        or assessor_usage_unknown
                    ):
                        self.accounting_unknown = True
                    if (
                        evidence is not None
                        and self.accounting_unknown
                        and not self.parent_session.state_write_failed
                    ):
                        self.parent_session.update_state(
                            {
                                _OWNER_KEY: {
                                    "goal_id": evidence.identity.goal_id,
                                    "revision": evidence.identity.revision,
                                    "attempt_index": evidence.identity.attempt_index,
                                    "accounting_unknown": True,
                                }
                            }
                        )
                    elif (
                        not self.parent_session.state_write_failed
                        and evidence is not None
                        and current is not None
                        and (current.goal_id, current.revision)
                        == (evidence.identity.goal_id, evidence.identity.revision)
                        and current.last_assessed_attempt
                        < evidence.identity.attempt_index
                    ):
                        self.accounting_unknown = (
                            not evidence.usage_complete
                            or assessor_pending
                            or assessor_usage_unknown
                        )
                        self.parent_session.update_state(
                            {
                                _OWNER_KEY: {
                                    "goal_id": evidence.identity.goal_id,
                                    "revision": evidence.identity.revision,
                                    "attempt_index": evidence.identity.attempt_index,
                                    **(
                                        {"accounting_unknown": True}
                                        if self.accounting_unknown
                                        else {
                                            "safe_boundary": "finished"
                                            if evidence.terminal is not None
                                            and evidence.terminal.kind
                                            is TurnEventKind.FINISHED
                                            else "exit_confirmed"
                                        }
                                    ),
                                }
                            }
                        )
                    self.report_scope = None
                    self.attempt = None
                    self.attempt_session = None
                    if continue_attempts:
                        runtime.release_external_execution(owner)
                    else:
                        self.adapter._release_external_owner(runtime, owner, request)
        finally:
            if mine:
                # Reader loss is a conservative pause, never an implicit new
                # attempt. Explicit stop confirmation preserves the owner on
                # failure and clears Provider interactions before releasing it.
                session = self.attempt_session
                if session is not None:
                    await self.adapter._stop_heartbeat_execution(session)
                self.report_scope = None
                self.attempt = None
                self.attempt_session = None
                current = self.manager.peek()
                if (
                    not self.parent_session.state_write_failed
                    and current is not None
                    and current.goal_id == owned_goal_id
                    and current.status is GoalStatus.ACTIVE
                ):
                    await self.manager.pause()
                self.adapter._release_external_owner(runtime, owner, request, goal=self)
