# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Opt-in construction entry point; existing chat routing is unchanged."""
from openjiuwen.harness.engine import HarnessEngine, create_harness_engine
from openjiuwen.harness_providers.construction import execution_authorization

from jiuwenswarm.common.runtime_workspace import RuntimeWorkspacePaths
from jiuwenswarm.runtime.harness.binding_store import ExecutionBindingStore
from jiuwenswarm.runtime.harness.config_source import ExecutionConfigSource
from jiuwenswarm.runtime.harness.execution_session import ExecutionSession
from jiuwenswarm.runtime.harness.recovery_store import SessionExecutionRecovery


def prepare_execution(source: ExecutionConfigSource, *, bindings: ExecutionBindingStore,
                      subject_id: str, host_session_id: str, workspace: str) -> HarnessEngine:
    """Bind and construct an unstarted provider for a trusted host scope.

    Authorization and lifecycle stay with the caller. A construction error
    preserves the selected binding (no silent fallback); retry with that same
    config or explicitly release it before beginning a new execution.
    """
    bound = bindings.bind(source, subject_id=subject_id,
                          host_session_id=host_session_id, workspace=workspace)
    return create_harness_engine(bound.spec, binding=bound.binding)


def prepare_native_session(source: ExecutionConfigSource, *, bindings: ExecutionBindingStore,
                           subject_id: str, host_session_id: str, workspace: str,
                           adapter, event_observer=None):
    """Bind a host-assembled Native Single without reconstructing its rails/tools.

    The caller supplies an authorized session adapter and owns start/stop and
    the projected output reader. Legacy and protocol input routes are exclusive.
    """
    bound = bindings.bind(source, subject_id=subject_id,
                          host_session_id=host_session_id, workspace=workspace)
    return adapter.build_native_execution(bound, event_observer=event_observer)


def prepare_execution_session(
    source: ExecutionConfigSource,
    *,
    bindings: ExecutionBindingStore,
    subject_id: str,
    host_session_id: str,
    runtime_paths: RuntimeWorkspacePaths,
    event_observer=None,
    detached_output=None,
    tool_gateway=None,
    recovery: SessionExecutionRecovery | None = None,
) -> ExecutionSession:
    """Construct an unstarted External session from one admitted path snapshot."""
    bound = bindings.bind(
        source,
        subject_id=subject_id,
        host_session_id=host_session_id,
        workspace=str(runtime_paths.runtime_workspace_root),
    )
    engine = create_harness_engine(bound.spec, binding=bound.binding)
    if engine.binding.provider_id == "native":
        raise ValueError("Native execution must use prepare_native_session")
    auto_approve_tools = execution_authorization(bound.spec).full_access
    return ExecutionSession(
        engine,
        runtime_paths,
        event_observer=event_observer,
        detached_output=detached_output,
        tool_gateway=tool_gateway,
        recovery=recovery,
        auto_approve_tools=auto_approve_tools,
    )
