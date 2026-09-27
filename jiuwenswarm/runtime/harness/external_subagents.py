# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Compose the existing product subagent runtime for one External parent."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from types import SimpleNamespace
from typing import Any

from openjiuwen.core.session.stream.base import OutputSchema
from openjiuwen.harness.tools.subagent import (
    build_subagent_tools,
    release_subagent_control,
)

from jiuwenswarm.runtime.harness.external_subagent import (
    ExternalSubagentExecutionFactory,
    SUPPORTED_SUBAGENT_PROVIDERS,
)
from jiuwenswarm.runtime.harness.request_binding import AdmittedExecutionRoute
from jiuwenswarm.runtime.harness.recovery_store import SessionExecutionRecovery
from jiuwenswarm.runtime.harness.tool_gateway import (
    ProductTool,
    ProductToolGateway,
    ProductToolScope,
)

_SAME_ENGINE_AGENT_DESCRIPTION = (
    "- general-purpose: same Provider, fixed parent configuration and workspace; "
    "display_name and role describe the delegated assignment"
)


class ExternalSubagentParentSession:
    """Minimal parent Session port backed by the parent's recovery archive."""

    def __init__(
        self,
        session_id: str,
        *,
        write_output: Callable[[OutputSchema], Awaitable[None]],
        recovery: SessionExecutionRecovery | None = None,
    ) -> None:
        if not session_id:
            raise ValueError("External subagent parent Session id is required")
        self._session_id = session_id
        self._recovery = recovery
        self._state: dict[str, Any] = (
            recovery.load_host_state() if recovery is not None else {}
        )
        self._write_output = write_output
        self.state_write_failed = False

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
                name: self._state.get(name, default) for name, default in key.items()
            }
        return None

    def update_state(self, data: dict[str, Any]) -> None:
        updated = {**self._state, **data}
        if self._recovery is not None:
            try:
                self._recovery.save_host_state(updated)
            except Exception:
                # A failed persistence acknowledgement is not a committed
                # product state. Goal must not resume from this memory cache.
                self.state_write_failed = True
                raise
        self._state = updated

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
        additional_tools: Sequence[ProductTool] = (),
        parent_session: ExternalSubagentParentSession | None = None,
    ) -> None:
        if route.provider_id not in SUPPORTED_SUBAGENT_PROVIDERS:
            raise ValueError(
                "External subagent runtime requires a supported External provider"
            )
        binding = route.bound.binding
        self._route = route
        self._parent_session = parent_session or ExternalSubagentParentSession(
            binding.host_session_id,
            write_output=write_output,
            recovery=route.recovery,
        )
        # SubagentControl only needs the parent workspace contract and a stable
        # object on which to cache its per-parent control registry.
        self._parent_host = SimpleNamespace(
            deep_config=SimpleNamespace(
                workspace=str(route.runtime_paths.runtime_workspace_root),
                subagents=(),
            )
        )
        self._factory = ExternalSubagentExecutionFactory(route)
        tools = build_subagent_tools(
            self._parent_host,
            language="cn",
            available_agents=_SAME_ENGINE_AGENT_DESCRIPTION,
            execution_factory=self._factory,
        )
        self._gateway = ProductToolGateway(
            [*tools, *additional_tools],
            scope=ProductToolScope(
                subject_id=binding.subject_id,
                host_session_id=binding.host_session_id,
                workspace=binding.workspace,
            ),
            invoke_kwargs={"session": self._parent_session},
        )
        self._closed = False
        self._close_lock = asyncio.Lock()

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
        async with self._close_lock:
            if self._closed:
                return
            failures: list[Exception] = []
            try:
                await release_subagent_control(
                    self._parent_host,
                    self._route.bound.binding.host_session_id,
                    reason=reason,
                )
            except Exception as exc:
                failures.append(exc)
            close_pending = getattr(self._factory, "close_pending", None)
            if callable(close_pending):
                try:
                    await close_pending()
                except Exception as exc:
                    failures.append(exc)
            if failures:
                raise ExceptionGroup(
                    "External subagent exits could not be confirmed", failures
                )
            self._closed = True


__all__ = [
    "ExternalSubagentParentSession",
    "ExternalSubagentRuntime",
]
