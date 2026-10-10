"""Taskboard's business contract; independent of execution state."""

from __future__ import annotations

STATUSES = frozenset({"todo", "doing", "done"})
PRIORITIES = frozenset({"high", "normal", "low"})
FIELDS = frozenset(
    {
        "title",
        "description",
        "status",
        "priority",
        "project_id",
        "linked_session_id",
        "result_note",
    }
)
LIMITS = {
    "title": 120,
    "description": 20000,
    "result_note": 20000,
    "project_id": 200,
    "linked_session_id": 200,
}


class TaskboardError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def validate_patch(value: object) -> dict:
    if not isinstance(value, dict) or not value or set(value) - FIELDS:
        raise TaskboardError("BAD_REQUEST", "invalid task fields")
    result = dict(value)
    for key, val in result.items():
        if key in {"project_id", "linked_session_id"} and val is None:
            continue
        if not isinstance(val, str):
            raise TaskboardError("BAD_REQUEST", f"{key} must be a string")
        if key in LIMITS and len(val) > LIMITS[key]:
            raise TaskboardError("BAD_REQUEST", f"{key} is too long")
        if key == "title":
            if not val.strip():
                raise TaskboardError("BAD_REQUEST", "title is required")
            result[key] = val.strip()
        if key in {"project_id", "linked_session_id"}:
            result[key] = val.strip() or None
    if "status" in result and result["status"] not in STATUSES:
        raise TaskboardError("BAD_REQUEST", "invalid task status")
    if "priority" in result and result["priority"] not in PRIORITIES:
        raise TaskboardError("BAD_REQUEST", "invalid task priority")
    return result
