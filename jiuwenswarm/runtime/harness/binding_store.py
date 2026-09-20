# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Process-local immutable bindings; not a second session runtime or cache."""
from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock

from openjiuwen.harness.engine import ExecutionBinding
from openjiuwen.harness_protocol import AgentExecutionSpec

from jiuwenswarm.runtime.harness.config_source import ExecutionConfigSource


@dataclass(frozen=True, slots=True)
class BoundExecution:
    binding: ExecutionBinding
    spec: AgentExecutionSpec = field(repr=False)


class ExecutionBindingStore:
    """Keep an exact config snapshot for each authorized session scope.

    No live provider instances are cached. The owning Runtime must retain
    the returned engine and explicitly release its binding at session close.
    Durable restore is outside this first construction slice.
    """

    def __init__(self) -> None:
        self._bindings: dict[tuple[str, str, str], BoundExecution] = {}
        self._lock = RLock()

    def bind(self, source: ExecutionConfigSource, *, subject_id: str,
             host_session_id: str, workspace: str) -> BoundExecution:
        # Validate scope even when retrieving an existing binding.
        for value in (subject_id, host_session_id, workspace):
            if not isinstance(value, str) or not value.strip():
                raise ValueError("subject, session and workspace must be non-empty strings")
        if not Path(workspace).is_absolute():
            raise ValueError("workspace must be an absolute path")
        key = (subject_id, host_session_id, str(Path(workspace).resolve()))
        with self._lock:
            existing = self._bindings.get(key)
            if existing is not None:
                if source.explicit is not None:
                    existing.binding.validate_spec(source.explicit)
                return existing
            spec = source.resolve()
            binding = ExecutionBinding.create(spec, subject_id=subject_id,
                                              host_session_id=host_session_id, workspace=workspace)
            result = BoundExecution(binding, spec)
            self._bindings[key] = result
            return result

    def release(self, binding: ExecutionBinding) -> None:
        """Remove only the matching binding; stale cleanup cannot remove another."""
        key = (binding.subject_id, binding.host_session_id, binding.workspace)
        with self._lock:
            current = self._bindings.get(key)
            if current is not None and current.binding is binding:
                del self._bindings[key]
