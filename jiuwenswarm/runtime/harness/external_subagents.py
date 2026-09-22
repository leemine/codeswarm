# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Compose the existing product subagent runtime for one External parent."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from types import SimpleNamespace
from typing import Any

from openjiuwen.core.session.stream.base import OutputSchema
from openjiuwen.harness.tools.subagent import (
    build_subagent_tools,
    release_subagent_control,
)

from jiuwenswarm.runtime.harness.codex_subagent import (
    CodexSubagentExecutionFactory,
)
from jiuwenswarm.runtime.harness.request_binding import AdmittedExecutionRoute
from jiuwenswarm.runtime.harness.tool_gateway import (
    ProductToolGateway,
    ProductToolScope,
)

_SAME_ENGINE_AGENT_DESCRIPTION = (
    "- general-purpose: same Provider, fixed parent configuration and workspace; "
    "display_name and role describe the delegated assignment"
)


class ExternalSubagentParentSession:
    """Minimal parent Session port used by the provider-neutral runtime.

    B4 needs live state and event projection only. Durable checkpoint restore is
    deliberately left to R1-04, so state stays owned by this live parent binding.
    """

    def __init__(
        self,
        session_id: str,
        *,
        write_output: Callable[[OutputSchema], Awaitable[None]],
    ) -> None:
        if not session_id:
            raise ValueError("External subagent parent Session id is required")
        self._session_id = session_id
        self._state: dict[str, Any] = {}
        self._write_output = write_output

    def get_session_id(self) -> str:
        return self._session_id

    def get_state(self, key: str | list | dict | None = None) -> Any:
        if key is None:
            return dict(self._state)
        if isinstance(key, str):
            return self._state.get(key)
        if isinstance(key, list):
            return {name: self._state.get(name) for name in key}
        if isinstance(key, dict):
            return {
                name: self._state.get(name, default)
                for name, default in key.items()
            }
        return None

    def update_state(self, data: dict[str, Any]) -> None:
        self._state.update(data)

    async def write_stream(self, data: dict | OutputSchema) -> None:
        if not isinstance(data, OutputSchema):
            raise TypeError("External subagent parent accepts OutputSchema events only")
        await self._write_output(data)


class ExternalSubagentRuntime:
    """Own six original product tools and one same-engine child factory."""

    def __init__(
        self,
        route: AdmittedExecutionRoute,
        *,
        write_output: Callable[[OutputSchema], Awaitable[None]],
    ) -> None:
        if route.provider_id != "codex":
            raise ValueError("External subagent runtime currently requires Codex")
        binding = route.bound.binding
        self._route = route
        self._parent_session = ExternalSubagentParentSession(
            binding.host_session_id,
            write_output=write_output,
        )
        # SubagentControl only needs the parent workspace contract and a stable
        # object on which to cache its per-parent control registry.
        self._parent_host = SimpleNamespace(
            deep_config=SimpleNamespace(
                workspace=str(route.runtime_paths.runtime_workspace_root),
                subagents=(),
            )
        )
        self._factory = CodexSubagentExecutionFactory(route)
        tools = build_subagent_tools(
            self._parent_host,
            language="cn",
            available_agents=_SAME_ENGINE_AGENT_DESCRIPTION,
            execution_factory=self._factory,
        )
        self._gateway = ProductToolGateway(
            tools,
            scope=ProductToolScope(
                subject_id=binding.subject_id,
                host_session_id=binding.host_session_id,
                workspace=binding.workspace,
            ),
            invoke_kwargs={"session": self._parent_session},
        )
        self._closed = False

    @property
    def gateway(self) -> ProductToolGateway:
        return self._gateway

    @property
    def parent_session(self) -> ExternalSubagentParentSession:
        return self._parent_session

    def has_control(self) -> bool:
        controls = getattr(self._parent_host, "_subagent_controls", None)
        return bool(controls)

    async def close(self, reason: str = "parent_ended") -> None:
        if self._closed:
            return
        self._closed = True
        await release_subagent_control(
            self._parent_host,
            self._route.bound.binding.host_session_id,
            reason=reason,
        )


__all__ = [
    "ExternalSubagentParentSession",
    "ExternalSubagentRuntime",
]
