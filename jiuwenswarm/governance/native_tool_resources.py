"""Conservative resource mapping for proven local Native file/shell tools.

The host supplies visible resource reference metadata and exact Session ownership.
This maps requirements only; BoundToolResourceAuthority checks current grants.
"""

from collections.abc import Callable, Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Any

from openjiuwen.core.session import get_current_session
from openjiuwen.core.sys_operation import OperationMode
from openjiuwen.core.sys_operation.sys_operation import SysOperation
from openjiuwen.harness.tools import BashTool
from openjiuwen.harness.tools.filesystem import (
    EditFileTool,
    ReadFileTool,
    WriteFileTool,
    _resolve_tool_file_path,
)
from openjiuwen.harness_protocol import BeforeToolContext

from .native_executor import has_native_invoke, require_native_executor
from .resources import ResourceDefinition, ResourceRequest
from .tool_resources import ResourceExecutionContext, ToolResourceUse

_TOOLS = {
    "read_file": ReadFileTool,
    "write_file": WriteFileTool,
    "edit_file": EditFileTool,
    "bash": BashTool,
}
_FIELDS = {
    "read_file": {"file_path", "offset", "limit", "pages"},
    "write_file": {"file_path", "content"},
    "edit_file": {"file_path", "old_string", "new_string", "replace_all"},
    "bash": {
        "command",
        "timeout",
        "workdir",
        "run_in_background",
        "max_output_chars",
        "shell_type",
        "description",
    },
}


class NativeToolResourceResolver:
    """Use exact host references; never grant resources or inherit credentials.

    Tool references are native:<tool_name>; the explicit local-process reference
    permits OS-user execution, including ambient filesystem/environment access.
    No shell parsing claims path confinement. Unsupported tools/backends deny.
    """

    def __init__(
        self,
        grants: Mapping[str, Any],
        *,
        owns_session: Callable[[ResourceExecutionContext, Any, Any], bool],
    ):
        if not callable(owns_session):
            raise TypeError("exact host Session ownership check is required")
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

    def _use(self, kind, reference, action, path=None):
        resource_id = self._catalog.get((kind, reference))
        if resource_id is None:
            raise ValueError("required host resource is unavailable")
        return ToolResourceUse(ResourceRequest(resource_id, action, path), reference)

    def _workspace_use(self, root, path, actions):
        root = str(Path(root).resolve())
        if not Path(path).is_relative_to(root):
            raise ValueError("Native path is outside the bound workspace")
        return [self._use("workspace", root, action, path) for action in actions]

    def resources_for_tool(
        self, execution: ResourceExecutionContext, tool: BeforeToolContext
    ) -> tuple[ToolResourceUse, ...]:
        proof = require_native_executor(tool)
        session = proof.session
        if (
            execution.provider_id != "native"
            or session is None
            or session.get_session_id() != execution.session_id
            or tool.provider_session_id != execution.session_id
            or self._owns_session(execution, proof.agent, session) is not True
        ):
            raise ValueError("Native operation has no matching owned Session")
        expected = _TOOLS.get(tool.tool_name)
        executor = proof.executor
        if (
            expected is None
            or type(executor) is not expected
            or not has_native_invoke(executor, expected)
        ):
            raise ValueError("unsupported Native executor")
        operation = proof.backend
        if (
            type(operation) is not SysOperation
            or operation.mode is not OperationMode.LOCAL
        ):
            raise ValueError("only explicit local Native resources are supported")
        if str(Path(proof.workspace).resolve()) != str(
            Path(execution.workspace).resolve()
        ):
            raise ValueError("Native runtime workspace differs from its Binding")
        args = tool.arguments
        if set(args) - _FIELDS[tool.tool_name]:
            raise ValueError("unknown Native argument schema")
        uses = [self._use("tool", f"native:{tool.tool_name}", "invoke")]
        if expected is BashTool:
            if (
                not isinstance(args.get("command"), str)
                or not args["command"].strip()
                or args.get("run_in_background", False) is not False
            ):
                raise ValueError("invalid or unsupported background shell operation")
            uses.append(self._use("process", "native:local-process", "execute"))
            return tuple(uses)
        raw = args.get("file_path")
        if (
            not isinstance(raw, str)
            or not raw
            or raw.lstrip().startswith("{")
            or "\x00" in raw
        ):
            raise ValueError("a plain file path is required")
        path = str(Path(_resolve_tool_file_path(operation, raw)).resolve())
        actions = ("read",) if expected is ReadFileTool else ("read", "write")
        uses.extend(self._workspace_use(execution.workspace, path, actions))
        if expected is not ReadFileTool:
            if expected is WriteFileTool and not isinstance(args.get("content"), str):
                raise ValueError("write content must be a string")
            if expected is EditFileTool and (
                not isinstance(args.get("old_string"), str)
                or not isinstance(args.get("new_string"), str)
            ):
                raise ValueError("edit strings are required")
            active = get_current_session()
            if active is not None:
                if active is not session:
                    raise ValueError("Native history Session differs from execution")
                history = Path(executor._build_history_path(active)).resolve()  # pylint: disable=protected-access
                # No inference from project ownership: host audit root must be registered/granted.
                uses.extend(
                    self._workspace_use(
                        str(history.parent), str(history), ("read", "write")
                    )
                )
                uses.extend(
                    self._workspace_use(
                        str(history.parent), str(history) + ".tmp", ("write",)
                    )
                )
        return tuple(uses)
