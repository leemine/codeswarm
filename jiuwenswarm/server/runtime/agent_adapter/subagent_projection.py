# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Shared product projection for Native and External subagent events."""

from __future__ import annotations

import hashlib
import json
import threading
import time
from typing import Any

from openjiuwen.harness.subagent_runtime import (
    SUBAGENT_ACTIVITY_EVENT_TYPE,
    SUBAGENT_MESSAGE_EVENT_TYPE,
    SUBAGENT_UPDATED_EVENT_TYPE,
)

from jiuwenswarm.server.runtime.session.session_history import (
    append_history_record,
    append_history_record_durable,
)

_progress_batches: dict[str, list[str]] = {}
_progress_batches_lock = threading.Lock()
_DURABLE_TRANSCRIPT_EVENT_TYPES = frozenset(
    {
        "chat.ask_user_question",
        "chat.error",
        "chat.final",
        "chat.tool_result",
        "harness.activate_interaction",
    }
)


def _append_subagent_history(
    *,
    durable: bool,
    delivery_id: str,
    **kwargs: Any,
) -> None:
    """Use one history FIFO and wait only for product-critical child records."""

    if not durable:
        append_history_record(delivery_id=delivery_id, **kwargs)
        return
    receipt = append_history_record_durable(delivery_id=delivery_id, **kwargs)
    if receipt is not None:
        # Subagent parsing is routed through run_history_io/run_stream_parser,
        # so this blocks its worker thread rather than the event loop.
        receipt.result(timeout=5.0)


def _legacy_status(projection: dict[str, Any]) -> tuple[str, str]:
    status = str(projection.get("status") or "running")
    message = ""
    if status == "closed":
        if projection.get("closed_reason") == "failed":
            legacy_status = "error"
            error = projection.get("error")
            if isinstance(error, dict):
                message = str(error.get("message") or "")
        else:
            legacy_status = "completed"
    elif status == "idle":
        if projection.get("turn_outcome") == "failed":
            legacy_status = "error"
            error = projection.get("error")
            if isinstance(error, dict):
                message = str(error.get("message") or "")
        else:
            legacy_status = "completed"
    else:
        legacy_status = "starting"
    return legacy_status, message


def _parallel_fields(
    *,
    parent_session_id: str,
    subagent_id: str,
    legacy_status: str,
) -> tuple[int, int, bool]:
    with _progress_batches_lock:
        order = _progress_batches.setdefault(parent_session_id, [])
        if legacy_status in {"completed", "error"}:
            if subagent_id not in order:
                return 0, 1, False
            index = order.index(subagent_id)
            total = max(len(order), 1)
            order.remove(subagent_id)
            if not order:
                _progress_batches.pop(parent_session_id, None)
            return index, total, total > 1
        if subagent_id not in order:
            order.append(subagent_id)
        index = order.index(subagent_id)
        total = len(order)
        return index, total, total > 1


def project_subagent_updated_for_web(projection: dict[str, Any]) -> dict[str, Any]:
    subagent_id = str(projection.get("subagent_id") or "")
    description = (
        str(projection.get("display_name") or "").strip()
        or str(projection.get("task_description") or "").strip()
        or subagent_id
    )
    legacy_status, message = _legacy_status(projection)
    index, total, is_parallel = _parallel_fields(
        parent_session_id=str(projection.get("parent_session_id") or ""),
        subagent_id=subagent_id,
        legacy_status=legacy_status,
    )
    payload = {
        "event_type": "chat.subtask_update",
        **projection,
        "task_id": subagent_id,
        "description": description,
        "legacy_status": legacy_status,
        "index": index,
        "total": total,
        "is_parallel": is_parallel,
    }
    if message:
        payload["message"] = message
    return payload


def persist_subagent_roster_history(
    projection: dict[str, Any],
    web_payload: dict[str, Any],
) -> None:
    parent_session_id = str(projection.get("parent_session_id") or "").strip()
    subagent_id = str(projection.get("subagent_id") or "").strip()
    if not parent_session_id or not subagent_id:
        return
    updated_at_ms = projection.get("updated_at_ms") or projection.get("created_at_ms")
    revision = projection.get("revision")
    legacy_status = str(web_payload.get("legacy_status") or "")
    delivery_id = (
        f"subagent:{subagent_id}:roster:{revision}"
        if revision is not None
        else f"subagent:{subagent_id}:roster:initial"
    )
    _append_subagent_history(
        durable=legacy_status in {"completed", "error"},
        delivery_id=delivery_id,
        session_id=parent_session_id,
        subagent_id=subagent_id,
        request_id=(
            f"subagent-roster-{subagent_id}:{revision}"
            if revision is not None
            else f"subagent-roster-{subagent_id}"
        ),
        channel_id="subagent",
        role="assistant",
        event_type="chat.subtask_update",
        content=str(web_payload.get("description") or subagent_id),
        timestamp=float(updated_at_ms) / 1000 if updated_at_ms else time.time(),
        extra=web_payload,
        mode="subagent",
    )


def persist_subagent_activity(projection: dict[str, Any]) -> None:
    parent_session_id = str(projection.get("parent_session_id") or "").strip()
    subagent_id = str(projection.get("subagent_id") or "").strip()
    if not parent_session_id or not subagent_id:
        return
    task_id = str(projection.get("task_id") or "").strip() or "turn"
    seq = projection.get("seq")
    seq_part = str(seq) if seq is not None else str(
        projection.get("activity_id")
        or projection.get("activityId")
        or projection.get("tool_call_id")
        or projection.get("toolCallId")
        or hashlib.sha256(
            json.dumps(
                projection,
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            ).encode("utf-8")
        ).hexdigest()[:16]
    )
    timestamp_ms = projection.get("at_ms")
    activity = {**projection, "parent_session_id": parent_session_id}
    _append_subagent_history(
        durable=False,
        delivery_id=f"subagent:{subagent_id}:activity:{task_id}:{seq_part}",
        session_id=parent_session_id,
        subagent_id=subagent_id,
        request_id=f"{subagent_id}:activity:{task_id}:{seq_part}",
        channel_id="subagent",
        role="assistant",
        content=str(projection.get("summary") or ""),
        timestamp=float(timestamp_ms) / 1000 if timestamp_ms else time.time(),
        event_type="chat.subagent_activity",
        extra={"subagent_activity": activity},
        mode="subagent",
    )


def persist_subagent_transcript_message(projection: dict[str, Any]) -> None:
    parent_session_id = str(projection.get("parent_session_id") or "").strip()
    subagent_id = str(projection.get("subagent_id") or "").strip()
    if not parent_session_id or not subagent_id:
        return
    seq = projection.get("seq")
    extra: dict[str, Any] = {}
    reasoning = projection.get("reasoning_content")
    if isinstance(reasoning, str) and reasoning.strip():
        extra["reasoning_content"] = reasoning.strip()
    try:
        phase_id = int(projection.get("phase_id") or 0)
    except (TypeError, ValueError):
        phase_id = 0
    if phase_id > 0:
        extra["phase_id"] = phase_id
    nested_extra = projection.get("extra")
    if isinstance(nested_extra, dict):
        extra.update(nested_extra)
    extra["parent_session_id"] = parent_session_id
    timestamp_ms = projection.get("at_ms")
    role = str(projection.get("role") or "assistant")
    event_type = (
        str(projection.get("event_type") or "").strip() or None
        if role == "assistant"
        else None
    )
    transcript_key = str(seq) if seq is not None else hashlib.sha256(
        json.dumps(
            projection,
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        ).encode("utf-8")
    ).hexdigest()[:16]
    _append_subagent_history(
        durable=event_type in _DURABLE_TRANSCRIPT_EVENT_TYPES,
        delivery_id=f"subagent:{subagent_id}:transcript:{transcript_key}",
        session_id=parent_session_id,
        subagent_id=subagent_id,
        request_id=f"{subagent_id}:{seq}" if seq is not None else subagent_id,
        channel_id="subagent",
        role=role,
        content=str(projection.get("content") or ""),
        timestamp=float(timestamp_ms) / 1000 if timestamp_ms else time.time(),
        event_type=event_type,
        extra=extra,
        mode="subagent",
    )


def parse_subagent_stream_chunk(
    chunk: Any,
    *,
    parent_session_id: str | None = None,
) -> dict[str, Any] | None:
    chunk_type = getattr(chunk, "type", None)
    payload = getattr(chunk, "payload", None)
    if chunk_type == SUBAGENT_UPDATED_EVENT_TYPE:
        projection = payload.get("subagent_updated") if isinstance(payload, dict) else None
        if not isinstance(projection, dict):
            return None
        web_payload = project_subagent_updated_for_web(projection)
        persist_subagent_roster_history(projection, web_payload)
        return web_payload
    if chunk_type == SUBAGENT_MESSAGE_EVENT_TYPE:
        projection = payload.get("subagent_message") if isinstance(payload, dict) else None
        if isinstance(projection, dict):
            persist_subagent_transcript_message(projection)
        return None
    if chunk_type == SUBAGENT_ACTIVITY_EVENT_TYPE:
        projection = payload.get("subagent_activity") if isinstance(payload, dict) else None
        if not isinstance(projection, dict):
            return None
        persisted = dict(projection)
        resolved_parent = str(
            persisted.get("parent_session_id") or parent_session_id or ""
        ).strip()
        if resolved_parent:
            persisted["parent_session_id"] = resolved_parent
        persist_subagent_activity(persisted)
        return {"event_type": "chat.subagent_activity", **projection}
    return None


def clear_subagent_progress_batches(parent_session_id: str | None = None) -> None:
    with _progress_batches_lock:
        if parent_session_id is None:
            _progress_batches.clear()
        else:
            _progress_batches.pop(parent_session_id, None)


__all__ = [
    "clear_subagent_progress_batches",
    "parse_subagent_stream_chunk",
    "persist_subagent_activity",
    "persist_subagent_roster_history",
    "persist_subagent_transcript_message",
    "project_subagent_updated_for_web",
]
