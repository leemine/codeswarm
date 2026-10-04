"""Resource mapping for original inputs at the pinned OpenCode pre-I/O gate.

The exact governed Session and fixed native gate prove the executor and inputs.
Permission summaries never establish file or command authority. Shell authority
remains an explicit OS-user process grant, not a workspace sandbox.
"""

from collections.abc import Callable, Mapping
from types import MappingProxyType
from pathlib import Path
import os
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
        schemas = {
            "read": ({"filePath"}, {"filePath", "offset", "limit"}),
            "write": ({"filePath", "content"}, {"filePath", "content"}),
            "edit": ({"filePath", "oldString", "newString"},
                     {"filePath", "oldString", "newString", "replaceAll"}),
            "bash": ({"command", "description"}, {"command", "description", "timeout", "workdir"}),
        }
        if tool.tool_name not in schemas:
            raise ValueError("unsupported OpenCode native operation")
        required, allowed = schemas[tool.tool_name]
        if not required <= args.keys() or args.keys() - allowed:
            raise ValueError("unsupported OpenCode original argument schema")
        for key, value in args.items():
            if key in {"offset", "limit", "timeout"}:
                valid = type(value) is int and 0 < value <= 2**31 - 1
            elif key == "replaceAll":
                valid = type(value) is bool
            else:
                valid = isinstance(value, str) and "\x00" not in value
            if not valid:
                raise ValueError("invalid OpenCode original argument")
        uses = [self._use("tool", f"opencode:{tool.tool_name}", "invoke")]
        if tool.tool_name == "bash":
            if not args["command"].strip():
                raise ValueError("empty OpenCode command")
            # A process can consume ambient files/network/environment. No shell
            # parser pretends to turn this explicit grant into path confinement.
            uses.append(self._use("process", "opencode:local-process", "execute"))
        else:
            raw = args["filePath"]
            if (not os.path.isabs(raw) or raw != os.path.normpath(raw)
                    or raw.startswith("//")):
                raise ValueError("OpenCode path must be canonical and absolute")
            root = Path(execution.workspace).resolve()
            actual = Path(raw).resolve()
            if not actual.is_relative_to(root):
                raise ValueError("OpenCode path is outside the bound workspace")
            # The fixed write/edit implementations read old content before write.
            actions = ("read",) if tool.tool_name == "read" else ("read", "write")
            uses.extend(self._use("workspace", str(root), action, str(actual)) for action in actions)
        if self._owns_session(execution, tool.provider_session_id) is not True:
            raise ValueError("OpenCode Session ownership changed while mapping")
        return tuple(uses)

    def _use(self, kind, reference, action, path=None):
        resource_id = self._catalog.get((kind, reference))
        if resource_id is None:
            raise ValueError("required host resource is unavailable")
        return ToolResourceUse(ResourceRequest(resource_id, action, path), reference)
