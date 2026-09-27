# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Attempt-scoped observation through real immutable protocol envelopes."""

from dataclasses import replace

import pytest
from openjiuwen.harness_protocol import (
    ContentBlock,
    HarnessEvent,
    ItemEventKind,
    ItemLifecycleEvent,
    MessageRole,
    OutputEvent,
    OutputKind,
    OutputOperation,
    TurnEventKind,
    TurnLifecycleEvent,
    TurnMessage,
    TurnResult,
    TurnStatus,
    TurnUsage,
    UsageUpdatedEvent,
    UsageUpdateMode,
    TurnTermination,
    TurnTerminationKind,
)

from jiuwenswarm.runtime.harness.goal_evidence import (
    GoalAttemptEvidence,
    GoalAttemptIdentity,
)


def evidence(**kwargs):
    return GoalAttemptEvidence(
        GoalAttemptIdentity("goal", 2, 1, "owner", 7),
        host_session_id="host",
        agent_id="agent",
        **kwargs,
    )


def event(sequence, payload, **kwargs):
    return HarnessEvent(
        sequence, 0.0, payload, "host", "agent", turn_id="turn", **kwargs
    )


def output(text, operation=OutputOperation.DELTA):
    return OutputEvent("output", OutputKind.TEXT, text, operation=operation)


def finished(usage=None, **kwargs):
    return TurnLifecycleEvent(
        TurnEventKind.FINISHED, TurnResult(TurnStatus.COMPLETED, usage=usage, **kwargs)
    )


def observe(target, *payloads):
    for sequence, payload in enumerate(payloads, 1):
        target.observe(event(sequence, payload), generation=7)


def test_receipt_selects_exact_turn_and_generation_before_replay():
    target = evidence()
    target.observe(replace(event(100, output("old")), turn_id="old"), generation=7)
    target.observe(event(1, output("wrong generation")), generation=6)
    target.observe(
        replace(event(1, output("wrong host")), host_session_id="other"), generation=7
    )
    target.observe(replace(event(1, output("child")), agent_id="child"), generation=7)
    target.observe(event(2, output("accepted")), generation=7)
    assert target.transcript == ""
    target.bind_turn("turn")
    assert target.transcript == "accepted"
    with pytest.raises(ValueError, match="different turn"):
        target.bind_turn("another")


def test_replayed_sequence_does_not_duplicate_delta_and_only_terminal_finishes():
    target = evidence()
    target.bind_turn("turn")
    target.observe(event(1, output("hello")), generation=7)
    target.observe(event(1, output("hello")), generation=7)
    assert target.terminal is None
    target.observe(event(2, output("hello world", OutputOperation.FINAL)), generation=7)
    target.observe(event(3, finished()), generation=7)
    target.observe(event(4, output("late")), generation=7)
    assert target.transcript == "hello world"
    assert target.terminal.kind == TurnEventKind.FINISHED


def test_final_transcript_replaces_streamed_text_but_preserves_tool_evidence():
    target = evidence()
    target.bind_turn("turn")
    observe(
        target,
        output("answer"),
        ItemLifecycleEvent(ItemEventKind.COMPLETED, "tool", {"output": "checked"}),
        finished(
            messages=(
                TurnMessage(
                    "m", MessageRole.ASSISTANT, (ContentBlock("b", "text", "answer"),)
                ),
            )
        ),
    )
    assert target.transcript.count("answer") == 1
    assert "checked" in target.transcript


def test_delta_cumulative_final_usage_counted_once_at_terminal():
    target = evidence()
    target.bind_turn("turn")
    first = UsageUpdatedEvent(
        TurnUsage(input_tokens=3, output_tokens=2), UsageUpdateMode.DELTA
    )
    target.observe(event(1, first), generation=7)
    target.observe(event(1, first), generation=7)
    target.observe(
        event(
            2,
            UsageUpdatedEvent(
                TurnUsage(input_tokens=4, output_tokens=3), UsageUpdateMode.DELTA
            ),
        ),
        generation=7,
    )
    target.observe(
        event(
            3,
            UsageUpdatedEvent(
                TurnUsage(input_tokens=7, output_tokens=5, total_tokens=12)
            ),
        ),
        generation=7,
    )
    assert target.take_usage_delta() is None
    target.observe(
        event(4, finished(TurnUsage(input_tokens=7, output_tokens=5, total_tokens=12))),
        generation=7,
    )
    usage = target.take_usage_delta()
    assert (usage.input_tokens, usage.output_tokens, usage.total_tokens) == (7, 5, 12)
    assert target.usage.cached_input_tokens is None
    assert target.take_usage_delta() is None


@pytest.mark.parametrize(
    "usage",
    [
        TurnUsage(total_tokens=12),
        TurnUsage(input_tokens=4),
        TurnUsage(input_tokens=4, output_tokens=5, total_tokens=12),
    ],
)
def test_missing_or_inconsistent_usage_never_becomes_budget_zero(usage):
    target = evidence()
    target.bind_turn("turn")
    observe(target, finished(usage))
    assert not target.usage_complete
    assert target.take_usage_delta() is None
    assert target.usage == usage


def test_unknown_delta_cannot_be_repaired_by_another_delta_only_cumulative():
    target = evidence()
    target.bind_turn("turn")
    observe(
        target,
        UsageUpdatedEvent(TurnUsage(input_tokens=4), UsageUpdateMode.DELTA),
        UsageUpdatedEvent(
            TurnUsage(input_tokens=2, output_tokens=3), UsageUpdateMode.DELTA
        ),
    )
    assert target.usage.input_tokens == 6
    assert target.usage.output_tokens is None
    assert target.reported_usage == {"input_tokens": 6, "output_tokens": 3}
    target.observe(
        event(3, finished(TurnUsage(input_tokens=6, output_tokens=8))), generation=7
    )
    assert target.usage_complete
    assert target.take_usage_delta().total_tokens == 14


def test_decreasing_cumulative_usage_fails_closed_but_preserves_known_subtotal():
    target = evidence()
    target.bind_turn("turn")
    observe(
        target,
        UsageUpdatedEvent(TurnUsage(input_tokens=10, output_tokens=5)),
        finished(TurnUsage(input_tokens=2, output_tokens=1)),
    )
    assert target.usage_error
    assert target.reported_usage == {"input_tokens": 10, "output_tokens": 5}
    assert target.take_usage_delta() is None


@pytest.mark.parametrize(
    "limits,payloads",
    [
        ({"max_transcript_chars": 4}, (output("oversized"),)),
        (
            {"max_blocks": 1},
            (output("a"), ItemLifecycleEvent(ItemEventKind.COMPLETED, "tool", {})),
        ),
        ({"max_pending_events": 1}, (output("a"), output("b"))),
    ],
)
def test_evidence_overflow_is_explicit_and_bounded(limits, payloads):
    target = evidence(**limits)
    observe(target, *payloads)
    target.bind_turn("turn")
    assert target.error
    assert len(target.transcript) <= limits.get("max_transcript_chars", 64000)
    assert not target.usage_complete


def test_no_usage_remains_unknown_after_finished():
    target = evidence()
    target.bind_turn("turn")
    observe(target, finished(final_output="done"))
    assert target.transcript == "done"
    assert target.usage is None
    assert target.reported_usage == {}
    assert not target.usage_complete


def test_aborted_terminal_retains_usage_without_claiming_goal_completion():
    target = evidence()
    target.bind_turn("turn")
    result = TurnResult(
        TurnStatus.INTERRUPTED,
        termination=TurnTermination(TurnTerminationKind.USER_ABORT),
        usage=TurnUsage(input_tokens=2, output_tokens=1),
    )
    observe(target, TurnLifecycleEvent(TurnEventKind.ABORTED, result))
    assert target.terminal.kind == TurnEventKind.ABORTED
    assert target.take_usage_delta().total_tokens == 3
    assert target.take_usage_delta() is None


def test_terminal_message_overflow_cannot_hide_truncated_assessment_evidence():
    target = evidence(max_blocks=1)
    target.bind_turn("turn")
    observe(
        target,
        finished(
            messages=(
                TurnMessage(
                    "m",
                    MessageRole.ASSISTANT,
                    (
                        ContentBlock("a", "text", "first"),
                        ContentBlock("b", "text", "lost"),
                    ),
                ),
            )
        ),
    )
    assert "first" in target.transcript
    assert "lost" not in target.transcript
    assert target.error


def test_transcript_truncation_does_not_discard_complete_usage():
    target = evidence(max_transcript_chars=4)
    target.bind_turn("turn")
    observe(
        target,
        output("oversized"),
        finished(TurnUsage(input_tokens=2, output_tokens=1)),
    )
    assert target.error
    assert target.usage_complete
    assert target.take_usage_delta().total_tokens == 3


@pytest.mark.parametrize(
    "usage",
    [
        TurnUsage(input_tokens=True, output_tokens=2),
        TurnUsage(input_tokens=1.5, output_tokens=2),
        TurnUsage(input_tokens=1, output_tokens=2, cached_input_tokens=3),
    ],
)
def test_invalid_protocol_counter_shapes_never_reach_goal_budget(usage):
    target = evidence()
    target.bind_turn("turn")
    observe(target, finished(usage))
    assert not target.usage_complete
    assert target.take_usage_delta() is None
