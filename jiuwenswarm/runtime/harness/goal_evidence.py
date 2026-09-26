# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Bounded attempt evidence observed by the existing External event consumer."""

from __future__ import annotations

import json
from dataclasses import dataclass

from openjiuwen.harness.goal.schema import TokenUsage
from openjiuwen.harness_protocol import (
    HarnessEvent,
    ItemLifecycleEvent,
    OutputEvent,
    OutputOperation,
    TurnEventKind,
    TurnLifecycleEvent,
    TurnUsage,
    UsageUpdatedEvent,
    UsageUpdateMode,
    json_value_to_builtin,
)


@dataclass(frozen=True, slots=True)
class GoalAttemptIdentity:
    """Host-owned attempt identity; generation changes with ExecutionSession."""

    goal_id: str
    revision: int
    attempt_index: int
    execution_id: str
    generation: int | str

    def __post_init__(self) -> None:
        if (
            not self.goal_id
            or not self.execution_id
            or type(self.revision) is not int
            or self.revision < 0
            or type(self.attempt_index) is not int
            or self.attempt_index < 1
        ):
            raise ValueError("goal attempt identity is incomplete")


_COUNTERS = (
    "input_tokens",
    "output_tokens",
    "cached_input_tokens",
    "reasoning_output_tokens",
    "total_tokens",
)
_TERMINAL = {TurnEventKind.FINISHED, TurnEventKind.FAILED, TurnEventKind.ABORTED}


class GoalAttemptEvidence:
    """One attempt's observations, never a Manager writer or execution owner.

    ``observe`` is synchronous and non-blocking. Call it on the existing sole
    event pump with that ExecutionSession's fixed generation. Before the send
    receipt arrives, a bounded buffer preserves observations; ``bind_turn``
    selects only the accepted turn. No EOF or timeout can manufacture terminal.
    """

    def __init__(
        self,
        identity: GoalAttemptIdentity,
        *,
        host_session_id: str,
        agent_id: str,
        max_transcript_chars: int = 64000,
        max_pending_events: int = 256,
        max_blocks: int = 256,
    ) -> None:
        if min(max_transcript_chars, max_pending_events, max_blocks) < 1:
            raise ValueError("evidence limits must be positive")
        self.identity = identity
        self.host_session_id = host_session_id
        self.agent_id = agent_id
        self.turn_id: str | None = None
        self.terminal: TurnLifecycleEvent | None = None
        self.error: str | None = None
        self.usage_error: str | None = None
        self._limit = max_transcript_chars
        self._pending_limit = max_pending_events
        self._block_limit = max_blocks
        self._pending: list[HarnessEvent] = []
        self._last_sequence = -1
        self._blocks: dict[str, str] = {}
        self._counters: dict[str, int | None] = dict.fromkeys(_COUNTERS)
        self._known: dict[str, int] = dict.fromkeys(_COUNTERS, 0)
        self._gaps: set[str] = set()
        self._has_usage = False
        self._usage_taken = False

    def bind_turn(self, turn_id: str) -> None:
        if not turn_id or (self.turn_id is not None and self.turn_id != turn_id):
            raise ValueError("attempt cannot bind a different turn")
        self.turn_id = turn_id
        pending, self._pending = self._pending, []
        for envelope in pending:
            self.observe(envelope, generation=self.identity.generation)

    def observe(self, envelope: HarnessEvent, *, generation: int | str) -> None:
        if (
            generation != self.identity.generation
            or envelope.host_session_id != self.host_session_id
            or envelope.agent_id != self.agent_id
            or not envelope.turn_id
        ):
            return
        if self.turn_id is None:
            if len(self._pending) >= self._pending_limit:
                self.error = "goal evidence before receipt exceeded its bound"
                self.usage_error = "goal usage may be missing after evidence overflow"
            else:
                self._pending.append(envelope)
            return
        if envelope.turn_id != self.turn_id or self.terminal is not None:
            return
        if envelope.sequence <= self._last_sequence:
            return
        self._last_sequence = envelope.sequence
        payload = envelope.event
        if isinstance(payload, OutputEvent):
            key = f"output:{payload.output_id}:{payload.content_index}"
            text = self._text(payload.content)
            if payload.operation is OutputOperation.DELTA:
                text = self._blocks.get(key, "") + text
            self._put_block(key, text)
        elif isinstance(payload, ItemLifecycleEvent):
            key = f"item:{envelope.item_id or envelope.sequence}"
            self._put_block(
                key,
                f"{payload.item_type} ({payload.kind.value}): {self._text(payload.data)}",
            )
        elif isinstance(payload, UsageUpdatedEvent):
            self._update_usage(payload.usage, payload.mode)
        elif isinstance(payload, TurnLifecycleEvent) and payload.kind in _TERMINAL:
            self.terminal = payload
            if payload.result.usage is not None:
                self._update_usage(payload.result.usage, UsageUpdateMode.CUMULATIVE)
            # Replace streamed output with normalized messages, but retain
            # tool/item evidence: Providers may return only assistant messages.
            if payload.result.messages:
                self._blocks = {
                    key: text
                    for key, text in self._blocks.items()
                    if key.startswith("item:")
                }
                count = 0
                if len(payload.result.messages) > self._block_limit:
                    self.error = "goal transcript exceeded its block bound"
                for message in payload.result.messages[: self._block_limit]:
                    if count >= self._block_limit:
                        self.error = "goal transcript exceeded its block bound"
                        break
                    for block in message.content:
                        if count >= self._block_limit:
                            self.error = "goal transcript exceeded its block bound"
                            break
                        count += 1
                        self._put_block(
                            f"message:{message.message_id}:{block.block_id}",
                            f"{message.role.value}/{block.kind}: {self._text(block.content)}",
                        )
            elif not self._blocks and payload.result.final_output is not None:
                self._put_block("final", self._text(payload.result.final_output))

    def _text(self, content) -> str:
        text = (
            content
            if isinstance(content, str)
            else json.dumps(
                json_value_to_builtin(content),
                ensure_ascii=False,
            )
        )
        if len(text) > self._limit:
            self.error = "goal transcript exceeded its character bound"
        return text[: self._limit]

    def _put_block(self, key: str, text: str) -> None:
        if key not in self._blocks and len(self._blocks) >= self._block_limit:
            self.error = "goal transcript exceeded its block bound"
            return
        other_size = sum(
            len(value) for name, value in self._blocks.items() if name != key
        )
        remaining = max(0, self._limit - other_size)
        if len(text) > remaining:
            self.error = "goal transcript exceeded its character bound"
        self._blocks[key] = text[:remaining]

    @property
    def transcript(self) -> str:
        return "\n".join(self._blocks.values())[: self._limit]

    def _update_usage(self, usage: TurnUsage, mode: UsageUpdateMode) -> None:
        self._has_usage = True
        for name in _COUNTERS:
            value = getattr(usage, name)
            if value is None:
                self._counters[name] = None
                self._gaps.add(name)
                continue
            if type(value) is not int:
                self.usage_error = "goal usage counters must be integers"
                self._counters[name] = None
                self._gaps.add(name)
                continue
            if mode is UsageUpdateMode.DELTA:
                # A missing earlier counter leaves the sum unknown. The known
                # subtotal is retained but never advertised as a complete sum.
                self._known[name] += value
                self._counters[name] = (
                    self._known[name] if name not in self._gaps else None
                )
            else:
                if value < self._known[name]:
                    self.usage_error = "cumulative goal usage decreased"
                self._gaps.discard(name)
                self._known[name] = max(value, self._known[name])
                self._counters[name] = value

    @property
    def usage(self) -> TurnUsage | None:
        return TurnUsage(**self._counters) if self._has_usage else None

    @property
    def reported_usage(self) -> dict[str, int]:
        """Known subtotals for diagnostics only, never complete budget usage."""
        return {
            name: value
            for name, value in self._known.items()
            if value or self._counters[name] is not None
        }

    @property
    def usage_complete(self) -> bool:
        usage = self.usage
        if self.usage_error or usage is None:
            return False
        if usage.input_tokens is None or usage.output_tokens is None:
            return False
        if (
            usage.cached_input_tokens is not None
            and usage.cached_input_tokens > usage.input_tokens
        ):
            return False
        return (
            usage.total_tokens is None
            or usage.total_tokens == usage.input_tokens + usage.output_tokens
        )

    def take_usage_delta(self) -> TokenUsage | None:
        """Return complete terminal usage once, without a streaming double write.

        The producer uses this exactly once in its indexed Manager/Driver call.
        Missing input/output remain unknown; independent total-only usage is
        deliberately not converted to a fabricated input/output split. Cached
        usage absent from the Provider contributes no *known* cached increment;
        its unknown value remains visible in ``usage``.
        """
        if self._usage_taken or self.terminal is None or not self.usage_complete:
            return None
        self._usage_taken = True
        usage = self.usage
        return TokenUsage(
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cached_input_tokens=usage.cached_input_tokens or 0,
            total_tokens=usage.input_tokens + usage.output_tokens,
        )
