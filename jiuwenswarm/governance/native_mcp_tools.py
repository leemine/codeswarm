"""Exact owned Native MCP executors for the host-declared text-only contract.

These records map requirements; they do not grant resources or discover servers.
The private consumer retains one actual Tool execution and its original host slice.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from openjiuwen.core.foundation.tool import ToolExecution, current_tool_execution
from openjiuwen.core.foundation.tool.mcp.base import (
    MCPTool,
    extract_mcp_tool_result_content,
)
from openjiuwen.core.runner import Runner
from openjiuwen.harness.engine import ExecutionBinding
from openjiuwen.harness.execution_subject import current_execution_subject
from openjiuwen.harness_protocol import (
    BeforeToolContext,
    freeze_json_object,
    json_value_to_builtin,
)

from jiuwenswarm.server.runtime.mcp.governed_http import (
    McpConsumptionDenied,
    McpHttpBinding,
    McpRequestTarget,
    invoke_mcp_tool,
)
from .credential_resources import BoundCredentialAuthority
from .native_executor import has_native_invoke, require_native_executor
from .resources import ResourceDefinition, ResourceRequest
from .tool_context import current_native_execution_slice
from .tool_resources import ResourceExecutionContext, ToolResourceUse

_TEXT_SCHEMA = {
    "type": "object",
    "properties": {"text": {"type": "string"}},
    "required": ["text"],
    "additionalProperties": False,
}


def _json(value):
    return json.dumps(
        json_value_to_builtin(value),
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


@dataclass(frozen=True, slots=True)
class NativeMcpToolSpec:
    remote_name: str
    description: str
    input_schema: Mapping
    tool_resource: ResourceDefinition
    contract: str = "stateless-text-v1"

    def __post_init__(self):
        if (
            type(self.remote_name) is not str
            or not self.remote_name.strip()
            or type(self.description) is not str
            or self.contract != "stateless-text-v1"
            or type(self.tool_resource) is not ResourceDefinition
            or self.tool_resource.kind != "tool"
        ):
            raise ValueError("explicit stateless text MCP manifest required")
        schema = freeze_json_object(self.input_schema)
        if _json(schema) != _json(_TEXT_SCHEMA):
            raise ValueError("unsupported MCP resource contract schema")
        object.__setattr__(self, "input_schema", schema)


@dataclass(frozen=True, slots=True)
class McpOperationAuthority:
    credential_authority: BoundCredentialAuthority = field(repr=False)
    admit_actual_request: Callable[[McpRequestTarget], bool] = field(repr=False)
    is_current: Callable[[], bool] = field(repr=False)

    def __post_init__(self):
        if (
            type(self.credential_authority) is not BoundCredentialAuthority
            or not callable(self.admit_actual_request)
            or not callable(self.is_current)
        ):
            raise TypeError("fixed MCP operation authority required")


@dataclass(frozen=True, slots=True)
class OwnedNativeMcpTool:
    project_id: str
    connection: McpHttpBinding
    spec: NativeMcpToolSpec
    execution_binding: ExecutionBinding
    native_session: Any = field(repr=False)
    agent: Any = field(repr=False)
    session: Any = field(repr=False)
    manager: Any = field(repr=False)
    card: Any = field(repr=False)
    executor: MCPTool = field(repr=False)
    client: Any = field(repr=False)
    alias: str
    card_snapshot: str = field(repr=False)
    registry_id: str
    _live: list[bool] = field(default_factory=lambda: [True], repr=False, compare=False)

    def is_current(self) -> bool:
        try:
            return (
                self._live[0]
                and self.native_session.engine.binding is self.execution_binding
                and self.agent.ability_manager is self.manager
                and self.manager.get(self.alias) is self.card
                and self.card.id == self.registry_id
                and self.card.name == self.alias
                and _json(self.card.model_dump(mode="json")) == self.card_snapshot
                and self.executor.card is self.card
                and type(self.executor) is MCPTool
                and self.executor._mcp_client is self.client  # pylint: disable=protected-access
                and type(self.client) is NativeMcpClient
                and self.client.record is self
                and Runner.resource_mgr.get_tool(self.registry_id, session=None)
                is self.executor
            )
        except Exception:
            return False

    def matches(
        self, execution: ResourceExecutionContext, operation: BeforeToolContext
    ) -> bool:
        try:
            binding = self.execution_binding
            return (
                self.is_current()
                and execution.project_id == self.project_id
                and execution.provider_id == binding.provider_id == "native"
                and execution.session_id == binding.host_session_id
                and operation.provider_session_id == self.session.get_session_id()
                and operation.tool_name == self.alias
                and execution.identity.subject_id == binding.subject_id
                and str(Path(execution.workspace).resolve()) == binding.workspace
                and self.native_session.owns_tool_session(
                    execution, self.agent, self.session
                )
                is True
            )
        except Exception:
            return False


def _requirements(record, operation):
    if (
        record.spec.contract != "stateless-text-v1"
        or set(operation.arguments) != {"text"}
        or type(operation.arguments["text"]) is not str
    ):
        raise McpConsumptionDenied("unsupported actual MCP arguments")
    resource, credential = record.spec.tool_resource, record.connection.credential_use
    return (
        ToolResourceUse(
            ResourceRequest(resource.resource_id, "invoke"), resource.reference
        ),
        ToolResourceUse(
            ResourceRequest(credential.resource_id, "use"), credential.reference
        ),
    )


def _record(executor):
    client = getattr(executor, "_mcp_client", None)
    record = client.record if type(client) is NativeMcpClient else None
    if (
        type(record) is not OwnedNativeMcpTool
        or record.executor is not executor
        or not record.is_current()
    ):
        raise McpConsumptionDenied("owned MCP executor is unavailable")
    return record


def native_mcp_resources(execution, operation):
    """Early/final rail mapping; an exact live Native proof is mandatory."""
    proof = require_native_executor(operation)
    record = _record(proof.executor)
    if (
        not has_native_invoke(proof.executor, MCPTool)
        or proof.agent is not record.agent
        or proof.session is not record.session
        or proof.manager is not record.manager
        or proof.card is not record.card
        or not record.matches(execution, operation)
    ):
        raise McpConsumptionDenied("Native MCP ownership is unavailable")
    uses = _requirements(record, operation)
    if not proof.is_current() or not record.matches(execution, operation):
        raise McpConsumptionDenied("Native MCP execution changed")
    return uses


def actual_mcp_resources(execution, operation, *, executor_binding, source_execution):
    """Map post-parse arguments using a captured actual-method certificate."""
    record, source = executor_binding, source_execution
    if (
        type(record) is not OwnedNativeMcpTool
        or type(source) is not ToolExecution
        or not source.is_current_origin()
        or source.executor is not record.executor
        or source.agent_context.agent is not record.agent
        or source.agent_context.session is not record.session
        or getattr(source.original_invoke, "__self__", None) is not record.executor
        or getattr(source.original_invoke, "__func__", None) is not MCPTool.invoke
        or replace(operation, arguments=source.operation.arguments) != source.operation
        or not record.matches(execution, operation)
    ):
        raise McpConsumptionDenied("actual MCP origin is unavailable")
    uses = _requirements(record, operation)
    if not source.is_current_origin() or not record.matches(execution, operation):
        raise McpConsumptionDenied("actual MCP origin changed")
    return uses


class NativeMcpClient:
    """Private adapter, never registered in the legacy MCP client pool."""

    def __init__(self):
        self._record = None

    @property
    def record(self):
        return self._record

    def bind(self, record):
        if (
            self._record is not None
            or type(record) is not OwnedNativeMcpTool
            or record.client is not self
        ):
            raise ValueError("MCP client already bound or foreign record")
        self._record = record

    async def call_tool(self, tool_name, arguments):
        cancelled = False
        try:
            return await self._call(tool_name, arguments)
        except asyncio.CancelledError:
            cancelled = True
        except Exception:
            pass
        # Never expose a host factory/resolver's diagnostics to MCPTool history.
        if cancelled:
            raise asyncio.CancelledError()
        raise McpConsumptionDenied("Native MCP execution denied")

    async def _call(self, tool_name, arguments):
        source = current_tool_execution()
        record = self.record
        bound = current_native_execution_slice()
        if (
            type(record) is not OwnedNativeMcpTool
            or type(source) is not ToolExecution
            or not source.is_current()
            or source.executor is not record.executor
            or tool_name != record.alias
            or bound is None
            or source.agent_context.agent is not record.agent
            or source.agent_context.session is not record.session
            or getattr(source.original_invoke, "__self__", None) is not record.executor
            or getattr(source.original_invoke, "__func__", None) is not MCPTool.invoke
        ):
            raise McpConsumptionDenied("actual Native MCP execution required")
        operation = replace(source.operation, arguments=arguments)
        _requirements(record, operation)
        factory = getattr(bound, "mcp_authorizer", None)

        def current_origin():
            return (
                source.is_current_origin()
                and record.is_current()
                and current_native_execution_slice() is bound
                and bound.active
                and bound.owner is record.native_session
                and bound.subject == current_execution_subject()
                and (bound.subject is None or bound.subject.kind != "subagent")
                and getattr(bound, "mcp_authorizer", None) is factory
            )

        if not callable(factory) or not current_origin():
            raise McpConsumptionDenied("fixed Native MCP authority required")
        authority = factory(
            record.connection,
            executor_binding=record,
            actual_operation=operation,
            source_execution=source,
            execution_slice=bound,
            native_session=record.native_session,
        )
        if (
            type(authority) is not McpOperationAuthority
            or not source.is_current()
            or not current_origin()
        ):
            raise McpConsumptionDenied("Native MCP authority changed")
        execution = authority.credential_authority.execution
        actual_mcp_resources(
            execution, operation, executor_binding=record, source_execution=source
        )

        def current():
            return current_origin() and authority.is_current() is True

        def admit(target):
            if not current():
                return False
            before = actual_mcp_resources(
                execution, operation, executor_binding=record, source_execution=source
            )
            allowed = authority.admit_actual_request(target)
            after = actual_mcp_resources(
                execution, operation, executor_binding=record, source_execution=source
            )
            return allowed is True and before == after and current()

        result = await invoke_mcp_tool(
            record.connection,
            tool_name=record.spec.remote_name,
            arguments=json_value_to_builtin(operation.arguments),
            credential_authority=authority.credential_authority,
            admit_actual_request=admit,
            is_current=current,
        )
        if not source.is_current() or not current():
            raise McpConsumptionDenied("Native MCP execution expired")
        return extract_mcp_tool_result_content(result.result, tool_name=record.alias)
