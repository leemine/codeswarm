# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Shared Goal control translation; execution, sessions and history stay with the host."""

from __future__ import annotations

from typing import Any

from openjiuwen.harness.goal.schema import GoalOperationError, GoalStatus
from openjiuwen.harness.schema.interaction import InteractionEventType

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod

GOAL_UPDATED_EVENT_TYPE = InteractionEventType.GOAL_UPDATED.value


def wants_attach_goal(params: Any) -> bool:
    return isinstance(params, dict) and params.get("attach_goal") is True


def should_parse_tui_goal_slash(
    *,
    pending_goal_op: dict[str, Any] | None,
    attach_goal_request: bool,
    channel_id: Any,
    query: Any,
) -> bool:
    """Whether to parse chat text ``/goal ...`` (TUI only, when no structured op)."""
    if pending_goal_op is not None or attach_goal_request:
        return False
    if str(channel_id or "").strip().lower() != "tui":
        return False
    return isinstance(query, str)


def parse_goal_slash_intent(query: str) -> dict[str, Any] | None:
    """Parse ``/goal ...`` into an action dict without touching GoalManager."""
    text = query.strip()
    if not text.startswith("/goal"):
        return None
    args = text[5:].strip()
    if not args:
        return {"action": "get"}
    lower = args.lower()
    if lower in {"pause", "resume", "clear"}:
        return {"action": lower}
    if lower.startswith("set "):
        return {"action": "set", "objective": args[4:].strip()}
    if lower == "set":
        return {"action": "set", "objective": ""}
    return {"action": "set", "objective": args}


def structured_goal_operation(
    request: AgentRequest,
) -> dict[str, Any] | None:
    """Map streaming ``command.goal`` set/resume onto the attach→control path.

    Plain chat text like ``/goal set ...`` is never parsed here — only an
    explicit ``command.goal`` method (Web/TUI structured API).
    """
    if request.req_method != ReqMethod.COMMAND_GOAL:
        return None
    raw = request.params if isinstance(request.params, dict) else {}
    action = str(raw.get("action", "get") or "get").strip().lower()
    if action not in {"set", "resume"}:
        return None
    op: dict[str, Any] = {"action": action}
    if action == "set":
        objective = raw.get("objective")
        op["objective"] = objective if isinstance(objective, str) else ""
        op["overwrite_confirmed"] = bool(raw.get("overwrite_confirmed", False))
        for key in ("token_budget", "max_attempts"):
            value = raw.get(key)
            if value is None or isinstance(value, bool):
                continue
            if isinstance(value, int):
                op[key] = value
            elif isinstance(value, str) and value.strip().lstrip("-").isdigit():
                op[key] = int(value.strip())
    return op


def goal_record_payload(record: Any | None) -> dict[str, Any] | None:
    if record is None:
        return None
    to_dict = getattr(record, "to_dict", None)
    return to_dict() if callable(to_dict) else None


def format_goal_control_message(action: str, goal: dict[str, Any] | None) -> str:
    if action == "get":
        return (
            "No goal in this session."
            if goal is None
            else f"Goal: {goal.get('objective', '')}"
        )
    if action == "pause":
        return "Goal paused." if goal is not None else "No goal in this session."
    if action == "clear":
        return "Goal cleared." if goal is None else "Goal was not cleared."
    if action == "resume":
        return "Goal resumed." if goal is not None else "No goal in this session."
    return "Goal set." if goal is not None else "Goal was not set."


def goal_updated_payload(payload: Any) -> dict[str, Any]:
    """Normalize goal updates to the public Web/TUI payload shape."""
    if not isinstance(payload, dict):
        return {"event_type": GOAL_UPDATED_EVENT_TYPE, "goal": None}
    if "goal" in payload:
        return {
            "event_type": GOAL_UPDATED_EVENT_TYPE,
            "goal": payload.get("goal"),
        }
    return {
        "event_type": GOAL_UPDATED_EVENT_TYPE,
        "goal": payload or None,
    }


async def dispatch_goal_control(
    manager: Any | None,
    *,
    action: str,
    objective: str | None = None,
    overwrite_confirmed: bool = False,
    token_budget: int | None = None,
    max_attempts: int | None = None,
) -> dict[str, Any]:
    """Translate controls through the supplied manager without owning execution."""
    normalized_action = action.strip().lower()
    if manager is None:
        return {
            "result_type": "goal_error",
            "action": normalized_action,
            "error_code": "goal_manager_not_started",
            "error": "goal manager is not started",
        }
    try:
        if normalized_action == "get":
            # Read-only status query: use the lock-free ``peek`` snapshot so a
            # long-running / stuck goal round (which holds the shared
            # interaction control lock across ``set``/``clear`` -> abort) can
            # never block a plain ``command.goal get`` up to the unary timeout.
            # ``await get()`` would serialize on that same control lock.
            peek = getattr(manager, "peek", None)
            goal = peek() if callable(peek) else await manager.get()
        elif normalized_action == "set":
            goal = await manager.set(
                objective or "",
                overwrite_confirmed=overwrite_confirmed,
                token_budget=token_budget,
                max_attempts=max_attempts,
            )
        elif normalized_action == "pause":
            before = await manager.get()
            if before is None:
                return {
                    "result_type": "goal_error",
                    "action": normalized_action,
                    "error_code": "no_goal",
                    "error": "No goal in this session; cannot pause.",
                    "goal": None,
                }
            before_status = before.status
            goal = await manager.pause()
            goal_payload = goal_record_payload(goal)
            if before_status is not GoalStatus.ACTIVE:
                status_value = getattr(before_status, "value", str(before_status))
                return {
                    "result_type": "goal_error",
                    "action": normalized_action,
                    "error_code": "invalid_state",
                    "error": (
                        f"Goal is {status_value}; only active goals can be paused."
                    ),
                    "goal": goal_payload,
                }
            return {
                "result_type": "goal_control",
                "action": normalized_action,
                "goal": goal_payload,
                "output": "Goal paused.",
            }
        elif normalized_action == "resume":
            before = await manager.get()
            if before is None:
                return {
                    "result_type": "goal_error",
                    "action": normalized_action,
                    "error_code": "no_goal",
                    "error": "No goal in this session; cannot resume.",
                    "goal": None,
                }
            before_status = before.status
            if before_status is GoalStatus.ACTIVE:
                goal_payload = goal_record_payload(before)
                return {
                    "result_type": "goal_control",
                    "action": normalized_action,
                    "goal": goal_payload,
                    "output": "Goal already active.",
                }
            if before_status not in (GoalStatus.PAUSED, GoalStatus.BLOCKED):
                status_value = getattr(before_status, "value", str(before_status))
                return {
                    "result_type": "goal_error",
                    "action": normalized_action,
                    "error_code": "invalid_state",
                    "error": (
                        f"Goal is {status_value}; only paused/blocked goals "
                        "can be resumed."
                    ),
                    "goal": goal_record_payload(before),
                }
            goal = await manager.resume()
        elif normalized_action == "clear":
            removed = await manager.clear()
            if removed is None:
                return {
                    "result_type": "goal_error",
                    "action": normalized_action,
                    "error_code": "no_goal",
                    "error": "No goal in this session; nothing to clear.",
                    "goal": None,
                    "cleared_goal": None,
                }
            return {
                "result_type": "goal_control",
                "action": normalized_action,
                "goal": None,
                "cleared_goal": goal_record_payload(removed),
                "output": "Goal cleared.",
            }
        else:
            return {
                "result_type": "goal_error",
                "action": normalized_action,
                "error_code": "invalid_action",
                "error": f"unsupported goal action: {action}",
            }
    except GoalOperationError as exc:
        if exc.code == "already_exists":
            return {
                "result_type": "goal_confirm_required",
                "action": normalized_action,
                "error_code": exc.code,
                "error": str(exc),
                "existing_goal": goal_record_payload(exc.goal),
                "requested_objective": objective,
            }
        return {
            "result_type": "goal_error",
            "action": normalized_action,
            "error_code": exc.code,
            "error": str(exc),
            "goal": goal_record_payload(exc.goal),
        }

    goal_payload = goal_record_payload(goal)
    active = goal is not None and goal.status is GoalStatus.ACTIVE
    return {
        "result_type": "goal_stream"
        if normalized_action in {"set", "resume"} and active
        else "goal_control",
        "action": normalized_action,
        "goal": goal_payload,
        "output": format_goal_control_message(normalized_action, goal_payload),
    }


def structured_goal_control_kwargs(params: dict[str, Any] | None) -> dict[str, Any]:
    """Keep unary field coercion distinct from streaming operation parsing."""
    raw = params if isinstance(params, dict) else {}
    objective = raw.get("objective")
    return {
        "action": str(raw.get("action", "get")),
        "objective": objective if isinstance(objective, str) else None,
        "overwrite_confirmed": bool(raw.get("overwrite_confirmed", False)),
        "token_budget": raw.get("token_budget"),
        "max_attempts": raw.get("max_attempts"),
    }
