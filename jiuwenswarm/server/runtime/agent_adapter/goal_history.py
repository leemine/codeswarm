# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Goal messages on the existing product history writer and wire format."""
from __future__ import annotations

import json
import time
from typing import Any

from jiuwenswarm.server.runtime.session.history_io import run_history_io
from jiuwenswarm.server.runtime.session.session_history import (
    append_history_record_durable,
    load_history_records,
    wait_for_history_receipt,
)

# Native and External share the existing pending objective slot per session.
pending_goal_objective_history: dict[str, dict[str, Any]] = {}


def objective_record(request, *, action, result_type, goal_payload) -> dict[str, Any] | None:
    if str(action or "").strip().lower() != "set":
        return None
    if result_type in {"goal_error", "goal_confirm_required", None}:
        return None
    if not isinstance(goal_payload, dict):
        return None
    objective = str(goal_payload.get("objective") or "").strip()
    if not objective:
        return None
    params = request.params if isinstance(request.params, dict) else {}
    return {
        "session_id": request.session_id or "default",
        "request_id": request.request_id,
        "channel_id": request.channel_id,
        "role": "user",
        "content": objective,
        "channel_metadata": request.metadata,
        "mode": params.get("mode", "unknown"),
        "extra": {
            "goal_id": str(goal_payload.get("goal_id") or "").strip() or None,
            "is_goal_objective_message": True,
        },
    }


def completion_record(*, session_id, channel_id, channel_metadata, mode, goal_payload):
    if not isinstance(goal_payload, dict):
        return None
    status = goal_payload.get("status")
    if str(getattr(status, "value", status) or "").strip().lower() != "completed":
        return None
    goal_id = str(goal_payload.get("goal_id") or "").strip()
    if not goal_id:
        return None
    assessment = goal_payload.get("last_assessment")
    evidence = str(assessment.get("evidence") or "").strip() if isinstance(assessment, dict) else ""
    message_id = f"goal-completed-{goal_id}"
    return {
        "session_id": (session_id or "default").strip() or "default",
        "request_id": message_id,
        "channel_id": channel_id,
        "role": "assistant",
        "content": "goal.completed:" + json.dumps({"evidence": evidence}, ensure_ascii=False),
        "channel_metadata": channel_metadata,
        "mode": mode,
        "extra": {
            "id": message_id,
            "goal_id": goal_id,
            "is_goal_completed_message": True,
            "evidence": evidence,
        },
    }


async def _write(record, delivery_id):
    receipt = await run_history_io(
        append_history_record_durable, timestamp=time.time(),
        delivery_id=delivery_id, **record,
    )
    if receipt is not None:
        await wait_for_history_receipt(receipt)


async def record_goal_set(request, *, action, result_type, goal_payload, defer=False):
    record = objective_record(
        request, action=action, result_type=result_type, goal_payload=goal_payload,
    )
    if record is None:
        return
    sid = record["session_id"]
    pending_goal_objective_history[sid] = record
    if not defer:
        await flush_goal_set(sid)


async def flush_goal_set(session_id):
    record = pending_goal_objective_history.get(session_id)
    if record is None:
        return
    goal_id = record["extra"].get("goal_id") or record["request_id"]
    await _write(record, f"goal-objective-{goal_id}")
    if pending_goal_objective_history.get(session_id) is record:
        pending_goal_objective_history.pop(session_id, None)


async def record_goal_completed(*, session_id, channel_id, channel_metadata, mode, goal_payload):
    record = completion_record(
        session_id=session_id, channel_id=channel_id,
        channel_metadata=channel_metadata, mode=mode, goal_payload=goal_payload,
    )
    if record is not None:
        # Old Native archives predate delivery keys but already have the same
        # stable completion-card id. Preserve their replay deduplication too.
        rows = await run_history_io(load_history_records, record["session_id"])
        if any(
            isinstance(row, dict) and (
                row.get("id") == record["request_id"]
                or (row.get("is_goal_completed_message") and row.get("goal_id") == record["extra"]["goal_id"])
            )
            for row in rows
        ):
            return
        await _write(record, record["request_id"])
