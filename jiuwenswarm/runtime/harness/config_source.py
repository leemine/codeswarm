# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Host-owned execution selection; model-provider settings are separate."""
from dataclasses import dataclass
from collections.abc import Mapping

from openjiuwen.harness.engine import resolve_execution_spec
from openjiuwen.harness_protocol import AgentExecutionSpec


def parse_execution_config(value: Mapping[str, object]) -> AgentExecutionSpec:
    """Parse only the dedicated execution config, never model settings."""
    allowed = {"provider_id", "config_revision", "requested_mode", "provider_config"}
    if not isinstance(value, Mapping):
        raise TypeError("execution configuration must be an object")
    if set(value) - allowed:
        raise ValueError("unknown execution configuration fields")
    if not {"provider_id", "config_revision"} <= set(value):
        raise ValueError("execution provider_id and config_revision are required")
    return AgentExecutionSpec(**dict(value))


@dataclass(frozen=True, slots=True)
class ExecutionConfigSource:
    """Complete snapshots; project is supplied only for a project request."""

    explicit: AgentExecutionSpec | None = None
    project: AgentExecutionSpec | None = None
    default: AgentExecutionSpec | None = None

    def resolve(self) -> AgentExecutionSpec:
        return resolve_execution_spec(explicit=self.explicit, project=self.project, default=self.default)
