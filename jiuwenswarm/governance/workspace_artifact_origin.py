"""Exact Native send_file method evidence; no ambient identity fallback."""

from __future__ import annotations

from openjiuwen.core.foundation.tool import ToolExecution
from openjiuwen.core.foundation.tool.function.function import LocalFunction
from openjiuwen.harness.execution_subject import current_execution_subject
from openjiuwen.harness_protocol import json_value_to_builtin

from .tool_context import current_native_execution_slice
from .workspace_download import WorkspaceDownloadDenied


def validate_send_file_origin(
    *,
    source_execution,
    execution_slice,
    toolkit,
    actual_paths,
    target_channels,
    session_id,
    channel_id,
    workspace,
):
    """Return a fixed-origin checker after checking actual LocalFunction args.

    Runtime must additionally prove original Native Session/agent ownership,
    full identity, admission generation and resource authority. This function
    does not assign identity or accept another toolkit's registered methods.
    """
    from jiuwenswarm.agents.harness.common.tools.send_file_to_user import (
        SendFileToolkit,
    )
    from jiuwenswarm.agents.harness.common.rails.permissions.generated_artifact_delivery import (
        normalize_send_file_paths,
        normalize_send_file_target_channels,
    )

    source, bound = source_execution, execution_slice
    if (
        type(source) is not ToolExecution
        or not source.is_current()
        or type(toolkit) is not SendFileToolkit
        or bound is None
    ):
        raise WorkspaceDownloadDenied("actual send_file execution required")
    executor = source.executor
    args = json_value_to_builtin(source.operation.arguments)
    if (
        type(executor) is not LocalFunction
        or source.operation.tool_name != "send_file_to_user"
        or type(args) is not dict
        or set(args) - {"abs_file_path_list", "target_channels"}
        or "abs_file_path_list" not in args
        or getattr(source.original_invoke, "__self__", None) is not executor
        or getattr(source.original_invoke, "__func__", None) is not LocalFunction.invoke
        or getattr(executor._func, "__self__", None) is not toolkit
        or getattr(executor._func, "__func__", None) is not SendFileToolkit.send_file
        or normalize_send_file_paths(args["abs_file_path_list"]) != actual_paths
        or normalize_send_file_target_channels(args.get("target_channels"))
        != target_channels
        or target_channels not in ((), (channel_id,))
    ):
        raise WorkspaceDownloadDenied("send_file origin or arguments mismatch")
    original_factory = getattr(bound, "artifact_issuer_factory", None)
    original_func = executor._func

    def check():
        return (
            source.is_current_origin()
            and current_native_execution_slice() is bound
            and bound.active
            and bound.subject == current_execution_subject()
            and (bound.subject is None or bound.subject.kind != "subagent")
            and getattr(bound, "artifact_issuer_factory", None) is original_factory
            and executor._func is original_func
            and toolkit.session_id == session_id
            and toolkit.channel_id == channel_id
            and toolkit._resolve_project_dir() == workspace
        )

    if not callable(original_factory) or not check():
        raise WorkspaceDownloadDenied("send_file execution no longer current")
    return check
