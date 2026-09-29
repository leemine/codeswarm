# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Product-owned approval and audit for External Browser actions."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import secrets
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from openjiuwen.harness.tools.browser_move.playwright_runtime import (
    BrowserExecutionIdentity,
)
from openjiuwen.harness_protocol import ToolInvocation

from jiuwenswarm.agents.harness.common.rails.permissions.audit import (
    emit_permission_audit,
)
from jiuwenswarm.agents.harness.common.rails.permissions.persistent_audit import (
    PersistentAuditWriter,
)
from jiuwenswarm.agents.harness.common.rails.permissions.tool_decision_facts import (
    build_tool_decision_facts,
)
from jiuwenswarm.common.runtime_workspace import RuntimeWorkspacePaths
from jiuwenswarm.runtime.harness.recovery_store import SessionExecutionRecovery

BrowserInteractionPublisher = Callable[
    [dict[str, Any], str],
    Awaitable[None],
]

_ALLOW_VALUES = frozenset({"allow", "allow_once", "approve", "approved", "本次允许"})
_AUTO_ALLOWED_TOOLS = frozenset(
    {
        "browser_console_messages",
        "browser_find",
        "browser_get_config",
        "browser_list_custom_actions",
        "browser_probe_cards",
        "browser_probe_interactives",
        "browser_runtime_health",
        "browser_snapshot",
        "browser_verify_element_visible",
        "browser_verify_list_visible",
        "browser_verify_text_visible",
        "browser_verify_value",
    }
)
_MAX_COMPLETED = 512


@dataclass(slots=True)
class _PendingBrowserDecision:
    identity: BrowserExecutionIdentity
    invocation: ToolInvocation
    future: asyncio.Future[bool]


def _invocation_key(
    identity: BrowserExecutionIdentity,
    invocation: ToolInvocation,
) -> str:
    payload = "\0".join(
        (
            identity.task.task_id,
            identity.task.request_id,
            invocation.call_id,
            invocation.name,
        )
    ).encode("utf-8")
    return f"browser-permission-{hashlib.sha256(payload).hexdigest()[:32]}"


def _selected_answer(params: Mapping[str, Any]) -> str:
    answers = params.get("answers")
    if not isinstance(answers, list) or len(answers) != 1:
        return ""
    answer = answers[0]
    if not isinstance(answer, Mapping):
        return ""
    selected = answer.get("selected_options")
    if not isinstance(selected, list) or len(selected) != 1:
        return ""
    return str(selected[0] or "").strip().lower()


class ExternalBrowserAdmission:
    """Await one product interaction before a sensitive Browser tool call.

    Only stable Browser identity and tool names enter the interaction/audit
    record. Tool arguments can contain URLs, credentials, cookies, form values,
    or local paths and therefore never cross this boundary.
    """

    def __init__(
        self,
        *,
        parent_subject_id: str,
        parent_session_id: str,
        runtime_paths: RuntimeWorkspacePaths,
        publish: BrowserInteractionPublisher,
        timeout_s: float = 300.0,
        recovery: SessionExecutionRecovery | None = None,
    ) -> None:
        if not parent_subject_id or not parent_session_id:
            raise ValueError("Browser admission requires parent identity")
        if not callable(publish):
            raise TypeError("Browser interaction publisher must be callable")
        if timeout_s <= 0:
            raise ValueError("Browser permission timeout must be positive")
        self._parent_subject_id = parent_subject_id
        self._parent_session_id = parent_session_id
        self._workspace = runtime_paths.runtime_workspace_root.resolve()
        self._publish = publish
        self._timeout_s = float(timeout_s)
        self._recovery = recovery
        self._call_decisions: dict[str, str] = {}
        self._decision_keys: dict[str, str] = {}
        self._pending: dict[str, _PendingBrowserDecision] = {}
        self._completed: OrderedDict[str, bool] = OrderedDict()
        self._lock = asyncio.Lock()
        self._closed = False

    async def __call__(
        self,
        identity: BrowserExecutionIdentity,
        invocation: ToolInvocation,
    ) -> bool:
        self._validate_scope(identity)
        call_key = _invocation_key(identity, invocation)
        async with self._lock:
            if self._closed:
                return False
            decision_id = self._call_decisions.get(call_key)
            if decision_id is None:
                # Correlation is stable; authorization attempts are not. A new
                # owner or an evicted tombstone must never recycle an old card.
                decision_id = f"browser-permission-{secrets.token_hex(24)}"
                self._call_decisions[call_key] = decision_id
                self._decision_keys[decision_id] = call_key
            completed = self._completed.get(decision_id)
            if completed is not None:
                # Replaying a sensitive tool call can repeat an external side
                # effect. One approval belongs to one invocation only.
                return completed if invocation.name in _AUTO_ALLOWED_TOOLS else False
            pending = self._pending.get(decision_id)
            owner = pending is None
            if pending is not None and invocation.name not in _AUTO_ALLOWED_TOOLS:
                return False
            if pending is None:
                pending = _PendingBrowserDecision(
                    identity=identity,
                    invocation=invocation,
                    future=asyncio.get_running_loop().create_future(),
                )
                self._pending[decision_id] = pending

        if invocation.name in _AUTO_ALLOWED_TOOLS:
            return await self._settle(
                decision_id,
                allowed=True,
                outcome="allow",
                reason="browser_observation_allowed",
            )

        if owner:
            self._audit(
                pending,
                decision_id=decision_id,
                decision="ask",
                reason="browser_manual_approval_required",
                outcome="manual_wait",
                record_kind="browser_permission_pending",
            )
            try:
                if self._recovery is not None:
                    await self._recovery.mark_pending_browser_task(identity.task.task_id)
                await self._publish(
                    self._question_payload(pending, decision_id),
                    f"browser:{identity.task.task_id}:permission:{decision_id}:pending",
                )
            except asyncio.CancelledError:
                await self._settle(
                    decision_id, allowed=False, outcome="cancelled",
                    reason="browser_permission_invocation_cancelled",
                )
                raise
            except Exception:
                await self._settle(
                    decision_id,
                    allowed=False,
                    outcome="deny",
                    reason="browser_permission_delivery_failed",
                    degraded=True,
                )

        try:
            return await asyncio.wait_for(
                asyncio.shield(pending.future),
                timeout=self._timeout_s,
            )
        except TimeoutError:
            await self._settle(
                decision_id,
                allowed=False,
                outcome="timeout",
                reason="browser_permission_timeout",
            )
            return False
        except asyncio.CancelledError:
            await self._settle(
                decision_id,
                allowed=False,
                outcome="cancelled",
                reason="browser_permission_invocation_cancelled",
            )
            raise

    def decision_id_for(
        self,
        identity: BrowserExecutionIdentity,
        invocation: ToolInvocation,
    ) -> str:
        """Return the actual admitted attempt, never synthesize an approval."""

        decision_id = self._call_decisions.get(_invocation_key(identity, invocation))
        if decision_id is None or self._completed.get(decision_id) is not True:
            raise ValueError("Browser invocation has no retained approval")
        return decision_id

    async def release_task(self, identity: BrowserExecutionIdentity) -> None:
        """Clear recovery only after the owner confirms Browser resource exit."""

        self._validate_scope(identity)
        if self._recovery is not None:
            await self._recovery.clear_pending_browser_task(identity.task.task_id)

    async def answer(self, params: Mapping[str, Any]) -> bool:
        """Resolve one exact pending Browser decision; late answers are rejected."""

        decision_id = str(params.get("request_id") or "").strip()
        selected = _selected_answer(params)
        if not decision_id or not selected:
            return False
        async with self._lock:
            pending = self._pending.get(decision_id)
            if pending is None or pending.future.done():
                return False
        allowed = selected in _ALLOW_VALUES
        return await self._settle(
            decision_id,
            allowed=allowed,
            outcome="allow_once" if allowed else "deny",
            reason=(
                "browser_permission_allowed_once"
                if allowed
                else "browser_permission_rejected"
            ),
        )

    async def close(self) -> None:
        """Fail closed every pending decision before Browser child teardown."""

        async with self._lock:
            self._closed = True
            decision_ids = tuple(self._pending)
        for decision_id in decision_ids:
            await self._settle(
                decision_id,
                allowed=False,
                outcome="cancelled",
                reason="browser_permission_owner_closed",
            )

    def _validate_scope(self, identity: BrowserExecutionIdentity) -> None:
        identity.validate_scope(
            owner_subject_id=self._parent_subject_id,
            parent_session_id=self._parent_session_id,
            subagent_id=identity.instance.subagent_id,
            workspace=str(self._workspace),
        )

    async def _settle(
        self,
        decision_id: str,
        *,
        allowed: bool,
        outcome: str,
        reason: str,
        degraded: bool = False,
    ) -> bool:
        async with self._lock:
            pending = self._pending.pop(decision_id, None)
            if pending is None:
                return False
            self._completed[decision_id] = allowed
            self._completed.move_to_end(decision_id)
            while len(self._completed) > _MAX_COMPLETED:
                expired_id, _ = self._completed.popitem(last=False)
                expired_key = self._decision_keys.pop(expired_id)
                self._call_decisions.pop(expired_key, None)
            if not pending.future.done():
                pending.future.set_result(allowed)
        self._audit(
            pending,
            decision_id=decision_id,
            decision="allow" if allowed else "deny",
            reason=reason,
            outcome=outcome,
            record_kind="browser_permission_terminal",
            degraded=degraded,
        )
        return True

    def _audit(
        self,
        pending: _PendingBrowserDecision,
        *,
        decision_id: str,
        decision: str,
        reason: str,
        outcome: str,
        record_kind: str,
        degraded: bool = False,
    ) -> None:
        identity = pending.identity
        invocation = pending.invocation
        facts = build_tool_decision_facts(
            invocation.name,
            {},
            workspace_root=self._workspace,
            original_args_were_valid_object=True,
        )
        audit_root = (
            self._workspace
            / ".jiuwenswarm"
            / "browser-audit"
            / identity.task.task_id
        )
        emit_permission_audit(
            facts,
            decision=decision,
            reason=reason,
            degraded=degraded,
            grant_id=decision_id,
            extra={
                "authorization_outcome": outcome,
                "authorization_stage": "browser_tool_admission",
                "browser_request_id": identity.task.request_id,
                "browser_task_id": identity.task.task_id,
                "decision_source": "browser_product_policy",
                "record_kind": record_kind,
                "tool_call_id": invocation.call_id,
            },
            persistent_writer=PersistentAuditWriter(data_root=audit_root),
        )

    @staticmethod
    def _question_payload(
        pending: _PendingBrowserDecision,
        decision_id: str,
    ) -> dict[str, Any]:
        identity = pending.identity
        tool_name = pending.invocation.name
        return {
            "event_type": "chat.ask_user_question",
            "request_id": decision_id,
            # This is intentionally not permission_interrupt: Browser admission
            # awaits a host future rather than resuming the Provider harness.
            "source": "browser_permission",
            "browser_task_id": identity.task.task_id,
            "browser_request_id": identity.task.request_id,
            "questions": [
                {
                    "question": f"Allow Browser action {tool_name}?",
                    "header": f"Browser: {tool_name}",
                    "card_id": decision_id,
                    "options": [
                        {"label": "本次允许", "value": "allow_once"},
                        {"label": "拒绝", "value": "reject"},
                    ],
                    "tool_name": tool_name,
                    "tool_payload": "[REDACTED]",
                    "reviewer_metadata": {
                        "decision_source": "manual_approval",
                        "final_reviewer_status": "manual",
                        "risk_level": "high",
                    },
                }
            ],
        }


def browser_runtime_enabled(environ: Mapping[str, str]) -> bool:
    """Reuse the existing opt-in Browser runtime contract for External."""

    value = str(
        environ.get("PLAYWRIGHT_RUNTIME_MCP_ENABLED")
        or environ.get("BROWSER_RUNTIME_MCP_ENABLED")
        or ""
    ).strip().lower()
    return value in {"1", "true", "yes", "on"}


async def close_browser_admission(admission: object | None) -> None:
    close = getattr(admission, "close", None)
    if not callable(close):
        return
    result = close()
    if inspect.isawaitable(result):
        await result


__all__ = [
    "ExternalBrowserAdmission",
    "browser_runtime_enabled",
    "close_browser_admission",
]
