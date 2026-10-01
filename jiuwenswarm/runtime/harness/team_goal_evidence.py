# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Read-only Team attempt evidence, attached to the original admitted Round."""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Callable
from uuid import uuid4

from openjiuwen.harness.goal.schema import TokenUsage
from openjiuwen.harness_protocol import (
    HarnessEvent, HarnessState, StateChangedEvent, TurnEventKind, TurnLifecycleEvent,
)
from jiuwenswarm.runtime.harness.goal_evidence import GoalAttemptEvidence, GoalAttemptIdentity


@dataclass(frozen=True, slots=True)
class TeamGoalSource:
    root_session_id: str
    member_session_id: str
    host_session_id: str
    agent_id: str
    member_name: str
    product_subagent: bool = False
    cycle: str = field(default_factory=lambda: uuid4().hex)
    execution_kind: str = "member"

    def __post_init__(self):
        if any(not isinstance(value, str) or not value or len(value.encode()) > 1024
               for value in (self.root_session_id, self.member_session_id, self.host_session_id,
                             self.agent_id, self.member_name, self.cycle)):
            raise ValueError('Team Goal source identity is incomplete or oversized')


class TeamGoalAttemptEvidence:
    """No tasks, Provider cursors, manager writes, or inferred Team completion.

    The future Goal producer must explicitly seal the original Team Round.
    Provider FINISHED events and Team idle alone never authorize settlement.
    """
    def __init__(self, identity: GoalAttemptIdentity, *, session_id: str, request_id: str,
                 is_current: Callable[[], bool], max_turns: int = 128,
                 max_transcript_chars: int = 64000):
        if not session_id or not request_id or min(max_turns, max_transcript_chars) < 1:
            raise ValueError('Team Goal evidence requires identity and positive bounds')
        self.identity = identity
        self.session_id = session_id
        self.request_id = request_id
        self._is_current = is_current
        self._max_turns = max_turns
        self._text_limit = max_transcript_chars
        self._turns: dict[tuple[TeamGoalSource, str, str], GoalAttemptEvidence] = {}
        self._error: str | None = None
        self._sealed: str | None = None
        self._released = False
        self._usage_taken = False

    @property
    def sealed(self):
        return self._sealed is not None

    @property
    def accepting(self):
        return not self._released and self._sealed is None and self._is_current()

    def invalidate(self, reason: str):
        if self._error is None:
            self._error = reason

    @property
    def error(self):
        return self._error or next((e.error or e.usage_error for e in self._turns.values()
                                   if e.error or e.usage_error), None)

    def observe(self, source: TeamGoalSource, event: HarnessEvent):
        if self._released or self._sealed is not None:
            return
        if (source.root_session_id != self.session_id
                or event.host_session_id != source.host_session_id or event.agent_id != source.agent_id):
            self.invalidate('team_goal_source_mismatch')
            return
        if not event.turn_id or not event.provider_session_id:
            self.invalidate('team_goal_turn_identity_missing')
            return
        key = (source, event.provider_session_id, event.turn_id)
        evidence = self._turns.get(key)
        if evidence is None:
            if not self.accepting:
                return
            if not (isinstance(event.event, TurnLifecycleEvent) and event.event.kind is TurnEventKind.STARTED):
                self.invalidate('team_goal_turn_start_missing')
                return
            if len(self._turns) >= self._max_turns:
                self.invalidate('team_goal_turn_budget_exceeded')
                return
            evidence = GoalAttemptEvidence(self.identity, host_session_id=source.host_session_id,
                agent_id=source.agent_id, max_transcript_chars=self._text_limit)
            evidence.bind_turn(event.turn_id)
            self._turns[key] = evidence
        evidence.observe(event, generation=self.identity.generation)
        if len(self._render_transcript()) > self._text_limit:
            self.invalidate('team_goal_transcript_budget_exceeded')

    @property
    def turn_count(self):
        return len(self._turns)

    @property
    def transcript(self):
        return self._render_transcript()[:self._text_limit]

    def _render_transcript(self):
        return '\n'.join(
            f'[{s.member_name}/{"product" if s.product_subagent else s.execution_kind}/{host}/{turn}]\n{e.transcript}'
            for (s, host, turn), e in self._turns.items()
        )

    @property
    def all_terminal(self):
        return bool(self._turns) and all(e.terminal is not None for e in self._turns.values())

    @property
    def usage_complete(self):
        return self.all_terminal and not self.error and all(e.usage_complete for e in self._turns.values())

    @property
    def ready_for_assessment(self):
        return (self._sealed == 'completed' and not self._released and self._is_current() and self.usage_complete
                and all(e.terminal.kind is TurnEventKind.FINISHED for e in self._turns.values()))

    def seal(self, outcome: str):
        """Called by the owning producer, never by a typed event observer."""
        if outcome not in {'completed', 'failed', 'cancelled', 'unknown'}:
            raise ValueError('Unknown Team Round outcome')
        if self._sealed is not None:
            if outcome != self._sealed:
                raise ValueError('Team Round evidence is already sealed')
            return
        if self._released or (outcome == 'completed' and not self.accepting):
            raise RuntimeError('Team Goal evidence owner is no longer current')
        self._sealed = outcome
        if not self.all_terminal:
            self.invalidate('team_goal_terminal_unconfirmed')

    def release(self):
        # Dropping a Round or its reader never manufactures its terminal.
        if self._sealed is None:
            self.invalidate('team_goal_round_released_without_settlement')
        self._released = True

    def take_usage_delta(self) -> TokenUsage | None:
        if self._usage_taken or self._sealed is None or not self.usage_complete:
            return None
        self._usage_taken = True
        usages = [e.take_usage_delta() for e in self._turns.values()]
        return TokenUsage(
            input_tokens=sum(u.input_tokens for u in usages),
            output_tokens=sum(u.output_tokens for u in usages),
            cached_input_tokens=sum(u.cached_input_tokens for u in usages),
            total_tokens=sum(u.total_tokens for u in usages),
        )


class TeamGoalEventObserver:
    """One source cycle; capture Round ownership on STARTED, never on replay."""
    def __init__(self, source: TeamGoalSource,
                 current: Callable[[], TeamGoalAttemptEvidence | None], *, max_tracked_turns: int = 256):
        if max_tracked_turns < 1:
            raise ValueError('Tracked Turn bound must be positive')
        self.source = source
        self._current = current
        self._limit = max_tracked_turns
        self._owners: OrderedDict[tuple[str, str], TeamGoalAttemptEvidence | None] = OrderedDict()
        self._last_sequence = -1
        self._closed = False

    async def __call__(self, event: HarnessEvent):
        if self._closed:
            return
        current = self._current()
        if event.host_session_id != self.source.host_session_id or event.agent_id != self.source.agent_id:
            if current is not None and current.accepting:
                current.invalidate('team_goal_source_mismatch')
            return
        if event.sequence <= self._last_sequence:
            return
        self._last_sequence = event.sequence
        if isinstance(event.event, StateChangedEvent) and event.event.new is HarnessState.TERMINATED:
            self.close()
            return
        if not event.turn_id:
            return
        if (not event.provider_session_id or len(event.provider_session_id.encode()) > 1024
                or len(event.turn_id.encode()) > 1024):
            if current is not None and current.accepting:
                current.invalidate('team_goal_turn_identity_missing')
            return
        key = (event.provider_session_id, event.turn_id)
        started = isinstance(event.event, TurnLifecycleEvent) and event.event.kind is TurnEventKind.STARTED
        if started and key in self._owners:
            if current is not None and current.accepting:
                current.invalidate('team_goal_reused_turn_identity')
            return
        if started and key not in self._owners:
            if len(self._owners) >= self._limit:
                # Evict only ended/unowned tombstones. Active sources retain
                # the exact old owner even across request or Goal replacement.
                expired = next((k for k, v in self._owners.items() if v is None), None)
                if expired is None:
                    if current is not None and current.accepting:
                        current.invalidate('team_goal_source_turn_budget_exceeded')
                    return
                self._owners.pop(expired)
            self._owners[key] = current if current is not None and current.accepting else None
        elif key not in self._owners:
            if current is not None and current.accepting:
                current.invalidate('team_goal_unattributed_turn')
            return
        owner = self._owners[key]
        if owner is not None:
            owner.observe(self.source, event)
        if isinstance(event.event, TurnLifecycleEvent) and event.event.kind in {
            TurnEventKind.FINISHED, TurnEventKind.FAILED, TurnEventKind.ABORTED,
        }:
            self._owners[key] = None

    def close(self):
        for owner in self._owners.values():
            if owner is not None and owner.accepting:
                owner.invalidate('team_goal_source_exit_before_terminal')
        self._owners.clear()
        self._closed = True
