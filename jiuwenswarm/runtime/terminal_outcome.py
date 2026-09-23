# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Stable product outcomes for terminal harness results."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from openjiuwen.harness_protocol import TurnEventKind

TERMINAL_STATUS_COMPLETED = "completed"
TERMINAL_STATUS_FAILED = "failed"
TERMINAL_STATUS_CANCELLED = "cancelled"
TERMINAL_STATUS_UNKNOWN = "unknown"

EXECUTION_FAILED = "EXECUTION_FAILED"
EXECUTION_CANCELLED = "EXECUTION_CANCELLED"
EXECUTION_TERMINAL_UNKNOWN = "EXECUTION_TERMINAL_UNKNOWN"


def harness_terminal_payload(
    kind: TurnEventKind,
    *,
    error: str | None = None,
    error_code: str | None = None,
    cancellation: str | None = None,
) -> dict[str, Any]:
    """Map one protocol terminal to the legacy chat envelope plus typed status."""

    if kind is TurnEventKind.FINISHED:
        return {
            "event_type": "chat.final",
            "content": "",
            "terminal_status": TERMINAL_STATUS_COMPLETED,
        }
    if kind is TurnEventKind.FAILED:
        return {
            "event_type": "chat.error",
            "error": error or "External execution Turn failed",
            "code": error_code or EXECUTION_FAILED,
            "terminal_status": TERMINAL_STATUS_FAILED,
        }
    if kind is TurnEventKind.ABORTED:
        return {
            "event_type": "chat.error",
            "error": cancellation or "External execution Turn was cancelled",
            "code": EXECUTION_CANCELLED,
            "terminal_status": TERMINAL_STATUS_CANCELLED,
        }
    raise ValueError(f"unsupported harness terminal kind: {kind!r}")


def unknown_terminal_payload() -> dict[str, Any]:
    """Describe an ended output stream without inventing a Provider terminal."""

    return {
        "event_type": "chat.error",
        "error": "External execution stream ended without a terminal result",
        "code": EXECUTION_TERMINAL_UNKNOWN,
        "terminal_status": TERMINAL_STATUS_UNKNOWN,
    }


def payload_terminal_status(payload: object) -> str | None:
    """Return a supported explicit outcome without guessing from text."""

    if not isinstance(payload, Mapping):
        return None
    value = str(payload.get("terminal_status") or "").strip().lower()
    if value in {
        TERMINAL_STATUS_COMPLETED,
        TERMINAL_STATUS_FAILED,
        TERMINAL_STATUS_CANCELLED,
        TERMINAL_STATUS_UNKNOWN,
    }:
        return value
    return None


__all__ = [
    "EXECUTION_CANCELLED",
    "EXECUTION_FAILED",
    "EXECUTION_TERMINAL_UNKNOWN",
    "TERMINAL_STATUS_CANCELLED",
    "TERMINAL_STATUS_COMPLETED",
    "TERMINAL_STATUS_FAILED",
    "TERMINAL_STATUS_UNKNOWN",
    "harness_terminal_payload",
    "payload_terminal_status",
    "unknown_terminal_payload",
]
