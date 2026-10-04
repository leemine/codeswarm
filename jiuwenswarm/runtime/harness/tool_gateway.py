# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Bind existing product tools to one authorized External parent Session."""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel

from openjiuwen.harness.execution_subject import (
    ExecutionSubject,
    execution_subject_scope,
)
from openjiuwen.harness_protocol import (
    ToolDefinition,
    ToolExecutionResult,
    ToolInvocation,
)

logger = logging.getLogger(__name__)


class ProductTool(Protocol):
    """The existing product tool surface consumed without reimplementation."""

    card: Any

    async def invoke(self, inputs: Mapping[str, Any], **kwargs: Any) -> Any:
        ...

    def render_for_llm(self, output: Any) -> str:
        ...


@dataclass(frozen=True, slots=True)
class ProductToolScope:
    """Host-owned routing identity that tool arguments cannot replace."""

    subject_id: str
    host_session_id: str
    workspace: str

    def __post_init__(self) -> None:
        if not self.subject_id or not self.host_session_id:
            raise ValueError("product tool subject and parent session are required")
        workspace = Path(self.workspace)
        if not workspace.is_absolute():
            raise ValueError("product tool workspace must be absolute")
        object.__setattr__(self, "workspace", str(workspace.resolve()))


ToolAdmission = Callable[
    [ProductToolScope, ToolInvocation],
    bool | Awaitable[bool],
]


def _input_schema(tool: ProductTool) -> dict[str, Any]:
    schema = getattr(tool.card, "input_params", {})
    if inspect.isclass(schema) and issubclass(schema, BaseModel):
        schema = schema.model_json_schema()
    if not isinstance(schema, Mapping):
        raise TypeError(f"product tool {tool.card.name!r} has no JSON input schema")
    return dict(schema)


def _mutable_tool_value(value: Any) -> Any:
    """Restore ordinary JSON containers expected by existing product tools."""

    if isinstance(value, Mapping):
        return {str(key): _mutable_tool_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_mutable_tool_value(item) for item in value]
    return value


class ProductToolGateway:
    """Expose one explicit product tool catalog under a fixed parent scope.

    The gateway delegates definitions, invocation, rendering and callbacks to
    the original tool instances.  It owns neither product tool state nor the
    subagent runtime; B2 supplies those objects through the composition root.
    """

    def __init__(
        self,
        tools: Sequence[ProductTool],
        *,
        scope: ProductToolScope,
        invoke_kwargs: Mapping[str, Any] | None = None,
        admit: ToolAdmission | None = None,
    ) -> None:
        catalog: dict[str, ProductTool] = {}
        definitions: list[ToolDefinition] = []
        unsafe_names: set[str] = set()
        for tool in tools:
            card = getattr(tool, "card", None)
            name = str(getattr(card, "name", "") or "").strip()
            if not name:
                raise ValueError("product tools require a named card")
            if name in catalog:
                raise ValueError(f"duplicate product tool name: {name}")
            catalog[name] = tool
            definitions.append(
                ToolDefinition(
                    name=name,
                    description=str(getattr(card, "description", "") or ""),
                    input_schema=_input_schema(tool),
                )
            )
            if not bool(getattr(card, "parallel_safe", True)):
                unsafe_names.add(name)
        if not catalog:
            raise ValueError("product ToolGateway requires at least one tool")
        self._scope = scope
        self._catalog = catalog
        self._definitions = tuple(definitions)
        self._invoke_kwargs = dict(invoke_kwargs or {})
        self._admit = admit
        self._unsafe_names = frozenset(unsafe_names)
        self._unsafe_lock = asyncio.Lock()
        self._required_authority_owner = None

    def bind_required_authority(self, owner) -> None:
        """Require original Provider-call proof for this Session-owned gateway."""
        if owner is None or self._required_authority_owner is not None:
            raise ValueError("product authority must bind once to its owner")
        self._required_authority_owner = owner

    def executor_for(self, invocation: ToolInvocation):
        """Return the exact frozen executor for private final-boundary proof."""
        return self._catalog.get(invocation.name)

    @property
    def scope(self) -> ProductToolScope:
        return self._scope

    @property
    def tool_names(self) -> tuple[str, ...]:
        """Return the frozen authoritative namespace without async discovery."""
        return tuple(self._catalog)

    async def definitions(self) -> tuple[ToolDefinition, ...]:
        return self._definitions

    async def _required_admitted(self, invocation: ToolInvocation) -> bool:
        from jiuwenswarm.governance.product_executor import current_product_executor
        proof = current_product_executor()
        if self._required_authority_owner is not None:
            if (proof is None or proof.gateway is not self
                    or proof.entered
                    or proof.owner is not self._required_authority_owner
                    or proof.invocation is not invocation
                    or proof.executor is not self._catalog.get(invocation.name)):
                return False
            try:
                allowed = await proof.authorize(proof.operation)
            except Exception:
                return False
            if allowed is not True:
                return False
            if (current_product_executor() is not proof
                    or proof.executor is not self._catalog.get(invocation.name)):
                return False
        return True

    async def _is_admitted(self, invocation: ToolInvocation) -> bool:
        if not await self._required_admitted(invocation):
            return False
        if self._admit is None:
            return True
        admitted = self._admit(self._scope, invocation)
        if inspect.isawaitable(admitted):
            admitted = await admitted
        return admitted is True and await self._required_admitted(invocation)

    @staticmethod
    def _denied(invocation: ToolInvocation) -> ToolExecutionResult:
        return ToolExecutionResult(
            content=f"Product tool is not allowed for this Session: {invocation.name}",
            is_error=True,
        )

    async def invoke(self, invocation: ToolInvocation) -> ToolExecutionResult:
        tool = self._catalog.get(invocation.name)
        if tool is None:
            return ToolExecutionResult(
                content=f"Unknown product tool: {invocation.name}",
                is_error=True,
            )
        try:
            subject = ExecutionSubject(
                subject_id=self._scope.subject_id,
                display_name=self._scope.subject_id,
                kind="agent",
                session_id=self._scope.host_session_id,
            )
            with execution_subject_scope(subject):
                if not await self._is_admitted(invocation):
                    return self._denied(invocation)
                if invocation.name in self._unsafe_names:
                    async with self._unsafe_lock:
                        # A queued call must not consume a decision made before
                        # another invocation released the execution lock.
                        if not await self._is_admitted(invocation):
                            return self._denied(invocation)
                        output = await self._invoke_authorized(tool, invocation)
                else:
                    output = await self._invoke_authorized(tool, invocation)
            rendered = tool.render_for_llm(output)
            success = getattr(output, "success", True)
            return ToolExecutionResult(
                content=str(rendered),
                is_error=success is not True,
            )
        except Exception as exc:  # noqa: BLE001 - cross the MCP boundary safely
            if self._required_authority_owner is not None:
                return self._denied(invocation)
            logger.exception(
                "Product tool invocation failed: tool=%s session=%s error=%s",
                invocation.name,
                self._scope.host_session_id,
                type(exc).__name__,
            )
            return ToolExecutionResult(
                content=f"Product tool execution failed: {type(exc).__name__}",
                is_error=True,
            )

    async def _invoke_authorized(self, tool, invocation):
        if self._required_authority_owner is None:
            return await tool.invoke(_mutable_tool_value(invocation.arguments), **self._invoke_kwargs)
        from jiuwenswarm.governance.product_executor import current_product_executor
        proof = current_product_executor()
        if (proof is None or proof.gateway is not self or proof.invocation is not invocation
                or proof.owner is not self._required_authority_owner or proof.executor is not tool
                or proof.entered):
            raise PermissionError('Product executor changed before invocation')
        proof.entered = True
        return await proof.invoke(_mutable_tool_value(invocation.arguments), **proof.kwargs)


__all__ = [
    "ProductToolGateway",
    "ProductToolScope",
    "ToolAdmission",
]
