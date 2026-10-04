"""Metadata-only installation of explicitly host-owned Native MCP tools.

No discovery, legacy config, credential resolution, network, or shared client pool.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass

from openjiuwen.core.foundation.tool.mcp.base import MCPTool, McpToolCard
from openjiuwen.core.runner import Runner
from openjiuwen.core.single_agent.ability_manager import AbilityManager
from openjiuwen.harness.engine import ExecutionBinding
from openjiuwen.harness_protocol import json_value_to_builtin

from jiuwenswarm.governance.native_mcp_tools import (
    NativeMcpClient,
    NativeMcpToolSpec,
    OwnedNativeMcpTool,
    _json,
)
from .governed_http import McpHttpBinding


@dataclass(frozen=True, slots=True)
class NativeMcpRegistration:
    records: tuple[OwnedNativeMcpTool, ...]

    def close(self):
        """Invalidate first; remove only the exact slots this installation owns."""
        for record in self.records:
            record._live[0] = False  # pylint: disable=protected-access
        for record in self.records:
            if record.manager.get(record.alias) is record.card:
                record.manager.remove(record.alias)
            if (
                Runner.resource_mgr.get_tool(record.registry_id, session=None)
                is record.executor
            ):
                Runner.resource_mgr.remove_tool(record.registry_id)


def install_native_mcp_tools(
    *,
    project_id,
    agent,
    session,
    native_session,
    execution_binding,
    connection,
    manifest,
):
    """Install one explicit connection's fixed text tools into one Binding.

    The host owns the manifest and existing resource references. Installation
    conveys no permission; early/final and actual HTTP checks still require the
    original execution's resource and credential authority.
    """
    manager = getattr(agent, "ability_manager", None)
    if (
        type(project_id) is not str
        or not project_id.strip()
        or type(manager) is not AbilityManager
        or type(execution_binding) is not ExecutionBinding
        or execution_binding.provider_id != "native"
        or getattr(getattr(native_session, "engine", None), "binding", None)
        is not execution_binding
        or not callable(getattr(session, "get_session_id", None))
        or type(connection) is not McpHttpBinding
        or type(manifest) is not tuple
        or not manifest
        or any(type(spec) is not NativeMcpToolSpec for spec in manifest)
        or len({spec.remote_name for spec in manifest}) != len(manifest)
    ):
        raise ValueError("explicit owned Native MCP installation required")
    records = []
    nonce = uuid.uuid4().hex
    try:
        for spec in manifest:
            alias = (
                "mcp_"
                + nonce[:16]
                + "_"
                + hashlib.sha256(spec.remote_name.encode()).hexdigest()[:16]
            )
            card = McpToolCard(
                id=alias,
                name=alias,
                description=spec.description,
                stateless=False,
                input_params=json_value_to_builtin(spec.input_schema),
                server_name=connection.connection_id,
                server_id=connection.catalog_revision,
            )
            slot = manager.qualify_tool_id(card, manager._owner_id)  # pylint: disable=protected-access
            if (
                manager.get(alias) is not None
                or Runner.resource_mgr.get_tool(slot, session=None) is not None
            ):
                raise ValueError("Native MCP registration slot already occupied")
            client = NativeMcpClient()
            executor = MCPTool(client, card)
            # Save the intended card ID before add_ability, including partial failures.
            card.id = slot
            record = OwnedNativeMcpTool(
                project_id,
                connection,
                spec,
                execution_binding,
                native_session,
                agent,
                session,
                manager,
                card,
                executor,
                client,
                alias,
                _json(card.model_dump(mode="json")),
                slot,
            )
            client.bind(record)
            records.append(record)
            manager.add_ability(card, executor)
            if not record.is_current():
                raise ValueError("Native MCP registration was not installed exactly")
        return NativeMcpRegistration(tuple(records))
    except BaseException:
        NativeMcpRegistration(tuple(records)).close()
        raise
