# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Team attempts in the original Runtime producer, using the existing Goal owner."""
from __future__ import annotations

import asyncio
from contextlib import aclosing

from openjiuwen.harness.goal import GoalStatus
from openjiuwen.harness.prompts.sections.goal import build_goal_task_query

from jiuwenswarm.runtime.harness.external_goal import ExternalGoalRuntime, _OWNER_KEY
from jiuwenswarm.runtime.harness.goal_assessment import GoalTranscriptAssessor
from jiuwenswarm.runtime.harness.goal_evidence import GoalAttemptIdentity
from jiuwenswarm.server.runtime.agent_adapter.goal_history import (
    flush_goal_set, record_goal_completed, record_goal_set,
)


class ExternalTeamGoalRuntime(ExternalGoalRuntime):
    """Reuse the Manager/control/store port; never the Single Turn execution loop.

    A Team Round remains owned until Runner exit, usage, assessment and durable
    settlement. Its stream terminal/idle is a candidate boundary, not success.
    """
    def __init__(self, adapter, parent_session, scope):
        super().__init__(adapter, parent_session, scope)
        self.identity = None
        self.request = None
        self.exit_confirmed = False
        self._exit_lock = asyncio.Lock()

    def bind_round(self, manager):
        if self.identity is None or self.owner is None or self.revoked:
            raise RuntimeError('Team Goal producer has no admitted attempt')
        self.attempt = manager.bind_goal_attempt_evidence(
            self.owner.session_id, self.owner.request_id,
            identity=self.identity, runtime=self.runtime,
        )

    def current(self):
        return (super().current() and self.runtime.holds_external_execution(self.owner)
                and self.adapter.team_manager.current_goal_attempt_evidence(self.owner.session_id) is self.attempt)

    async def confirm_exit(self):
        async with self._exit_lock:
            if not self.exit_confirmed:
                await self.adapter.confirm_goal_round_exit(self)
                self.exit_confirmed = True

    async def cancel_attempt(self, *, goal_id, reason):
        del reason
        if self.identity is None or self.identity.goal_id != goal_id:
            return
        self.revoked = True
        await self.confirm_exit()
        # Do not clear the durable marker before the producer accounts usage.
        await self.runtime.request_external_execution_cancel(self.owner)

    async def _cleanup_attempt(self, *, assessor_unknown):
        # Shield only original resource cleanup; retain the exact owner on any
        # failed exit confirmation. No next producer can take its permit.
        task = asyncio.create_task(self.confirm_exit())
        while True:
            try:
                await asyncio.shield(task)
                break
            except asyncio.CancelledError:
                if task.cancelled():
                    raise
        evidence = self.attempt
        if evidence is not None and not evidence.sealed:
            evidence.seal('cancelled')
        await self._account_provider_usage(propagate_cancel=False)
        self.accounting_unknown |= evidence is None or not evidence.usage_complete or assessor_unknown
        if self.identity is not None and not self.parent_session.state_write_failed:
            current = self.manager.peek()
            self.parent_session.update_state({_OWNER_KEY: (
                {**self._marker(), 'accounting_unknown': True}
                if self.accounting_unknown else
                {**self._marker(), 'safe_boundary': 'exit_confirmed'}
                if current is not None and current.goal_id == self.identity.goal_id
                and current.last_assessed_attempt < self.identity.attempt_index else None
            )})
        # Resource teardown is already confirmed; now retire the original Round.
        await self.adapter.team_manager.stop_session_runtime(
            self.owner.session_id, reason='Team Goal settled', require_exit_confirmation=True,
        )
        self.attempt = None
        self.identity = None

    def _marker(self):
        identity = self.identity
        return {'goal_id': identity.goal_id, 'revision': identity.revision,
                'attempt_index': identity.attempt_index, 'execution_id': identity.execution_id,
                'generation': identity.generation, 'topology': 'team'}

    async def stream(self, request, inputs, *, runtime, owner, operation):
        from jiuwenswarm.runtime.session.model import SessionWorkKind
        if (owner.session_id != self.scope.host_session_id or owner.request_id != request.request_id
                or owner.work_kind not in {SessionWorkKind.GOAL_STREAM, SessionWorkKind.GOAL_ATTACH}):
            raise ValueError('Team Goal requires its original Runtime Goal producer')
        if self.owner is not None:
            yield self.chunk(request, {'event_type': 'chat.error', 'code': 'goal_owner_busy',
                                       'error': 'The prior Team Goal producer has not released ownership.'}, complete=True)
            return
        self.owner, self.runtime, self.request = owner, runtime, request
        self.owner_done.clear()
        owned_goal_id = None
        try:
            if operation is not None:
                result = await self.control(operation)
                if result['result_type'] in {'goal_error', 'goal_confirm_required'}:
                    payload = ({'event_type': 'goal.confirm_required',
                                'existing_goal': result.get('existing_goal'),
                                'requested_objective': result.get('requested_objective')}
                               if result['result_type'] == 'goal_confirm_required' else
                               {'event_type': 'chat.error', 'code': result.get('error_code'),
                                'error': result.get('error')})
                    yield self.chunk(request, payload, complete=True)
                    return
                await record_goal_set(request, action=result.get('action'), result_type=result['result_type'],
                                      goal_payload=result.get('goal'), defer=True)
            yield self.chunk(request, {'event_type': 'goal.snapshot', 'goal': self.payload(),
                **({'recovery_error': 'goal_usage_unavailable' if self.accounting_unknown else 'goal_recovery_unconfirmed'}
                   if self.cold_unconfirmed or self.accounting_unknown or self.parent_session.state_write_failed else {})})
            stored = self.manager.peek()
            owned_goal_id = stored.goal_id if stored is not None else None
            while self.is_available():
                record = self.manager.peek()
                if record is None or record.goal_id != owned_goal_id or record.status is not GoalStatus.ACTIVE:
                    break
                await runtime.acquire_external_execution(owner, goal=True)
                pending_assessor = False
                assessor_unknown = False
                self.exit_confirmed = False
                try:
                    record = await self.driver.begin(goal_id=record.goal_id, revision=record.revision,
                                                     attempt_index=record.attempt_count + 1)
                    if record is None:
                        break
                    identity = self.identity = GoalAttemptIdentity(record.goal_id, record.revision,
                        record.attempt_count, owner.execution_id, owner.generation)
                    self.revoked = False
                    self.parent_session.update_state({_OWNER_KEY: self._marker()})
                    await flush_goal_set(owner.session_id)
                    boundary = False
                    failure = None
                    try:
                        async with aclosing(self.adapter.process_goal_round(request,
                                {**inputs, 'query': build_goal_task_query(record)}, self)) as stream:
                            async for chunk in stream:
                                payload = chunk.payload if isinstance(chunk.payload, dict) else {}
                                if payload.get('event_type') in {'chat.error', 'team.error'} or payload.get('terminal_status') in {'unknown', 'failed'}:
                                    failure = 'team_goal_round_failed'
                                if (payload.get('event_type') == 'chat.processing_status'
                                        and payload.get('is_processing') is False and payload.get('is_complete') is True):
                                    boundary = True
                                # The Team request ending is not the root Goal ending.
                                if not chunk.is_complete:
                                    yield chunk
                                if failure is not None:
                                    # A failed reviewer may leave the original task
                                    # IN_REVIEW, so no idle boundary will follow.
                                    # Close this waiter, then confirm Runner exit
                                    # and settle usage through the existing owner.
                                    break
                    except Exception:
                        failure = 'team_goal_stream_failed'
                    await self.confirm_exit()
                    evidence = self.attempt
                    if evidence is None:
                        failure = failure or 'team_goal_round_not_started'
                    else:
                        evidence.seal('completed' if boundary and self.current() and not failure else 'failed')
                        await self._account_provider_usage()
                        if not evidence.usage_complete:
                            self.accounting_unknown = True
                            failure = 'goal_usage_unavailable'
                        if not evidence.ready_for_assessment:
                            failure = failure or evidence.error or 'team_goal_terminal_unconfirmed'
                    if self.revoked or owner.cancellation_requested or not self.current():
                        break
                    assessment = None
                    if failure is None:
                        captured = []
                        pending_assessor = True
                        try:
                            assessor = GoalTranscriptAssessor(self.evaluator,
                                model_factory=getattr(request, '_goal_assessor_factory', self.assessor_factory))
                            assessment = await assessor.maybe_assess(record, None, evidence.transcript,
                                                                    is_current=self.current, on_usage=captured.append)
                            pending_assessor = False
                        finally:
                            if captured:
                                pending_assessor = False
                                assessor_unknown = not captured[-1].usage_available
                                await self._account_usage(identity, captured[-1].usage, propagate_cancel=False)
                        if assessment.error_code or not assessment.usage_available:
                            failure = assessment.error_code or 'goal_usage_unavailable'
                            self.accounting_unknown |= not assessment.usage_available
                    settled = await self.driver.finish(goal_id=identity.goal_id, revision=identity.revision,
                        attempt_index=identity.attempt_index, outcome='failed' if failure else 'completed',
                        execution_error=failure,
                        transcript_response=assessment.transcript_response if assessment else None)
                    if settled is not None:
                        await record_goal_completed(session_id=owner.session_id, channel_id=request.channel_id,
                            channel_metadata=request.metadata, mode=(request.params or {}).get('mode', 'unknown'),
                            goal_payload=settled.to_dict())
                        yield self.chunk(request, {'event_type': 'goal.updated', 'goal': settled.to_dict()})
                    if settled is None or settled.status is not GoalStatus.ACTIVE:
                        status = ('completed' if settled is not None and settled.status is GoalStatus.COMPLETED
                                  else 'cancelled' if settled is None or settled.status is GoalStatus.PAUSED else 'failed')
                        yield self.chunk(request, {'event_type': 'chat.final', 'content': '', 'terminal_status': status},
                                         complete=True, status=status)
                        break
                finally:
                    if self.identity is not None:
                        await self._cleanup_attempt(assessor_unknown=pending_assessor or assessor_unknown)
                    # History still owns the final release; between attempts the
                    # same root producer may yield its original execution permit.
                    if self.is_available() and (remaining := self.manager.peek()) is not None and remaining.status is GoalStatus.ACTIVE:
                        runtime.release_external_execution(owner)
        finally:
            # A failed exit/persistence keeps the captured owner retryable.
            if self.identity is None:
                record = self.manager.peek()
                if (record is not None and record.goal_id == owned_goal_id
                        and record.status is GoalStatus.ACTIVE and not self.parent_session.state_write_failed):
                    await self.manager.pause()
                self.adapter.release_goal_owner(runtime, owner, request, self)
