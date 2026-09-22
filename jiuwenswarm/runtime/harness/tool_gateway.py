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

    @property
    def scope(self) -> ProductToolScope:
        return self._scope

    async def definitions(self) -> tuple[ToolDefinition, ...]:
        return self._definitions

    async def invoke(self, invocation: ToolInvocation) -> ToolExecutionResult:
        tool = self._catalog.get(invocation.name)
        if tool is None:
            return ToolExecutionResult(
                content=f"Unknown product tool: {invocation.name}",
                is_error=True,
            )
        if self._admit is not None:
            admitted = self._admit(self._scope, invocation)
            if inspect.isawaitable(admitted):
                admitted = await admitted
            if admitted is not True:
                return ToolExecutionResult(
                    content=f"Product tool is not allowed for this Session: {invocation.name}",
                    is_error=True,
                )
        try:
            if invocation.name in self._unsafe_names:
                async with self._unsafe_lock:
                    output = await tool.invoke(
                        dict(invocation.arguments),
                        **self._invoke_kwargs,
                    )
            else:
                output = await tool.invoke(
                    dict(invocation.arguments),
                    **self._invoke_kwargs,
                )
            rendered = tool.render_for_llm(output)
            success = getattr(output, "success", True)
            return ToolExecutionResult(
                content=str(rendered),
                is_error=success is not True,
            )
        except Exception as exc:  # noqa: BLE001 - cross the MCP boundary safely
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


__all__ = [
    "ProductToolGateway",
    "ProductToolScope",
    "ToolAdmission",
]
