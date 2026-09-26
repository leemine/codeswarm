# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Independent Goal assessment: authorization, no-tool calls and stale evidence."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from openjiuwen.core.foundation.llm.schema.message import (
    AssistantMessage,
    UsageMetadata,
)
from openjiuwen.harness.goal.evaluation import GoalEvaluator
from openjiuwen.harness.goal.schema import (
    GoalAssessment,
    GoalAssessmentStatus,
    GoalRecord,
    GoalStopConfig,
    GoalStopStrategy,
)
from openjiuwen.harness.prompts.sections.goal import (
    TRANSCRIPT_ASSESSOR_SYSTEM,
    build_goal_current_instruction,
    build_transcript_assessor_prompt,
)

from jiuwenswarm.runtime.harness.goal_assessment import (
    GoalTranscriptAssessor,
    catalog_model_factory,
)
from jiuwenswarm.runtime.model_catalog import ModelCatalogError


RESPONSE = '{"status":"complete","evidence":"verified artifact"}'


def _record(attempts=1):
    record = GoalRecord.create(session_id="session", objective="complete the artifact")
    record.attempt_count = attempts
    return record


def _response(usage=None):
    return AssistantMessage(
        content=RESPONSE,
        usage_metadata=usage
        or UsageMetadata(
            input_tokens=5, output_tokens=3, total_tokens=8, cache_read_tokens=2
        ),
    )


def _entry(name, *, alias="", source=None):
    return {
        "alias": alias,
        "model_client_config": {
            "model_name": name,
            "api_key": "test-secret",
            "api_base": "https://example.invalid/v1",
        },
        "model_config_obj": {"reasoning_level": "high", "_source": source},
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "strategy,report_status,attempts,expected",
    [
        (GoalStopStrategy.AGENT_REPORT, None, 1, False),
        (GoalStopStrategy.AGENT_REPORT, GoalAssessmentStatus.COMPLETE, 1, False),
        (GoalStopStrategy.TRANSCRIPT, GoalAssessmentStatus.CONTINUE, 1, True),
        (GoalStopStrategy.HYBRID, None, 1, True),
        (GoalStopStrategy.HYBRID, GoalAssessmentStatus.COMPLETE, 1, True),
        (GoalStopStrategy.HYBRID, GoalAssessmentStatus.BLOCKED, 1, True),
        (GoalStopStrategy.HYBRID, GoalAssessmentStatus.CONTINUE, 1, False),
        (GoalStopStrategy.HYBRID, GoalAssessmentStatus.CONTINUE, 2, True),
    ],
)
async def test_native_policy_invokes_actual_model_without_tools(
    strategy, report_status, attempts, expected
):
    evaluator = GoalEvaluator(
        GoalStopConfig(strategy=strategy, verification_interval=2)
    )
    model = SimpleNamespace(invoke=AsyncMock(return_value=_response()))
    factory = Mock(return_value=model)
    assessor = GoalTranscriptAssessor(evaluator, model_factory=factory, language="en")
    record = _record(attempts)
    before = record.to_dict()
    report = (
        GoalAssessment(status=report_status, evidence="agent evidence")
        if report_status
        else None
    )
    result = await assessor.maybe_assess(
        record, report, "attempt transcript", is_current=lambda: True
    )
    assert result.invoked is expected
    assert record.to_dict() == before
    if not expected:
        factory.assert_not_called()
        model.invoke.assert_not_awaited()
        assert result.usage is None and result.usage_available
        return
    model.invoke.assert_awaited_once()
    args, kwargs = model.invoke.call_args
    assert kwargs == {"tools": [], "temperature": 0.0, "top_p": 1.0}
    assert args[0][0].content == TRANSCRIPT_ASSESSOR_SYSTEM["en"]
    assert args[0][1].content == build_transcript_assessor_prompt(
        record.objective,
        build_goal_current_instruction(record, "en"),
        "attempt transcript",
        "en",
    )
    assert result.transcript_response == RESPONSE
    assert result.usage.to_dict() == dict(
        input_tokens=5, output_tokens=3, cached_input_tokens=2, total_tokens=8
    )
    assert (
        evaluator.assess(
            record, report, transcript_response=result.transcript_response
        ).status
        is GoalAssessmentStatus.COMPLETE
    )


@pytest.mark.asyncio
async def test_async_authorized_factory_is_awaited_and_unknown_language_uses_core_default():
    model = SimpleNamespace(invoke=AsyncMock(return_value=_response()))
    factory = AsyncMock(return_value=model)
    assessor = GoalTranscriptAssessor(
        GoalEvaluator(), model_factory=factory, language="unknown"
    )
    await assessor.maybe_assess(_record(), None, "transcript", is_current=lambda: True)
    factory.assert_awaited_once_with()
    assert model.invoke.call_args.args[0][0].content == TRANSCRIPT_ASSESSOR_SYSTEM["cn"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "usage",
    [
        None,
        {},
        {"total_tokens": 8},
        {"input_tokens": 5},
        {"input_tokens": 5, "output_tokens": True},
        {"input_tokens": -1, "output_tokens": 3},
        {"input_tokens": 5, "output_tokens": 3, "total_tokens": 9},
        UsageMetadata(total_tokens=8),
        UsageMetadata(),
    ],
)
async def test_unavailable_usage_is_never_zero_filled(usage):
    model = SimpleNamespace(
        invoke=AsyncMock(
            return_value=SimpleNamespace(content=RESPONSE, usage_metadata=usage)
        )
    )
    result = await GoalTranscriptAssessor(
        GoalEvaluator(), model_factory=lambda: model
    ).maybe_assess(_record(), None, "x", is_current=lambda: True)
    assert result.invoked and not result.usage_available and result.usage is None
    assert result.error_code == "ASSESSOR_USAGE_UNAVAILABLE"


@pytest.mark.asyncio
async def test_explicit_zero_usage_is_available_and_cached_input_is_not_added_twice():
    model = SimpleNamespace(
        invoke=AsyncMock(
            return_value=_response(
                UsageMetadata(input_tokens=0, output_tokens=0, total_tokens=0)
            )
        )
    )
    result = await GoalTranscriptAssessor(
        GoalEvaluator(), model_factory=lambda: model
    ).maybe_assess(_record(), None, "x", is_current=lambda: True)
    assert result.usage_available and result.usage.total_tokens == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("factory_fails", [True, False])
async def test_errors_visible_without_leaking_secrets_or_retrying_calls(factory_fails):
    fail = TypeError("test-secret in request internals")
    model = SimpleNamespace(invoke=AsyncMock(side_effect=fail))
    factory = Mock(side_effect=fail) if factory_fails else Mock(return_value=model)
    evaluator = GoalEvaluator()
    result = await GoalTranscriptAssessor(
        evaluator, model_factory=factory
    ).maybe_assess(
        _record(),
        GoalAssessment(
            status=GoalAssessmentStatus.COMPLETE, evidence="claimed complete"
        ),
        "x",
        is_current=lambda: True,
    )
    assert result.transcript_response is None and "test-secret" not in repr(result)
    assert result.invoked is not factory_fails
    assert result.usage_available is factory_fails
    assert result.error_code == (
        "ASSESSOR_MODEL_UNAVAILABLE" if factory_fails else "ASSESSOR_INVOCATION_FAILED"
    )
    assert model.invoke.await_count == (0 if factory_fails else 1)
    assert (
        evaluator.assess(
            _record(), transcript_response=result.transcript_response
        ).status
        is GoalAssessmentStatus.CONTINUE
    )


@pytest.mark.asyncio
async def test_stale_attempt_never_constructs_model():
    factory = Mock()
    with pytest.raises(asyncio.CancelledError):
        await GoalTranscriptAssessor(
            GoalEvaluator(), model_factory=factory
        ).maybe_assess(_record(), None, "x", is_current=lambda: False)
    factory.assert_not_called()


@pytest.mark.asyncio
async def test_factory_yield_checks_current_before_invoking():
    current = True
    model = SimpleNamespace(invoke=AsyncMock())

    async def factory():
        nonlocal current
        current = False
        return model

    with pytest.raises(asyncio.CancelledError):
        await GoalTranscriptAssessor(
            GoalEvaluator(), model_factory=factory
        ).maybe_assess(_record(), None, "x", is_current=lambda: current)
    model.invoke.assert_not_awaited()


@pytest.mark.asyncio
async def test_late_response_after_identity_change_cannot_be_settled():
    current = True

    async def invoke(*args, **kwargs):
        nonlocal current
        current = False
        return _response()

    with pytest.raises(asyncio.CancelledError):
        await GoalTranscriptAssessor(
            GoalEvaluator(), model_factory=lambda: SimpleNamespace(invoke=invoke)
        ).maybe_assess(_record(), None, "x", is_current=lambda: current)


@pytest.mark.asyncio
@pytest.mark.parametrize("swallows_cancel", [False, True])
async def test_cancellation_terminates_assessment_and_rejects_late_response(
    swallows_cancel,
):
    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def invoke(*args, **kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            if not swallows_cancel:
                raise
        return _response()

    assessor = GoalTranscriptAssessor(
        GoalEvaluator(), model_factory=lambda: SimpleNamespace(invoke=invoke)
    )
    task = asyncio.create_task(
        assessor.maybe_assess(_record(), None, "x", is_current=lambda: True)
    )
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_timeout_is_visible_and_not_known_zero_cost():
    async def invoke(*args, **kwargs):
        await asyncio.Event().wait()

    assessor = GoalTranscriptAssessor(
        GoalEvaluator(),
        model_factory=lambda: SimpleNamespace(invoke=invoke),
        timeout_seconds=0.01,
    )
    result = await assessor.maybe_assess(_record(), None, "x", is_current=lambda: True)
    assert result.error_code == "ASSESSOR_TIMEOUT"
    assert result.invoked and not result.usage_available


def test_catalog_global_index_snapshot_preserves_endpoint_and_reasoning():
    entries = [_entry("same"), _entry("video"), _entry("same")]
    builder = Mock(return_value=object())
    factory = catalog_model_factory(entries, "same#2", model_builder=builder)
    entries[2]["model_client_config"]["api_key"] = "mutated"
    factory()
    client, config = builder.call_args.args
    assert client["api_key"] == "test-secret"
    assert config["reasoning_level"] == "high"
    assert client["api_base"] == "https://example.invalid/v1"


@pytest.mark.parametrize(
    "selection", ["", "unknown", "same#9", "same#1", "video", "ambiguous"]
)
def test_catalog_never_falls_back_for_invalid_explicit_selection(selection):
    builder = Mock()
    with pytest.raises(ModelCatalogError):
        catalog_model_factory(
            [_entry("same", alias="ambiguous"), _entry("other", alias="ambiguous")],
            selection,
            model_builder=builder,
        )
    builder.assert_not_called()


@pytest.mark.parametrize(
    "requires_request_authorization,source", [(True, None), (False, "agentos")]
)
def test_catalog_refuses_request_credentials_without_authorized_factory(
    requires_request_authorization, source
):
    with pytest.raises(ModelCatalogError) as caught:
        catalog_model_factory(
            [_entry("login", source=source)],
            "login",
            requires_request_authorization=requires_request_authorization,
        )
    assert caught.value.code == "ASSESSOR_AUTHORIZATION_REQUIRED"


def test_catalog_builder_failure_does_not_expose_configuration():
    factory = catalog_model_factory(
        [_entry("model")],
        "model",
        model_builder=Mock(side_effect=ValueError("test-secret")),
    )
    with pytest.raises(ModelCatalogError) as caught:
        factory()
    assert "test-secret" not in str(caught.value)


@pytest.mark.parametrize("field", ["source", "model_source"])
def test_catalog_login_source_requires_request_authorization(field):
    entry = _entry("login")
    entry[field] = "huawei-maas-login"
    with pytest.raises(ModelCatalogError) as caught:
        catalog_model_factory([entry], "login")
    assert caught.value.code == "ASSESSOR_AUTHORIZATION_REQUIRED"


@pytest.mark.asyncio
async def test_partial_usage_retains_safe_reported_counters_without_fabricating_split():
    response = SimpleNamespace(
        content=RESPONSE,
        usage_metadata={"input_tokens": 5, "total_tokens": 8, "api_key": "test-secret"},
    )
    model = SimpleNamespace(invoke=AsyncMock(return_value=response))
    result = await GoalTranscriptAssessor(
        GoalEvaluator(), model_factory=lambda: model
    ).maybe_assess(_record(), None, "x", is_current=lambda: True)
    assert result.usage is None and not result.usage_available
    assert result.reported_usage == {"input_tokens": 5, "total_tokens": 8}
    assert "test-secret" not in repr(result)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "max_attempts,expected",
    [(None, GoalAssessmentStatus.CONTINUE), (1, GoalAssessmentStatus.BLOCKED)],
)
async def test_core_evaluator_retains_conflict_resolution_and_hard_limits(
    max_attempts, expected
):
    record = _record()
    record.max_attempts = max_attempts
    response = _response()
    response.content = '{"status":"continue","evidence":"artifact still missing"}'
    model = SimpleNamespace(invoke=AsyncMock(return_value=response))
    evaluator = GoalEvaluator()
    report = GoalAssessment(
        status=GoalAssessmentStatus.COMPLETE, evidence="unverified claim"
    )
    result = await GoalTranscriptAssessor(
        evaluator, model_factory=lambda: model
    ).maybe_assess(record, report, "x", is_current=lambda: True)
    assessment = evaluator.assess(
        record, report, transcript_response=result.transcript_response
    )
    assert assessment.status is expected


@pytest.mark.asyncio
@pytest.mark.parametrize("retire_by_cancel", [False, True])
@pytest.mark.parametrize("partial_usage", [False, True])
async def test_usage_callback_preserves_only_old_attempt_counters_before_rejecting_response(
    retire_by_cancel,
    partial_usage,
):
    old_record, replacement_record = _record(), _record()
    current_record = old_record
    entered, release = asyncio.Event(), asyncio.Event()
    buffered = {}
    response = _response()
    if partial_usage:
        response.usage_metadata = UsageMetadata(input_tokens=5, total_tokens=8)

    async def invoke(*args, **kwargs):
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            # Some custom model wrappers consume cancellation before returning.
            pass
        return response

    def preserve_usage(result):
        assert result.transcript_response is None
        buffered.setdefault((old_record.goal_id, old_record.revision), []).append(
            result
        )

    assessor = GoalTranscriptAssessor(
        GoalEvaluator(), model_factory=lambda: SimpleNamespace(invoke=invoke)
    )
    task = asyncio.create_task(
        assessor.maybe_assess(
            old_record,
            None,
            "x",
            is_current=lambda: current_record is old_record,
            on_usage=preserve_usage,
        )
    )
    await entered.wait()
    if retire_by_cancel:
        task.cancel()
    else:
        current_record = replacement_record
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    observed = buffered[(old_record.goal_id, old_record.revision)]
    assert len(observed) == 1
    if partial_usage:
        assert observed[0].usage is None and not observed[0].usage_available
        assert observed[0].reported_usage == {"input_tokens": 5, "total_tokens": 8}
    else:
        assert observed[0].usage_available and observed[0].usage.total_tokens == 8
    # The callback is evidence-only. Neither the retiring nor replacement Goal
    # has been settled or charged by this module.
    assert (
        old_record.token_usage.total_tokens
        == replacement_record.token_usage.total_tokens
        == 0
    )
    assert (replacement_record.goal_id, replacement_record.revision) not in buffered


@pytest.mark.asyncio
async def test_usage_callback_once_for_success_and_never_for_no_response_cancel():
    observer = Mock()
    model = SimpleNamespace(invoke=AsyncMock(return_value=_response()))
    assessor = GoalTranscriptAssessor(GoalEvaluator(), model_factory=lambda: model)
    result = await assessor.maybe_assess(
        _record(), None, "x", is_current=lambda: True, on_usage=observer
    )
    observer.assert_called_once()
    assert observer.call_args.args[0].transcript_response is None
    assert result.transcript_response == RESPONSE
    observer.reset_mock()
    model.invoke.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await assessor.maybe_assess(
            _record(), None, "x", is_current=lambda: True, on_usage=observer
        )
    observer.assert_not_called()
