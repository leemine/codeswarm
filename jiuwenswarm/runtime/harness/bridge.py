# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Opt-in construction entry point; existing chat routing is unchanged."""
from openjiuwen.harness.engine import HarnessEngine, create_harness_engine

from jiuwenswarm.runtime.harness.binding_store import ExecutionBindingStore
from jiuwenswarm.runtime.harness.config_source import ExecutionConfigSource


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
