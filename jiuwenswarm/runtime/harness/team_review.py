# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""One scheduled review invocation over the original External IO and Team store."""
from __future__ import annotations

import asyncio
from dataclasses import asdict, replace
import hashlib
import json

from openjiuwen.agent_teams.external.member_runtime import ExternalHarnessMemberRuntime
from openjiuwen.agent_teams.schema.stream import TeamOutputSchema
from openjiuwen.agent_teams.schema.team import TeamRole
from openjiuwen.harness.engine import ExecutionBinding
from openjiuwen.harness_protocol import HostCapability, ResumePolicy, TurnEventKind, TurnLifecycleEvent, WorkspaceAccess

from jiuwenswarm.runtime.harness.context_bridge import build_external_context
from jiuwenswarm.runtime.harness.team_goal_evidence import TeamGoalEventObserver, TeamGoalSource
from jiuwenswarm.runtime.harness.team_projection import TeamMemberProjection
from jiuwenswarm.runtime.harness.tool_gateway import ProductToolGateway, ProductToolScope

_REVIEW_STATE = 'external_team_reviews'


def validate_review_recovery(session):
    records = session.get_state(_REVIEW_STATE)
    if records is None:
        records = {}
    if not isinstance(records, dict) or any(
            not isinstance(record, dict) or record.get('status') != 'closed' for record in records.values()):
        raise ValueError('Scheduled review has unconfirmed execution, usage or history; automatic replay refused')


class TeamReviewProjection(TeamMemberProjection):
    """Reuse product history/artifact projection with explicit temporary identity."""
    def goal_observer(self, host_session_id, agent_id, *, product_subagent=False):
        from jiuwenswarm.agents.harness.team import get_team_manager
        root = self.route.bound.binding.host_session_id
        manager = get_team_manager(self.route.channel_id)
        self._review_goal_current = lambda: manager.current_goal_attempt_evidence(root)
        return TeamGoalEventObserver(TeamGoalSource(
            root, self.binding.host_session_id, host_session_id, agent_id, self.request.reviewer,
            execution_kind='reviewer',
        ), self._review_goal_current)

    def identity(self, turn_id=None):
        return {
            'team_name': self.request.spec.team_name, 'source_member': self.request.reviewer,
            'member_name': self.request.reviewer, 'member_id': self.binding.host_session_id,
            'member_session_id': self.binding.host_session_id, 'role': 'reviewer',
            'execution_kind': 'scheduled_review', 'review_task_id': self.request.task_id,
            'review_round': self.request.review_round, 'review_invocation_id': self.request.invocation_id,
            'provider_session_id': self.runtime.session_id if self.runtime else None,
            'provider_id': self.route.provider_id, 'turn_id': turn_id,
        }

    def invalidate(self, reason):
        evidence = self._review_goal_current()
        if evidence is not None and evidence.accepting:
            evidence.invalidate(reason)


class ExternalTeamReviewRuntime:
    """Original scheduler owns run_once/dispose; no roster or Provider queue."""
    def __init__(self, factory, request, *, create_engine, transport_type):
        self.factory, self.request = factory, request
        self.provider_id = factory.provider_id
        self._root = factory._route.bound.binding
        self._session = request.team_session
        if self._session is None or self._session.get_session_id() != self._root.host_session_id:
            raise ValueError('Reviewer belongs to another Team Session')
        if request.spec.team_name != factory._team_name or request.spec.dispatch_mode != 'scheduled':
            raise ValueError('Reviewer requires the admitted scheduled Team')
        if not all(isinstance(value, str) and value for value in (
                request.reviewer, request.task_id, request.invocation_id)) or request.review_round < 1:
            raise ValueError('Reviewer invocation identity is incomplete')
        if tuple(tool.card.name for tool in request.tools) != ('verify_task', 'view_task'):
            raise ValueError('Reviewer requires original Verify/View tools')
        manager = request.tools[0].task_manager
        if manager.member_name != request.reviewer or manager.team_name != factory._team_name:
            raise ValueError('Reviewer tool identity does not match its invocation')
        self._key = hashlib.sha256(json.dumps([
            factory._team_name, request.task_id, request.review_round, request.reviewer,
        ], separators=(',', ':')).encode()).hexdigest()
        self.interaction_owner = 'review:' + request.invocation_id
        agent_id = 'team-review:' + request.invocation_id
        self.binding = ExecutionBinding.create(
            factory._route.bound.spec, subject_id=self._root.subject_id,
            host_session_id=f'{self._root.host_session_id}:{agent_id}', workspace=self._root.workspace,
        )
        self._engine = create_engine(factory._route.bound.spec, binding=self.binding)
        self._transport_type = transport_type
        self._transport = None
        self._accepting = False
        self._started = False
        self._submitted = False
        self._terminal = None
        self._history_error = None
        self._done = None
        self._consumer = None
        self._dispose_lock = asyncio.Lock()
        self._closed = False
        self._marked = False
        self._projection = TeamReviewProjection(factory._route, request, self.binding, agent_id=agent_id)
        self.runtime = ExternalHarnessMemberRuntime(
            harness=self._engine.harness, context=self._context,
            auto_approve_tools=factory.surface.runtime_policy.workspace_access is WorkspaceAccess.FULL_ACCESS,
            interaction_scope=(factory._team_name, self.interaction_owner, self._root.host_session_id),
            event_observer=self._observe, output_projection=self._projection.output,
        )
        self._projection.runtime = self.runtime

    async def _context(self, session):
        self.factory._validate_team_spec(self.request.spec, scheduled=True)
        if session is not self._session:
            raise ValueError('Reviewer Team Session changed')
        scope = ProductToolScope(self.binding.subject_id, self.binding.host_session_id, self.binding.workspace)

        async def admit(actual_scope, invocation):
            if not (self._accepting and actual_scope == scope and self.factory._host_selection_unchanged()):
                return False
            if invocation.name == 'verify_task':
                if invocation.arguments.get('task_id') != self.request.task_id:
                    return False
                task = await self.request.tools[0].task_manager.get(self.request.task_id)
                return task is not None and task.status == 'in_review' and task.review_round == self.request.review_round
            return True

        gateway = ProductToolGateway(self.request.tools, scope=scope, admit=admit,
            invoke_kwargs={'member_name': self.request.reviewer, 'display_name': self.request.reviewer})
        self._transport = self._transport_type(gateway, host_session_id=self.binding.host_session_id)
        await self._transport.start()
        self._accepting = True
        context = build_external_context(
            paths=self.factory._route.runtime_paths, host_session_id=self.binding.host_session_id,
            channel_id=self.factory._route.channel_id, provider_id=self.provider_id, surface=self.factory.surface,
        )
        return replace(context, agent_id='team-review:' + self.request.invocation_id,
            agent_name=self.interaction_owner, system_prompt=context.system_prompt + '\n\n' + self.request.system_prompt,
            mcp_servers=(self._transport.server_config(),),
            host_capabilities=context.host_capabilities | {HostCapability.MCP_SERVERS},
            resume_policy=ResumePolicy.NEW,
            metadata={**context.metadata, **self._projection.identity(), 'review_binding': asdict(self.binding),
                      'parent_session_id': self._root.host_session_id})

    async def _mark(self, status):
        async with self.factory._review_state_lock:
            records = self._session.get_state(_REVIEW_STATE)
            if records is None:
                records = {}
            if not isinstance(records, dict):
                raise ValueError('Invalid persisted reviewer state')
            previous = records.get(self._key)
            if not self._marked and previous is not None and (
                    not isinstance(previous, dict) or previous.get('status') != 'closed'):
                raise ValueError('Previous reviewer execution is unconfirmed; replay refused')
            previous_records = records
            records = {**records, self._key: {
                'status': status, 'invocation_id': self.request.invocation_id,
                'binding': asdict(self.binding), 'task_id': self.request.task_id,
                'review_round': self.request.review_round, 'reviewer': self.request.reviewer,
            }}
            self._session.update_state({_REVIEW_STATE: records})
            try:
                await self._session.commit()
            except BaseException:
                # Failed completion persistence must not admit warm replay.
                # Keep a first pending marker if its commit outcome is unknown.
                if previous is not None:
                    self._session.update_state({_REVIEW_STATE: previous_records})
                raise
            self._marked = True

    async def _observe(self, event):
        await self._projection.observe(event)
        if isinstance(event.event, TurnLifecycleEvent) and event.event.kind in {
                TurnEventKind.FINISHED, TurnEventKind.FAILED, TurnEventKind.ABORTED}:
            self._terminal = event.event

    async def _consume(self):
        try:
            async for chunk in self.runtime.outputs():
                try:
                    # The original Runner consumer filters by Team envelope
                    # role before parsing payloads. Temporary review identity
                    # stays in the payload; this does not add a roster member.
                    sink = self.request.output_sink or self._session.write_stream
                    await sink(TeamOutputSchema.from_output(
                        chunk, source_member=self.request.reviewer, role=TeamRole.TEAMMATE))
                    payload = chunk.payload
                    if isinstance(payload, dict) and payload.get('code') == 'HISTORY_PERSISTENCE_UNCONFIRMED':
                        raise RuntimeError('Reviewer history persistence unconfirmed')
                    if isinstance(payload, dict) and payload.get('terminal_status') and not self._done.done():
                        self._done.set_result(None)
                except Exception as exc:
                    self._history_error = exc
                    self._projection.invalidate('team_review_history_unconfirmed')
                    if not self._done.done():
                        self._done.set_exception(exc)
        except Exception as exc:
            self._history_error = exc
            self._projection.invalidate('team_review_output_unconfirmed')
            if not self._done.done():
                self._done.set_exception(exc)
        finally:
            if not self._done.done():
                self._history_error = RuntimeError('Reviewer stream ended without a terminal')
                self._projection.invalidate('team_review_output_unconfirmed')
                self._done.set_exception(self._history_error)

    async def run_once(self, prompt):
        if self._started or self._closed:
            raise RuntimeError('Reviewer invocation cannot be replayed')
        self._started = True
        self._done = asyncio.get_running_loop().create_future()
        self._done.add_done_callback(lambda f: None if f.cancelled() else f.exception())
        await self._mark('pending')
        await self.runtime.start(team_session=self._session)
        self._consumer = asyncio.create_task(self._consume(), name='team-review-output')
        self._submitted = True
        await self.runtime.send(prompt)
        await asyncio.shield(self._done)
        if self._terminal is None or self._terminal.kind is not TurnEventKind.FINISHED:
            raise RuntimeError('Reviewer did not finish successfully')
        if not self._known_usage():
            self._projection.invalidate('team_review_usage_unconfirmed')
            raise RuntimeError('Reviewer usage is unconfirmed')
        return self._terminal.result.final_output

    def _known_usage(self):
        usage = self._terminal.result.usage if self._terminal and self._terminal.result else None
        return (usage is not None and usage.input_tokens is not None and usage.output_tokens is not None
                and (usage.total_tokens is None or usage.total_tokens == usage.input_tokens + usage.output_tokens)
                and (usage.cached_input_tokens is None or usage.cached_input_tokens <= usage.input_tokens))

    def is_pending_interrupt_resume_valid(self, payload):
        return self.runtime.is_pending_interrupt_resume_valid(payload)

    async def send(self, payload):
        # Only exact pending replies; never a second reviewer input.
        if not self.is_pending_interrupt_resume_valid(payload):
            from openjiuwen.harness_protocol import HarnessStateError
            raise HarnessStateError('Stale reviewer interaction')
        return await self.runtime.send(payload)

    async def dispose(self):
        async with self._dispose_lock:
            if self._closed:
                return
            self._accepting = False
            await self.runtime.dispose()
            if self._consumer is not None:
                await self._consumer
            if self._transport is not None:
                await self._transport.stop()
                if not self._transport.exit_confirmed:
                    raise RuntimeError('Reviewer MCP exit unconfirmed')
            await self._projection.close()
            known = self._known_usage()
            successful = self._terminal is not None and self._terminal.kind is TurnEventKind.FINISHED
            if self._marked:
                await self._mark('closed' if not self._submitted or (
                    successful and known and self._history_error is None) else 'blocked')
            if self._submitted and not (successful and known and self._history_error is None):
                self._projection.invalidate('team_review_execution_or_usage_unconfirmed')
            self._closed = True
