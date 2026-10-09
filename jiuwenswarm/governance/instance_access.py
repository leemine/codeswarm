"""Policy for existing installation-wide resources, independent of sharing.

One explicit host owner uses the existing instance stores and connections.
This is not a second identity provider or a per-user MCP implementation.
"""
from dataclasses import asdict

from .contracts import TrustedIdentity

MCP_OWNER_METHODS = frozenset({
    'mcp.install', 'mcp.uninstall', 'mcp.connect', 'mcp.wait_auth',
    'mcp.disconnect', 'mcp.register_custom', 'mcp.delete_custom', 'mcp.save_credentials',
})


def is_instance_owner(identity):
    """Require the complete configured identity, never infer owner from login."""
    from .organization_auth import configured_authenticator
    try:
        auth = configured_authenticator()
        return (isinstance(identity, TrustedIdentity) and auth is not None
                and auth.known_actor(identity)
                and auth._config().get('instance_owner') == asdict(identity))
    except Exception:
        return False


def require_instance_mcp_access():
    """Admit a current instance owner; retain legacy local operation unchanged."""
    from .organization_auth import configured_authenticator, current_identity
    try:
        if configured_authenticator() is None:
            return None
        identity = current_identity()
        if is_instance_owner(identity):
            return identity
    except Exception:
        pass
    raise PermissionError('organization-scoped MCP authorization required')


def capture_instance_mcp_check():
    """Keep the original credential's liveness across an existing async flow."""
    from .organization_auth import (
        configured_authenticator,
        current_identity,
        current_principal,
    )
    identity = require_instance_mcp_access()
    principal = current_principal()

    def check():
        if identity is None:
            if configured_authenticator() is not None:
                raise PermissionError('MCP host identity changed')
            return
        if (principal is None or principal.identity() != identity
                or current_identity() != identity or not is_instance_owner(identity)):
            raise PermissionError('MCP owner authority unavailable')
    check()
    return check


def authorize_native_catalog(execution, operation, *, current_identity, is_current, owns_session):
    """Admit the original Session's metadata search/dispatcher, not its targets.

    ProgressiveToolRail dispatches each selected target through AbilityManager
    and the same final resource rail. Search visibility grants no tool access.
    """
    from openjiuwen.harness.rails.progressive_tool_rail import ProgressiveToolRail
    from openjiuwen.harness.tools.tool_discovery import ToolCallTool, ToolSearchTool

    from .native_executor import has_native_invoke, require_native_executor

    if operation.tool_name not in {'tool_search', 'tool_call'}:
        return None
    try:
        proof = require_native_executor(operation)
        expected, attribute, method = (
            (ToolSearchTool, '_search_tools', ProgressiveToolRail._search_tools)
            if operation.tool_name == 'tool_search' else
            (ToolCallTool, '_call_tool', ProgressiveToolRail._call_discovered_tool))
        callback = getattr(proof.executor, attribute, None)
        owner = getattr(callback, '__self__', None)
        return (type(proof.executor) is expected and has_native_invoke(proof.executor, expected)
                and type(owner) is ProgressiveToolRail
                and getattr(callback, '__func__', None) is method
                and owner._tool_search_registry is proof.manager
                and owner._owned_tool_cards.get(operation.tool_name) is proof.card
                and execution.provider_id == 'native'
                and current_identity() == execution.identity and is_current() is True
                and proof.session.get_session_id() == execution.session_id
                and operation.provider_session_id == execution.session_id
                and owns_session(execution, proof.agent, proof.session) is True
                and proof.is_current())
    except Exception:
        return False
