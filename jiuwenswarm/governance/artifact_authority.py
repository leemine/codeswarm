"""Original Runtime source for one Native Workspace artifact publication."""

from __future__ import annotations

from .resources import ResourceAccessDenied, ResourceGuard, ResourceRequest


class NativeArtifactAuthority:
    def __init__(
        self,
        execution,
        *,
        host,
        current_identity,
        is_current_execution,
        owns_execution,
        owns_tool,
    ):
        self.execution = execution
        self._host = host
        self._identity = current_identity
        self._current = is_current_execution
        self._owns = owns_execution
        self._owns_tool = owns_tool

    def __call__(
        self,
        *,
        source_execution,
        execution_slice,
        native_session,
        is_current_host_request,
        toolkit,
        actual_paths,
        target_channels,
        session_id,
        channel_id,
        workspace,
    ):
        from .workspace_artifact_origin import validate_send_file_origin
        from .workspace_download import WorkspaceArtifactIssuer

        if (
            self._host is None
            or self.execution.provider_id != "native"
            or session_id != self.execution.session_id
            or workspace != self.execution.workspace
        ):
            raise ResourceAccessDenied("artifact original Runtime unavailable")
        source_check = validate_send_file_origin(
            source_execution=source_execution,
            execution_slice=execution_slice,
            toolkit=toolkit,
            actual_paths=actual_paths,
            target_channels=target_channels,
            session_id=session_id,
            channel_id=channel_id,
            workspace=workspace,
        )
        ctx = source_execution.agent_context

        def tool_decision():
            rows = self._host._storage.resource_grants(
                self.execution.project_id, self.execution.identity
            )["resources"]
            ids = {
                row["resource_id"]
                for row in rows
                if row["kind"] == "tool"
                and row["reference"] == "native:send_file_to_user"
                and row["action"] == "invoke"
            }
            if len(ids) != 1:
                raise ResourceAccessDenied("artifact tool resource unavailable")
            decision = ResourceGuard(self._host._storage).check(
                self.execution.project_id,
                self.execution.identity,
                ResourceRequest(next(iter(ids)), "invoke"),
            )
            if decision.reference != "native:send_file_to_user":
                raise ResourceAccessDenied("artifact tool resource changed")
            return decision

        original_tool_decision = tool_decision()

        def current():
            return (
                self._identity() == self.execution.identity
                and self._current() is True
                and is_current_host_request() is True
                and self._owns(self.execution, native_session) is True
                and ctx is source_execution.agent_context
                and self._owns_tool(self.execution, ctx.agent, ctx.session) is True
                and source_check() is True
                and tool_decision() == original_tool_decision
            )

        if not current():
            raise ResourceAccessDenied("artifact original execution changed")
        return WorkspaceArtifactIssuer.capture(
            self._host,
            self._identity,
            session_id,
            channel_id=channel_id,
            workspace=workspace,
            source_check=current,
            actual_paths=actual_paths,
        )
