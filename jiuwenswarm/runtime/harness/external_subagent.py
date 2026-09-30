# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Same-engine child executions bound to one admitted External parent."""

from __future__ import annotations

import asyncio
import dataclasses
import inspect
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from openjiuwen.harness.engine import ExecutionBinding
from openjiuwen.harness.subagent_runtime.ports import (
    ChunkCallback,
    ParentExecutionContext,
    ResultCallback,
    SubagentBuildRequest,
    SubagentExecution,
    SubagentTurnRequest,
    SubagentTurnResult,
)
from openjiuwen.harness.subagent_runtime.stream_output import TurnOutputAggregator
from openjiuwen.harness_protocol import HarnessInput, ToolInvocation, TurnEventKind

from jiuwenswarm.runtime.harness.bridge import prepare_execution_session
from jiuwenswarm.runtime.harness.config_source import ExecutionConfigSource
from jiuwenswarm.runtime.harness.context_bridge import (
    build_external_context,
    build_external_context_snapshot,
)
from jiuwenswarm.runtime.harness.execution_session import ExecutionSession
from jiuwenswarm.runtime.harness.external_subagent_profiles import (
    ExternalSubagentExecutionKind,
    ExternalSubagentProfile,
    validate_external_subagent_request,
)
from jiuwenswarm.runtime.harness.request_binding import AdmittedExecutionRoute

if TYPE_CHECKING:
    from openjiuwen.harness.tools.browser_move.playwright_runtime import (
        BrowserToolAdmission,
    )
    from jiuwenswarm.runtime.harness.external_browser_artifacts import (
        BrowserArtifactSink,
        BrowserDecisionId,
    )


def _build_external_browser_resources(**kwargs: Any):
    from jiuwenswarm.runtime.harness.external_browser import (
        build_external_browser_resources,
    )

    return build_external_browser_resources(**kwargs)


def _build_external_browser_identity(**kwargs: Any):
    from jiuwenswarm.runtime.harness.external_browser import build_external_browser_identity

    return build_external_browser_identity(**kwargs)


_CHILD_SUBJECT_PREFIX = "subagent:"
SUPPORTED_SUBAGENT_PROVIDERS = frozenset({"codex", "opencode"})


class ExternalSubagentExecution:
    """Drive one independently bound External child through the shared session path."""

    def __init__(
        self,
        session: ExecutionSession,
        *,
        parent_session_id: str,
        release: Callable[["ExternalSubagentExecution"], Awaitable[None]],
    ) -> None:
        self._session = session
        self._parent_session_id = parent_session_id
        self._release = release
        self._close_lock = asyncio.Lock()
        self._closed = False

    @property
    def binding(self):
        """Expose the immutable child identity for host lifecycle checks."""
        return self._session.binding

    @property
    def closed(self) -> bool:
        return self._closed

    async def run_turn(
        self,
        request: SubagentTurnRequest,
        *,
        on_chunk: ChunkCallback | None = None,
        on_result: ResultCallback,
    ) -> None:
        if self._closed:
            raise RuntimeError("External subagent execution is closed")
        receipt = await self._session.send(
            HarnessInput(
                content=request.query,
                metadata={
                    "task_id": request.task_id,
                    "parent_session_id": self._parent_session_id,
                    "subagent_id": self.binding.host_session_id,
                },
            )
        )
        aggregator = TurnOutputAggregator()
        terminal: TurnEventKind | None = None
        try:
            async for item in self._session.outputs(receipt.turn_id):
                if item.chunk is not None:
                    aggregator.consume(item.chunk)
                    if on_chunk is not None:
                        await on_chunk(item.chunk)
                if item.terminal is not None:
                    terminal = item.terminal
        except asyncio.CancelledError:
            abort_task = asyncio.create_task(self._session.abort(immediate=True))
            try:
                await asyncio.shield(abort_task)
            except asyncio.CancelledError:
                await abort_task
            raise

        if terminal is None:
            raise RuntimeError("External subagent turn ended without a terminal event")
        failed = terminal is not TurnEventKind.FINISHED or aggregator.is_error()
        error_code = None
        if terminal is TurnEventKind.FAILED:
            error_code = "PROVIDER_TURN_FAILED"
        elif terminal is TurnEventKind.ABORTED:
            error_code = "PROVIDER_TURN_ABORTED"
        await on_result(
            SubagentTurnResult(
                output=(
                    aggregator.output()
                    or ("External subagent turn failed" if failed else "")
                ),
                reasoning=aggregator.reasoning_text(),
                is_error=failed,
                error_code=error_code,
            )
        )

    async def close(self, reason: str) -> None:
        del reason
        async with self._close_lock:
            if self._closed:
                return
            await self._session.stop()
            await self._release(self)
            self._closed = True


class ExternalSubagentExecutionFactory:
    """Create same-engine children from an exact parent route snapshot.

    The child gets a distinct subject, Binding and Provider session.  It uses
    the parent's already admitted Provider configuration and task paths; there
    is deliberately no child Provider/config/cwd override surface.
    """

    def __init__(
        self,
        parent_route: AdmittedExecutionRoute,
        *,
        allowed_subagent_types: tuple[str, ...] | None = None,
        browser_admit: BrowserToolAdmission | None = None,
        browser_artifact_sink: BrowserArtifactSink | None = None,
        browser_decision_id_for: BrowserDecisionId | None = None,
    ) -> None:
        binding = parent_route.bound.binding
        if (
            binding.provider_id not in SUPPORTED_SUBAGENT_PROVIDERS
            or parent_route.provider_id != binding.provider_id
        ):
            raise ValueError(
                "External subagent factory requires a supported, consistent parent"
            )
        if binding.workspace != str(
            parent_route.runtime_paths.runtime_workspace_root.resolve()
        ):
            raise ValueError("External parent workspace does not match runtime paths")
        if browser_admit is not None and not callable(browser_admit):
            raise ValueError("Browser admission adapter must be callable")
        if (browser_artifact_sink is None) != (browser_decision_id_for is None):
            raise ValueError(
                "Browser Artifact sink and decision identity must be configured together"
            )
        if browser_artifact_sink is not None and browser_admit is None:
            raise ValueError("Browser Artifact delivery requires Browser admission")
        if browser_artifact_sink is not None and not callable(browser_artifact_sink):
            raise ValueError("Browser Artifact sink must be callable")
        if browser_decision_id_for is not None and not callable(browser_decision_id_for):
            raise ValueError("Browser Artifact decision identity must be callable")
        try:
            parent_route.runtime_paths.cwd.resolve().relative_to(
                parent_route.runtime_paths.runtime_workspace_root.resolve()
            )
        except ValueError as exc:
            raise ValueError(
                "External parent cwd is outside the admitted workspace"
            ) from exc
        binding.validate_spec(parent_route.bound.spec)
        self._parent_route = parent_route
        self._parent_binding = binding
        self._parent_spec = parent_route.bound.spec
        self._work_mode = (
            parent_route.surface.identity.work_mode
            if parent_route.surface is not None
            else None
        )
        self._allowed_subagent_types = (
            frozenset(allowed_subagent_types)
            if allowed_subagent_types is not None
            else None
        )
        self._child_source = ExecutionConfigSource(explicit=self._parent_spec)
        self._browser_admit = browser_admit
        self._browser_artifact_sink = browser_artifact_sink
        self._browser_decision_id_for = browser_decision_id_for
        self._live: dict[str, ExternalSubagentExecution] = {}
        self._cleanup_pending: dict[str, ExecutionSession] = {}
        self._reserved: set[str] = set()
        self._browser_identities: dict[str, Any] = {}
        self._browser_resources: dict[str, Any] = {}
        self._lock = asyncio.Lock()

    @property
    def parent_binding(self):
        return self._parent_binding

    async def create(
        self,
        request: SubagentBuildRequest,
        context: ParentExecutionContext,
    ) -> SubagentExecution:
        self._validate_parent_context(context)
        profile = self._validate_build_request(request, context)
        async with self._lock:
            existing = self._live.get(request.subagent_id)
            if (
                request.subagent_id in self._reserved
                or existing is not None
                and not existing.closed
            ):
                raise RuntimeError(
                    f"External subagent execution already exists: {request.subagent_id}"
                )
            self._reserved.add(request.subagent_id)

        session: ExecutionSession | None = None
        browser_resources = None
        browser_identity = None
        try:
            child_subject_id = f"{_CHILD_SUBJECT_PREFIX}{request.subagent_id}"
            prospective_binding = ExecutionBinding.create(
                self._parent_spec,
                subject_id=child_subject_id,
                host_session_id=request.subagent_id,
                workspace=str(self._parent_route.runtime_paths.runtime_workspace_root),
            )
            if profile.execution_kind is ExternalSubagentExecutionKind.BROWSER:
                browser_identity = _build_external_browser_identity(
                    request=request, child_binding=prospective_binding,
                    parent_subject_id=context.parent_subject_id,
                    parent_session_id=context.parent_session_id,
                    channel_id=self._parent_route.channel_id,
                    runtime_paths=self._parent_route.runtime_paths,
                )
                self._browser_identities[request.subagent_id] = browser_identity
                await self._admit_browser_profile(browser_identity)
                browser_resources = _build_external_browser_resources(
                    request=request,
                    child_binding=prospective_binding,
                    parent_subject_id=context.parent_subject_id,
                    parent_session_id=context.parent_session_id,
                    channel_id=self._parent_route.channel_id,
                    runtime_paths=self._parent_route.runtime_paths,
                    admit=self._require_browser_admission(),
                    artifact_sink=self._browser_artifact_sink,
                    decision_id_for=self._browser_decision_id_for,
                )
                self._browser_resources[request.subagent_id] = browser_resources
            child_recovery = (
                self._parent_route.recovery.child(
                    prospective_binding,
                    self._parent_route.runtime_paths,
                )
                if self._parent_route.recovery is not None
                else None
            )
            session = prepare_execution_session(
                self._child_source,
                bindings=self._parent_route.bindings,
                subject_id=child_subject_id,
                host_session_id=request.subagent_id,
                runtime_paths=self._parent_route.runtime_paths,
                tool_gateway=(
                    browser_resources.gateway if browser_resources is not None else None
                ),
                recovery=child_recovery,
                runtime_policy=(
                    self._parent_route.surface.runtime_policy
                    if self._parent_route.surface is not None
                    else None
                ),
            )
            child_binding = session.binding
            if (
                child_binding is self._parent_binding
                or child_binding.subject_id != child_subject_id
                or child_binding.host_session_id != request.subagent_id
                or child_binding.workspace != self._parent_binding.workspace
                or child_binding.provider_id != self._parent_binding.provider_id
                or child_binding.config_revision != self._parent_binding.config_revision
                or child_binding.fingerprint != self._parent_binding.fingerprint
            ):
                raise RuntimeError(
                    "External child binding did not inherit the parent scope"
                )

            parent_surface = self._parent_route.surface
            context_snapshot = (
                build_external_context_snapshot(
                    paths=self._parent_route.runtime_paths,
                    surface=parent_surface,
                )
                if parent_surface is not None
                else None
            )
            context_value = build_external_context(
                paths=self._parent_route.runtime_paths,
                host_session_id=request.subagent_id,
                channel_id=self._parent_route.channel_id,
                provider_id=self._parent_binding.provider_id,
                surface=parent_surface,
                context_snapshot=context_snapshot,
            )
            child_prompt = (
                f"{context_value.system_prompt}\n"
                "You are a product subagent. Work only on the delegated task; "
                "do not act as the parent execution.\n"
                f"Subagent type: {request.subagent_type}.\n"
                f"Display name: {request.display_name}.\n"
                f"Role: {request.role}."
            )
            if browser_resources is not None:
                child_prompt += f"\n{browser_resources.system_prompt}"
            context_value = dataclasses.replace(
                context_value,
                agent_name=request.display_name,
                agent_id=child_subject_id,
                system_prompt=child_prompt,
                metadata={
                    **dict(context_value.metadata),
                    "parent_session_id": context.parent_session_id,
                    "parent_subject_id": context.parent_subject_id,
                    "subagent_id": request.subagent_id,
                    "subagent_type": request.subagent_type,
                },
            )
            await session.start(context_value)
            execution = ExternalSubagentExecution(
                session,
                parent_session_id=context.parent_session_id,
                release=self._release,
            )
            async with self._lock:
                self._live[request.subagent_id] = execution
            return execution
        except BaseException as create_error:
            cleanup_failures: list[BaseException] = []
            if session is not None:
                try:
                    await session.stop()
                except Exception as cleanup_error:
                    cleanup_failures.append(cleanup_error)
                    async with self._lock:
                        self._cleanup_pending[request.subagent_id] = session
                else:
                    self._parent_route.bindings.release(session.binding)
            elif browser_resources is not None:
                try:
                    await browser_resources.gateway.close()
                except Exception as cleanup_error:
                    cleanup_failures.append(cleanup_error)
            if not cleanup_failures and browser_identity is not None:
                try:
                    await self._release_browser_identity(request.subagent_id)
                except Exception as cleanup_error:
                    cleanup_failures.append(cleanup_error)
            if cleanup_failures:
                raise BaseExceptionGroup(
                    "External child startup failed and exit was not confirmed",
                    [create_error, *cleanup_failures],
                ) from None
            raise
        finally:
            async with self._lock:
                self._reserved.discard(request.subagent_id)

    async def can_restore(
        self,
        request: SubagentBuildRequest,
        context: ParentExecutionContext,
    ) -> bool:
        self._validate_parent_context(context)
        self._validate_build_request(request, context)
        parent_recovery = self._parent_route.recovery
        if parent_recovery is None:
            return False
        child_binding = ExecutionBinding.create(
            self._parent_spec,
            subject_id=f"{_CHILD_SUBJECT_PREFIX}{request.subagent_id}",
            host_session_id=request.subagent_id,
            workspace=str(self._parent_route.runtime_paths.runtime_workspace_root),
        )
        child_recovery = parent_recovery.child(
            child_binding,
            self._parent_route.runtime_paths,
            create_if_missing=False,
        )
        return child_recovery is not None and child_recovery.has_checkpoint()

    def _validate_parent_context(self, context: ParentExecutionContext) -> None:
        if context.parent_session_id != self._parent_binding.host_session_id:
            raise ValueError("External child parent Session does not match the binding")
        if context.parent_subject_id != self._parent_binding.subject_id:
            raise ValueError("External child parent subject does not match the binding")

    def _validate_build_request(
        self,
        request: SubagentBuildRequest,
        context: ParentExecutionContext,
    ) -> ExternalSubagentProfile:
        profile = validate_external_subagent_request(
            subagent_type=request.subagent_type,
            browser_capabilities=request.browser_capabilities,
            browser_available=self._browser_admit is not None,
            work_mode=self._work_mode,
        )
        if (
            self._allowed_subagent_types is not None
            and profile.subagent_type not in self._allowed_subagent_types
        ):
            raise ValueError(
                f"External subagent type is not mounted: {profile.subagent_type}"
            )
        expected_prefix = f"{context.parent_session_id}_sub_"
        if not request.subagent_id.startswith(expected_prefix):
            raise ValueError(
                "External child identity does not belong to the parent Session"
            )
        return profile

    def _require_browser_admission(self) -> BrowserToolAdmission:
        admission = self._browser_admit
        if admission is None:
            raise RuntimeError("Browser admission adapter is unavailable")
        return admission

    async def _admit_browser_profile(self, identity: Any) -> None:
        """Authorize persistent Profile use before Provider/MCP/Chrome startup."""

        invocation = ToolInvocation(
            call_id=f"{identity.task.task_id}:profile-use",
            name="browser_profile_use",
            arguments={},
        )
        admission = self._require_browser_admission()
        try:
            admitted = admission(identity, invocation)
            if inspect.isawaitable(admitted):
                admitted = await admitted
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise RuntimeError(
                "External Browser profile admission failed"
            ) from exc
        if admitted is not True:
            raise PermissionError("External Browser profile use is not allowed")

    async def _release(self, execution: ExternalSubagentExecution) -> None:
        binding = execution.binding
        await self._release_browser_identity(binding.host_session_id)
        async with self._lock:
            current = self._live.get(binding.host_session_id)
            if current is execution:
                del self._live[binding.host_session_id]
        self._parent_route.bindings.release(binding)

    async def _release_browser_identity(self, subagent_id: str) -> None:
        identity = self._browser_identities.get(subagent_id)
        if identity is None:
            return
        from jiuwenswarm.runtime.harness.external_browser import cleanup_external_browser_configuration

        cleanup_external_browser_configuration(self._parent_route.runtime_paths, identity)
        release = getattr(self._browser_admit, "release_task", None)
        if callable(release):
            result = release(identity)
            if inspect.isawaitable(result):
                await result
        self._browser_identities.pop(subagent_id, None)
        self._browser_resources.pop(subagent_id, None)

    async def close_pending(self) -> None:
        """Retry half-started child cleanup retained by this factory."""

        failures: list[Exception] = []
        async with self._lock:
            pending = tuple(self._cleanup_pending.items())
        for subagent_id, session in pending:
            try:
                await session.stop()
                await self._release_browser_identity(subagent_id)
            except Exception as exc:
                failures.append(exc)
                continue
            self._parent_route.bindings.release(session.binding)
            async with self._lock:
                if self._cleanup_pending.get(subagent_id) is session:
                    self._cleanup_pending.pop(subagent_id, None)
        # Retain and retry resources from failures before a child Session was
        # created, rather than losing the only cleanup owner.
        for subagent_id in tuple(self._browser_identities):
            if (subagent_id in self._live or subagent_id in self._reserved
                    or subagent_id in self._cleanup_pending):
                continue
            try:
                resources = self._browser_resources.get(subagent_id)
                if resources is not None:
                    await resources.gateway.close()
                await self._release_browser_identity(subagent_id)
            except Exception as exc:
                failures.append(exc)
        if failures:
            raise ExceptionGroup(
                "one or more half-started External children did not confirm exit",
                failures,
            )


__all__ = [
    "ExternalSubagentExecution",
    "ExternalSubagentExecutionFactory",
    "SUPPORTED_SUBAGENT_PROVIDERS",
]
