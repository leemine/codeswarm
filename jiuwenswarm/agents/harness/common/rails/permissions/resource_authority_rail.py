"""Final Native tool authority, independent of optional permission rails."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from openjiuwen.core.foundation.tool import current_tool_invocation
from openjiuwen.harness.rails.base import DeepAgentRail
from openjiuwen.harness.rails.security.tool_security_rail import PermissionInterruptRail
from openjiuwen.harness.security import ToolPermissionHost
from openjiuwen.harness_protocol import BeforeToolContext, json_value_to_builtin

from jiuwenswarm.governance.tool_context import current_tool_authorizer
from jiuwenswarm.governance.native_executor import native_executor_scope


def _arguments(value: Any) -> dict:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, Mapping):
        raise ValueError("tool arguments must be an object")
    # The protocol validates nested JSON keys/values without coercing them.
    validated = BeforeToolContext("", None, None, "", "", value)
    return json_value_to_builtin(validated.arguments)


def _same_operation(left: BeforeToolContext, right: BeforeToolContext) -> bool:
    return left == right and json.dumps(
        json_value_to_builtin(left.arguments),
        sort_keys=True,
        allow_nan=False,
    ) == json.dumps(
        json_value_to_builtin(right.arguments), sort_keys=True, allow_nan=False
    )


def _operation(ctx: Any) -> BeforeToolContext:
    inputs = ctx.inputs
    call = inputs.tool_call
    if not call or not isinstance(call.name, str) or not call.name:
        raise ValueError("tool call identity is missing")
    if inputs.tool_name != call.name or not isinstance(call.id, str) or not call.id:
        raise ValueError("tool call identity differs from execution")
    actual = _arguments(inputs.tool_args)
    declared = _arguments(call.arguments)
    # Python equality conflates True and 1; compare canonical JSON instead.
    if json.dumps(actual, sort_keys=True) != json.dumps(declared, sort_keys=True):
        raise ValueError("tool arguments differ from execution")
    session = getattr(ctx, "session", None)
    session_id = session.get_session_id() if session is not None else None
    card = getattr(getattr(ctx, "agent", None), "card", None)
    return BeforeToolContext(
        agent_name=str(getattr(card, "name", "") or ""),
        provider_session_id=session_id,
        turn_id=None,
        call_id=call.id,
        tool_name=call.name,
        arguments=actual,
    )


class NativeResourceAuthorityRail(DeepAgentRail):
    """Run after ordinary approval/argument rails, at the invocation boundary.

    Compose the core rail so optional permission-group replacement and child
    filtering cannot mistake this mandatory rail for an optional approval rail.
    No authority means the legacy no-op, including no configuration reads/UI.
    """

    priority = -10000

    def __init__(self) -> None:
        super().__init__()
        self._permission: PermissionInterruptRail | None = None

    async def _authorize(self, incoming: Any) -> bool:
        callback = current_tool_authorizer()
        if callback is None:
            return False  # A bound authority cannot disappear during a check.
        final = current_tool_invocation()
        if final is not None:
            if final.agent_context is not incoming.ctx or not final.is_current():
                return False
            operation = final.operation
            if (
                incoming.tool_call.name != operation.tool_name
                or incoming.tool_call.id != operation.call_id
                or json.dumps(_arguments(incoming.tool_args), sort_keys=True)
                != json.dumps(_arguments(operation.arguments), sort_keys=True)
            ):
                return False
        else:
            # The final bridge creates a new ToolCall carrying transformed args.
            # It must never fall back to a merely registered early executor.
            if incoming.tool_call is not incoming.ctx.inputs.tool_call:
                return False
            operation = _operation(incoming.ctx)
        with native_executor_scope(incoming.ctx, operation) as proof:
            allowed = await callback(operation)
            executor_unchanged = proof is None or proof.is_current()
        # Re-read live inputs after the await, not the approval/UI snapshot.
        return (
            allowed is True
            and executor_unchanged
            and current_tool_authorizer() is callback
            and (
                final.is_current()
                if final is not None
                else _same_operation(_operation(incoming.ctx), operation)
            )
        )

    async def before_tool_call(self, ctx: Any) -> None:
        if current_tool_authorizer() is None or ctx.extra.get("_skip_tool") is True:
            return
        if self._permission is None:
            self._permission = PermissionInterruptRail(
                config={
                    "enabled": True,
                    "defaults": {"*": "allow"},
                    "file_guard": {"enabled": False},
                },
                host=ToolPermissionHost(authorize_tool=self._authorize),
            )
        await self._permission.before_tool_call(ctx)


def ensure_native_tool_authority(rails: list[Any]) -> list[Any]:
    """Return the assembly list with exactly one final authority rail."""
    existing = [rail for rail in rails if isinstance(rail, NativeResourceAuthorityRail)]
    if len(existing) > 1:
        raise ValueError("duplicate mandatory Native authority rails")
    return rails if existing else [*rails, NativeResourceAuthorityRail()]
