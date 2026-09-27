# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Independent no-tool Goal transcript assessment, without execution ownership.

The Runtime producer supplies an authorized model factory and the current-attempt
check. This module never consumes Provider events or changes a GoalRecord. The
returned usage belongs to the caller's current attempt and must be accounted for
alongside Provider usage, once; unavailable usage is not zero usage.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable, Iterable, Mapping
from copy import deepcopy
from dataclasses import dataclass, field, replace
from typing import Any

from openjiuwen.core.foundation.llm import SystemMessage, UserMessage
from openjiuwen.harness.goal.evaluation import GoalEvaluator
from openjiuwen.harness.goal.schema import (
    GoalAssessment,
    GoalAssessmentStatus,
    GoalRecord,
    GoalStopStrategy,
    TokenUsage,
)
from openjiuwen.harness.prompts.sections.goal import (
    TRANSCRIPT_ASSESSOR_SYSTEM,
    build_goal_current_instruction,
    build_transcript_assessor_prompt,
)

from jiuwenswarm.runtime.model_catalog import (
    ModelCatalogError,
    _resolve_configured_model_entry,
    build_model_catalog,
    resolve_model_selection,
)


@dataclass(frozen=True, slots=True)
class TranscriptAssessmentResult:
    """Raw assessor evidence for GoalAttemptDriver; never a completion decision."""

    transcript_response: str | None = None
    usage: TokenUsage | None = None
    usage_available: bool = True
    invoked: bool = False
    error_code: str | None = None
    # Diagnostic evidence only. Missing fields stay absent; this mapping must
    # not be zero-filled and submitted to GoalManager as authoritative usage.
    reported_usage: dict[str, int] = field(default_factory=dict)


def catalog_model_factory(
    entries: Iterable[Mapping[str, Any] | object],
    selection: str,
    *,
    requires_request_authorization: bool = False,
    model_builder: Callable[[dict[str, Any], dict[str, Any]], Any] | None = None,
) -> Callable[[], Any]:
    """Freeze a server-owned catalog selection; never accept request credentials.

    ``entries`` must come from the host's configured model catalog, and selection
    from its authorized request/session resolution. Empty/unknown selections do
    not fall back to defaults. Login models require the host to inject its own
    request-authorized factory instead, preserving the Gateway credential scope.
    Errors intentionally omit model config and builder exception text.
    """
    if requires_request_authorization:
        raise ModelCatalogError(
            "request-authorized assessor model factory is required",
            code="ASSESSOR_AUTHORIZATION_REQUIRED",
        )
    snapshot = deepcopy(tuple(entries))
    selected = resolve_model_selection(build_model_catalog(snapshot), selection)
    if selected.is_agentos:
        raise ModelCatalogError(
            "request-authorized assessor model factory is required",
            code="ASSESSOR_AUTHORIZATION_REQUIRED",
        )
    entry = _resolve_configured_model_entry(snapshot, selected.selection_key)
    if entry is None:
        raise ModelCatalogError(
            "assessor model is unavailable", code="ASSESSOR_MODEL_UNAVAILABLE"
        )
    from jiuwenswarm.common.auth.model_catalog import is_login_model

    if is_login_model(dict(entry)):
        raise ModelCatalogError(
            "request-authorized assessor model factory is required",
            code="ASSESSOR_AUTHORIZATION_REQUIRED",
        )
    client_config = entry.get("model_client_config")
    model_config = entry.get("model_config_obj")
    if not isinstance(client_config, Mapping) or (
        model_config is not None and not isinstance(model_config, Mapping)
    ):
        raise ModelCatalogError(
            "assessor model is unavailable", code="ASSESSOR_MODEL_UNAVAILABLE"
        )

    def create_model() -> Any:
        builder = model_builder
        if builder is None:
            from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
                build_model_from_entry,
            )

            builder = build_model_from_entry
        try:
            # Give each construction a fresh copy; a builder must not mutate the
            # snapshot or a later request's endpoint/reasoning configuration.
            return builder(
                deepcopy(dict(client_config)), deepcopy(dict(model_config or {}))
            )
        except Exception:
            raise ModelCatalogError(
                "assessor model construction failed", code="ASSESSOR_MODEL_UNAVAILABLE"
            ) from None

    return create_model


def _usage_fields(response: Any) -> Mapping[str, Any]:
    raw = getattr(response, "usage_metadata", None)
    if isinstance(raw, Mapping):
        return raw
    if callable(getattr(raw, "model_dump", None)):
        # UsageMetadata has zero defaults. Omitted fields must remain missing,
        # rather than treating a total-only response as zero input/output.
        return raw.model_dump(exclude_unset=True)
    return {}


def _response_usage(fields: Mapping[str, Any]) -> TokenUsage | None:
    inputs, outputs = fields.get("input_tokens"), fields.get("output_tokens")
    if any(type(value) is not int or value < 0 for value in (inputs, outputs)):
        return None
    total = fields.get("total_tokens")
    if total is not None and (type(total) is not int or total != inputs + outputs):
        return None
    cached = fields.get("cache_read_tokens")
    if cached is None:
        cached = fields.get("cache_tokens", 0)
    if type(cached) is not int or not 0 <= cached <= inputs:
        return None
    return TokenUsage(
        input_tokens=inputs,
        output_tokens=outputs,
        cached_input_tokens=cached,
        total_tokens=inputs + outputs,
    )


def _response_result(response: Any) -> TranscriptAssessmentResult:
    content = getattr(response, "content", None)
    fields = _usage_fields(response)
    usage = _response_usage(fields)
    reported_usage = {
        key: fields[key]
        for key in (
            "input_tokens",
            "output_tokens",
            "total_tokens",
            "cache_tokens",
            "cache_read_tokens",
        )
        if type(fields.get(key)) is int and fields[key] >= 0
    }
    return TranscriptAssessmentResult(
        transcript_response=content if isinstance(content, str) else None,
        usage=usage,
        usage_available=usage is not None,
        invoked=True,
        reported_usage=reported_usage,
        error_code=(
            "ASSESSOR_USAGE_UNAVAILABLE"
            if usage is None
            else "ASSESSOR_RESPONSE_INVALID"
            if not isinstance(content, str)
            else None
        ),
    )


class GoalTranscriptAssessor:
    """Apply Native assessment invocation policy using a separately built Model.

    The supplied factory may be async, but must already enforce server/request
    authorization. No execution Provider model name or raw credential payload is
    interpreted here. GoalEvaluator/GoalAttemptDriver remain the decision makers.
    """

    def __init__(
        self,
        evaluator: GoalEvaluator,
        *,
        model_factory: Callable[[], Any],
        language: str = "cn",
        timeout_seconds: float = 120.0,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("assessor timeout must be positive")
        self._evaluator = evaluator
        self._model_factory = model_factory
        self._language = language if language in TRANSCRIPT_ASSESSOR_SYSTEM else "cn"
        self._timeout_seconds = timeout_seconds

    def _needs_transcript(
        self, record: GoalRecord, report: GoalAssessment | None
    ) -> bool:
        strategy = self._evaluator.strategy
        if strategy is GoalStopStrategy.AGENT_REPORT:
            return False
        if strategy is GoalStopStrategy.TRANSCRIPT or report is None:
            return True
        if report.status in (
            GoalAssessmentStatus.COMPLETE,
            GoalAssessmentStatus.BLOCKED,
        ):
            return True
        try:
            return bool(self._evaluator.should_spot_check(record))
        except Exception:
            return False

    async def maybe_assess(
        self,
        record: GoalRecord,
        agent_report: GoalAssessment | None,
        transcript: str,
        *,
        is_current: Callable[[], bool],
        on_usage: Callable[[TranscriptAssessmentResult], None] | None = None,
    ) -> TranscriptAssessmentResult:
        """Return current assessment; optionally preserve usage before stale checks.

        ``on_usage`` is synchronous and must only buffer safe counters in the
        original attempt evidence. It must not call GoalManager, await IO, or
        settle an assessment. It receives no transcript response, and may run
        after cancellation/identity retirement so spent usage is not lost.
        """
        task = asyncio.current_task()
        initial_cancels = task.cancelling() if task is not None else 0

        def check_current() -> None:
            # Also reject a custom Model that swallows task cancellation and
            # eventually returns a response. Such a response cannot settle work.
            if not is_current() or (
                task is not None and task.cancelling() > initial_cancels
            ):
                raise asyncio.CancelledError(
                    "goal assessment attempt is no longer current"
                )

        check_current()
        if not self._needs_transcript(record, agent_report):
            return TranscriptAssessmentResult()
        language = self._language
        messages = [
            SystemMessage(content=TRANSCRIPT_ASSESSOR_SYSTEM[language]),
            UserMessage(
                content=build_transcript_assessor_prompt(
                    record.objective,
                    build_goal_current_instruction(record, language),
                    transcript,
                    language,
                )
            ),
        ]
        invoked = False
        try:
            async with asyncio.timeout(self._timeout_seconds):
                model = self._model_factory()
                if inspect.isawaitable(model):
                    model = await model
                check_current()
                if not callable(getattr(model, "invoke", None)):
                    return TranscriptAssessmentResult(
                        error_code="ASSESSOR_MODEL_UNAVAILABLE"
                    )
                invoked = True
                # No TypeError retry: a failed custom invoke may already have
                # incurred usage. One attempt owns exactly one assessor call.
                response = await model.invoke(
                    messages, tools=[], temperature=0.0, top_p=1.0
                )
                result = _response_result(response)
                if on_usage is not None:
                    on_usage(replace(result, transcript_response=None))
                check_current()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            check_current()
            return TranscriptAssessmentResult(
                invoked=invoked,
                usage_available=not invoked,
                error_code=(
                    "ASSESSOR_TIMEOUT"
                    if isinstance(exc, TimeoutError)
                    else "ASSESSOR_INVOCATION_FAILED"
                    if invoked
                    else "ASSESSOR_MODEL_UNAVAILABLE"
                ),
            )
        check_current()
        return result
