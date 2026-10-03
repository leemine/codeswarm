"""Resource requirements proven by the pinned OpenCode permission boundary.

Only bash is supported, with an explicit whole OS-user process grant. Permission
patterns are summaries, never original tool inputs or a path sandbox. Read lacks
a trusted worktree root; edit reads old content before asking; search/network and
plugin-defined executors have no supported resource proof here and are denied.
"""

from collections.abc import Callable, Mapping
from types import MappingProxyType
from typing import Any

from openjiuwen.harness_protocol import BeforeToolContext

from .resources import ResourceDefinition, ResourceRequest
from .tool_resources import ResourceExecutionContext, ToolResourceUse


class OpenCodeToolResourceResolver:
    """Map an owned, governed Provider Session to fixed host resource references.

    ``owns_session`` must verify the exact Provider-issued Session against the
    private execution and its admitted CLI/config/plugin sources. Host Session
    IDs differ from OpenCode IDs. A name, prefix or client assertion is no proof.
    This resolver neither starts a Provider nor grants resources. Its caller must
    use BoundToolResourceAuthority so current grants are checked on every ask,
    including after approval waits. The core governed mode must remain enabled.
    """

    def __init__(
        self,
        grants: Mapping[str, Any],
        *,
        owns_session: Callable[[ResourceExecutionContext, str], bool],
    ):
        if not callable(owns_session):
            raise TypeError("exact governed Provider Session ownership is required")
        catalog = {}
        rows = grants.get("resources")
        if not isinstance(rows, list):
            raise ValueError("host resource metadata is required")
        for row in rows:
            if not isinstance(row, Mapping):
                raise ValueError("invalid resource metadata")
            definition = ResourceDefinition(
                row.get("resource_id"), row.get("kind"), row.get("reference")
            )
            key = (definition.kind, definition.reference)
            if key in catalog and catalog[key] != definition.resource_id:
                raise ValueError("ambiguous host resource reference")
            catalog[key] = definition.resource_id
        self._catalog = MappingProxyType(catalog)
        self._owns_session = owns_session

    def resources_for_tool(
        self, execution: ResourceExecutionContext, tool: BeforeToolContext
    ) -> tuple[ToolResourceUse, ...]:
        if (
            execution.provider_id != "opencode"
            or not isinstance(tool, BeforeToolContext)
            or not tool.provider_session_id
            or not tool.turn_id
            or not tool.call_id
            or self._owns_session(execution, tool.provider_session_id) is not True
        ):
            raise ValueError("OpenCode operation has no matching governed Session")
        args = tool.arguments
        metadata = args.get("metadata")
        patterns = args.get("patterns")
        if (
            tool.tool_name != "bash"
            or set(args) != {"metadata", "patterns"}
            or not isinstance(metadata, Mapping)
            or set(metadata) != {"command"}
            or not isinstance(metadata["command"], str)
            or not metadata["command"].strip()
            or "\x00" in metadata["command"]
            or not isinstance(patterns, tuple)
            or not patterns
            or any(not isinstance(item, str) or not item for item in patterns)
        ):
            raise ValueError("unsupported OpenCode permission operation")
        # The command may access ambient files, network and environment. There
        # is deliberately no command parser or workspace-path inference here.
        uses = []
        for kind, reference, action in (
            ("tool", "opencode:bash", "invoke"),
            ("process", "opencode:local-process", "execute"),
        ):
            resource_id = self._catalog.get((kind, reference))
            if resource_id is None:
                raise ValueError("required host resource is unavailable")
            uses.append(
                ToolResourceUse(ResourceRequest(resource_id, action), reference)
            )
        if self._owns_session(execution, tool.provider_session_id) is not True:
            raise ValueError("OpenCode Session ownership changed while mapping")
        return tuple(uses)
