"""Instance MCP consumers bound to their original Native execution.

Reuse the SDK client, tool and Runtime lifecycle. These checks confer only the
explicit instance owner's MCP capability, never an unrelated project's rights.
"""
import json

from .instance_access import is_instance_owner, require_instance_mcp_access


def shell_environment(adapter):
    from openjiuwen.core.foundation.tool import current_tool_execution

    from jiuwenswarm.server.runtime.mcp.credential import CredentialStore
    from jiuwenswarm.server.runtime.mcp.state_store import list_connected_mcps

    from .organization_auth import configured_authenticator, current_identity
    from .tool_context import current_native_execution_slice

    connected = {row['name'] for row in list_connected_mcps() if row.get('name')}
    if configured_authenticator() is None:
        names = connected  # Original local instance behavior, now child-only.
    else:
        # Ordinary Shell use remains available without any connector credential.
        if not is_instance_owner(current_identity()):
            return {}
        names = set(getattr(adapter, '_session_selected_mcp', ())) & connected
        if not names:
            return {}
        native = getattr(adapter, '_native_execution', None)
        source, bound = current_tool_execution(), current_native_execution_slice()
        if (native is None or source is None or not source.is_current_origin()
                or bound is None or not bound.active or bound.owner is not native
                or native.closed or native.engine.binding.subject_id != current_identity().subject_id
                or native.engine.binding.host_session_id != adapter._parent_session_id
                or native._tool_owner is None
                or source.agent_context.agent is not native._tool_owner[1]
                or source.agent_context.session is not native._tool_owner[2]):
            raise PermissionError('Original owner Shell execution required')
        entry = native._active_entry()
        if entry is None or entry.lifecycle is None:
            raise PermissionError('Original owner Shell request required')
        entry.lifecycle.source._check_current()
        require_instance_mcp_access()
    environment = {}
    store = CredentialStore()
    for name in sorted(names):
        for key, value in store.get_all(name).items():
            if key in environment and environment[key] != value:
                raise PermissionError('Selected connector environments conflict')
            environment[key] = value
    return environment


class InstanceMcpClient:
    """A tool binding over the existing SDK connection, not a connection pool."""

    def __init__(self, *, client, config, identity, adapter, native, agent, session, remote_name):
        self.client, self.config, self.identity = client, config, identity
        self.adapter, self.native, self.agent, self.session = adapter, native, agent, session
        self.remote_name = remote_name
        self.executor = None
        self.card_snapshot = None
        self.live = True

    def current(self):
        from openjiuwen.core.runner import Runner

        from jiuwenswarm.common.config import get_mcp_server_config
        from jiuwenswarm.common.mcp_config import (
            build_mcp_credential_resolver,
            build_mcp_server_config,
        )
        from jiuwenswarm.server.runtime.mcp.state_store import list_connected_mcps

        from .organization_auth import current_identity
        if (not self.live or not is_instance_owner(self.identity) or current_identity() != self.identity
                or self.native.closed or self.adapter._native_execution is not self.native
                or self.native.engine.binding.subject_id != self.identity.subject_id
                or self.config.server_name not in self.adapter._session_selected_mcp
                or self.config.server_name not in {r.get('name') for r in list_connected_mcps()}
                or Runner.resource_mgr._get_mcp_client(self.config.server_id) is not self.client):
            return False
        entry = get_mcp_server_config(self.config.server_name)
        if entry is None:
            return False
        current = build_mcp_server_config(entry, credential_resolver=build_mcp_credential_resolver(self.config.server_name))
        tool = self.executor
        return (current == self.config and tool is not None
                and self.agent.ability_manager.get(tool.card.name) is tool.card
                and Runner.resource_mgr.get_tool(tool.card.id, session=None) is tool
                and tool._mcp_client is self
                and tool.card.model_dump(mode="json") == self.card_snapshot)

    async def call_tool(self, tool_name, arguments):
        from openjiuwen.core.foundation.tool import current_tool_execution
        from openjiuwen.harness_protocol import json_value_to_builtin

        from .native_executor import native_executor_scope
        from .tool_context import current_native_execution_slice
        source, bound = current_tool_execution(), current_native_execution_slice()
        if (source is None or bound is None or not bound.active or bound.owner is not self.native
                or not callable(bound.tool_authorizer) or source.executor is not self.executor
                or source.agent_context.agent is not self.agent or source.agent_context.session is not self.session
                or tool_name != self.executor.card.name
                or json.dumps(arguments, sort_keys=True, allow_nan=False)
                != json.dumps(json_value_to_builtin(source.operation.arguments), sort_keys=True, allow_nan=False)):
            raise PermissionError('Original instance MCP execution required')

        async def check():
            if not source.is_current_origin() or not bound.active or not self.current():
                raise PermissionError('Instance MCP execution unavailable')
            with native_executor_scope(source.agent_context, source.operation):
                if await bound.tool_authorizer(source.operation) is not True:
                    raise PermissionError('Instance MCP execution revoked')
            if not source.is_current_origin() or not bound.active or not self.current():
                raise PermissionError('Instance MCP execution changed')
        await check()
        result = await self.client.call_tool(self.remote_name, arguments)
        await check()
        return result

    def close(self):
        from openjiuwen.core.runner import Runner
        self.live = False
        tool = self.executor
        if tool is not None:
            manager = self.agent.ability_manager
            if manager.get(tool.card.name) is tool.card:
                manager.remove(tool.card.name)
            if Runner.resource_mgr.get_tool(tool.card.id, session=None) is tool:
                Runner.resource_mgr.remove_tool(tool.card.id)


def authorize_instance_mcp(execution, operation, *, current_identity, is_current, owns_session):
    """Return None for unrelated tools; exact owner MCP must pass this policy."""
    from openjiuwen.core.foundation.tool import MCPTool

    from .native_executor import has_native_invoke, require_native_executor
    try:
        proof = require_native_executor(operation)
    except Exception:
        return None
    client = getattr(proof.executor, '_mcp_client', None)
    if type(client) is not InstanceMcpClient:
        return None
    try:
        return (type(proof.executor) is MCPTool and has_native_invoke(proof.executor, MCPTool)
                and client.executor is proof.executor and proof.agent is client.agent
                and proof.session is client.session and client.current()
                and current_identity() == execution.identity == client.identity
                and is_current() is True and owns_session(execution, proof.agent, proof.session) is True
                and execution.provider_id == 'native'
                and client.native.engine.binding.host_session_id == execution.session_id
                and client.native.engine.binding.workspace == execution.workspace)
    except Exception:
        return False


def install_instance_mcp_tools(adapter, native, agent, session, config):
    """Use discovered SDK cards and the existing manager for this exact Session."""
    import uuid

    from openjiuwen.core.foundation.tool import MCPTool
    from openjiuwen.core.foundation.tool.mcp.base import mcp_model_tool_name
    from openjiuwen.core.runner import Runner
    identity = require_instance_mcp_access()
    if identity is None:
        return []
    client = Runner.resource_mgr._get_mcp_client(config.server_id)
    if client is None:
        raise PermissionError('Instance MCP connection unavailable')
    manager = agent.ability_manager
    records = []
    try:
        # Remove the lazy global card path before installing private exact tools.
        manager.remove(config.server_name)
        for tool_id in Runner.resource_mgr.get_mcp_tool_ids(config.server_id):
            original = Runner.resource_mgr.get_tool(tool_id, session=None)
            card = original.card.model_copy(deep=True)
            remote_name = card.name
            card.name = mcp_model_tool_name(config.server_name, remote_name)
            card.id = 'instance-mcp-' + uuid.uuid4().hex
            if manager.get(card.name) is not None:
                raise PermissionError('Instance MCP tool alias collision')
            consumer = InstanceMcpClient(client=client, config=config, identity=identity,
                adapter=adapter, native=native, agent=agent, session=session, remote_name=remote_name)
            executor = MCPTool(consumer, card)
            consumer.executor = executor
            records.append(consumer)
            manager.add_ability(card, executor)
            consumer.card_snapshot = card.model_dump(mode="json")
        return records
    except BaseException:
        for record in reversed(records):
            record.close()
        raise
