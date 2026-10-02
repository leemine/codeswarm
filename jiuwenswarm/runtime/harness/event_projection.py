# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Project External output through the existing history and runtime push path."""

from __future__ import annotations

import logging
import hashlib
import json
import os
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from openjiuwen.harness_protocol import (
    HarnessEvent,
    ProviderEvent,
    TurnEventKind,
    TurnLifecycleEvent,
)
from openjiuwen.core.session.stream.base import OutputSchema
from openjiuwen.core.single_agent.schema.agent_result import Artifact
from openjiuwen.harness.subagent_runtime import (
    SUBAGENT_ACTIVITY_EVENT_TYPE,
    SUBAGENT_UPDATED_EVENT_TYPE,
)
from openjiuwen.harness_providers.io_adapter import ProjectedOutput
from openjiuwen.harness_providers.output_buffer import (
    OutputBudget,
    OutputBudgetExceeded,
    OutputText,
)

from jiuwenswarm.runtime.host_services import send_runtime_push
from jiuwenswarm.runtime.harness.surface_projection import SurfaceResultProjection
from jiuwenswarm.runtime.terminal_outcome import harness_terminal_payload
from jiuwenswarm.server.runtime.session.history_io import run_history_io
from jiuwenswarm.server.runtime.session.session_history import (
    append_history_record,
    append_history_record_durable,
    wait_for_history_receipt,
)
from jiuwenswarm.server.runtime.session.session_metadata import (
    build_server_push_message,
    get_session_delivery_context,
    get_session_metadata,
)
from jiuwenswarm.server.utils.stream_utils import parse_stream_chunk

logger = logging.getLogger(__name__)
_HISTORY_PERSISTENCE_UNCONFIRMED = "HISTORY_PERSISTENCE_UNCONFIRMED"
_DELIVERY_UNCONFIRMED = "DETACHED_DELIVERY_UNCONFIRMED"
_SURFACE_PERSISTENCE_UNCONFIRMED = "SURFACE_PERSISTENCE_UNCONFIRMED"
_TERMINAL_TOMBSTONES = 1024
_DURABLE_EVENT_TYPES = frozenset(
    {
        "chat.ask_user_question",
        "chat.error",
        "chat.final",
        "chat.tool_result",
        "context.usage",
        "harness.activate_interaction",
        "team.member_turn",
    }
)


@dataclass(slots=True)
class _TurnProjection:
    request_id: str
    channel_id: str
    mode: str
    text: OutputText = field(default_factory=OutputText)
    final_seen: bool = False
    error_seen: bool = False
    delivery_failed: bool = False
    goal_attempt: bool = False
    goal_id: str | None = None
    detached: bool = False
    pending_terminal: dict[str, Any] | None = None


class _HistoryPersistenceUnconfirmed(RuntimeError):
    pass


def _codex_internal_status(value: Any, message: Any) -> dict[str, Any]:
    native = str(getattr(value, "value", value) or "").strip()
    error_message = str(message or "").strip()
    if native in {"pendingInit", "inProgress", "running"}:
        status, outcome, lifecycle, reason = "running", None, "live", None
    elif native == "completed":
        status, outcome, lifecycle, reason = "idle", "completed", "live", None
    elif native in {"interrupted", "cancelled", "canceled"}:
        status, outcome, lifecycle, reason = "idle", "cancelled", "live", None
    elif native in {"shutdown"}:
        status, outcome, lifecycle, reason = "closed", "completed", "closed", "manual"
    elif native in {"notFound"}:
        status, outcome, lifecycle, reason = "closed", "failed", "closed", "failed"
    else:
        status, outcome, lifecycle, reason = "idle", "failed", "live", None
    error = None
    if outcome == "failed":
        error = {
            "code": "CODEX_INTERNAL_SUBAGENT_FAILED",
            "message": error_message
            or f"Codex internal subagent status: {native or 'unknown'}",
        }
    return {
        "status": status,
        "turn_outcome": outcome,
        "lifecycle": lifecycle,
        "closed_reason": reason,
        "error": error,
    }


class ExternalEventProjection:
    """Track owned output and persist/push only after request ownership ends."""

    def __init__(
        self,
        session_id: str,
        *,
        on_detached_terminal: Callable[[str], Awaitable[None]] | None = None,
        workspace_root: Path | None = None,
        work_mode: str | None = None,
        cwd: Path | None = None,
        outputs_dir: Path | None = None,
        provider_id: str = "",
        projection_scope: str | None = None,
        artifact_sink=None,
        require_artifact_attribution: bool = False,
    ) -> None:
        self._session_id = session_id
        self._on_detached_terminal = on_detached_terminal
        self._workspace_root = workspace_root.resolve() if workspace_root else None
        self._text_budget = OutputBudget()
        self._max_turns = 128
        self._turns: dict[str, _TurnProjection] = {}
        self._terminal_errors: dict[str, str] = {}
        self._terminal_error_codes: dict[str, str] = {}
        self._terminal_cancellations: dict[str, str | None] = {}
        self._terminated_turns: set[str] = set()
        self._terminal_order: deque[str] = deque()
        self._codex_internal_subagents: dict[str, dict[str, Any]] = {}
        self._codex_internal_event_index = 0
        self._surface_projection = (
            SurfaceResultProjection(
                projection_scope or session_id,
                work_mode=work_mode,
                workspace_root=self._workspace_root,
                cwd=cwd,
                outputs_dir=outputs_dir,
                provider_id=provider_id,
                artifact_sink=artifact_sink or self.publish_product_artifact,
                require_artifact_attribution=require_artifact_attribution,
            )
            if (
                work_mode in {"work", "code"}
                and self._workspace_root is not None
                and cwd is not None
                and provider_id
            )
            else None
        )

    async def observe(self, envelope: HarnessEvent) -> None:
        """Retain normalized terminal failures before IO projection drops them."""

        turn_id = envelope.turn_id
        event = envelope.event
        if turn_id in self._terminated_turns:
            return
        if self._surface_projection is not None:
            await self._surface_projection.observe(envelope)
        if (
            isinstance(event, ProviderEvent)
            and event.provider == "codex"
            and event.event_type.startswith("internal_subagent/")
        ):
            await self._project_codex_internal_subagent(event)
        if (
            turn_id
            and isinstance(event, TurnLifecycleEvent)
            and event.result is not None
        ):
            if (
                len(self._terminal_errors) + len(self._terminal_cancellations)
                >= self._max_turns
            ):
                raise OutputBudgetExceeded("terminal detail count budget exhausted")
            detail = event.result.error or event.result.termination
            if detail is not None and len(str(detail).encode("utf-8")) > 64 * 1024:
                raise OutputBudgetExceeded("terminal detail byte budget exhausted")
            if event.kind is TurnEventKind.FAILED and event.result.error is not None:
                self._terminal_errors[turn_id] = event.result.error.message
                if event.result.error.code:
                    self._terminal_error_codes[turn_id] = event.result.error.code
            elif (
                event.kind is TurnEventKind.ABORTED
                and event.result.termination is not None
            ):
                self._terminal_cancellations[turn_id] = event.result.termination.message

    def register_turn(
        self,
        turn_id: str,
        *,
        request_id: str,
        channel_id: str,
        mode: str,
        goal_attempt: bool = False,
        goal_id: str | None = None,
    ) -> None:
        if any(
            len(value.encode("utf-8")) > 1024
            for value in (turn_id, request_id, channel_id, mode)
        ):
            raise OutputBudgetExceeded(
                "projection correlation identifier byte budget exhausted"
            )
        if turn_id in self._turns or turn_id in self._terminated_turns:
            return
        if len(self._turns) >= self._max_turns:
            raise OutputBudgetExceeded("projection Turn count budget exhausted")
        self._turns.setdefault(
            turn_id,
            _TurnProjection(
                request_id=request_id,
                channel_id=channel_id or "web",
                mode=mode or "unknown",
                text=OutputText(budget=self._text_budget),
                goal_attempt=goal_attempt,
                goal_id=goal_id,
            ),
        )
        if self._surface_projection is not None:
            self._surface_projection.register_turn(turn_id)

    def owned_payload(self, item: ProjectedOutput) -> dict[str, Any] | None:
        """Parse owned output and retain its prefix for a possible handoff."""

        turn_id = item.turn_id
        if not turn_id:
            return None
        state = self._turns.get(turn_id)
        if state is None:
            return None
        payload = self.payload(item, state=state)
        if payload is not None:
            try:
                self._note_payload(state, payload)
            except OutputBudgetExceeded:
                state.delivery_failed = True
                raise
        if item.terminal is not None and not state.goal_attempt:
            self._turns.pop(turn_id, None)
            state.text.close()
            self._remember_terminal(turn_id)
        return payload

    def accepts_turn_output(self, turn_id: str | None) -> bool:
        return turn_id is None or turn_id not in self._terminated_turns

    async def persist_member_output(
        self, item: ProjectedOutput, payload: dict[str, Any]
    ) -> bool:
        """Keep a failed history receipt visible through the original Team stream."""
        turn_id = item.turn_id or "product-interaction"
        state = self._turns.get(turn_id)
        failed = bool(state and state.delivery_failed)
        try:
            await self._persist_member_output(item, payload)
        except Exception:
            state = self._turns.get(turn_id)
            if state is not None:
                state.delivery_failed = True
            logger.exception(
                "Team history persistence unconfirmed: session=%s turn=%s",
                self._session_id,
                turn_id,
            )
            return False
        return not failed

    async def _persist_member_output(
        self, item: ProjectedOutput, payload: dict[str, Any]
    ) -> None:
        """Persist Team-owned output before its original broadcaster sees it.

        Uses this projection's existing text buffer and terminal tombstones.
        Team owns delivery; this method never sends a second server push.
        """
        turn_id = item.turn_id or "product-interaction"
        state = self._turns.get(turn_id)
        if state is None:
            state = self._fallback_state(turn_id)
            self._turns[turn_id] = state
        et = payload.get("event_type")
        if et == "chat.delta":
            self._note_payload(state, payload)
            return
        owner = json.dumps(
            {
                key: payload.get(key)
                for key in (
                    "member_session_id",
                    "provider_session_id",
                    "request_id",
                )
            },
            sort_keys=True,
        ).encode()
        delivery_id = (
            self._delivery_id(turn_id, item, payload)
            + ":"
            + hashlib.sha256(owner).hexdigest()
        )
        if state.text and et in {
            "chat.ask_user_question",
            "chat.tool_call",
            "team.member_turn",
            "chat.error",
        }:
            await self._publish(
                state,
                {**payload, "event_type": "chat.final", "content": state.text.read()},
                delivery_id=delivery_id + ":text",
                push=False,
            )
            state.text.clear()
        await self._publish(state, payload, delivery_id=delivery_id, push=False)
        if et == "chat.final":
            state.text.clear()
        if item.terminal is not None:
            self._turns.pop(turn_id, None)
            state.text.close()
            self._remember_terminal(turn_id)

    def payload(
        self,
        item: ProjectedOutput,
        *,
        state: _TurnProjection | None = None,
    ) -> dict[str, Any] | None:
        if item.chunk is not None:
            payload = parse_stream_chunk(
                item.chunk,
                _has_streamed_content=bool(state and state.text),
            )
            payload = self._with_surface_activity(item, payload)
            if state is not None and state.goal_attempt and payload is not None:
                if payload.get("event_type") == "chat.error":
                    state.pending_terminal = payload
                    return None
                if payload.get("event_type") == "chat.final":
                    # A Provider final belongs to an attempt. The existing
                    # Goal owner must assess it before ending the root stream.
                    content = str(payload.get("content") or "")
                    prefix = state.text.read()
                    if prefix:
                        content = (
                            content[len(prefix) :] if content.startswith(prefix) else ""
                        )
                    return (
                        {"event_type": "chat.delta", "content": content}
                        if content
                        else None
                    )
            return payload
        if item.terminal is not None:
            turn_id = item.turn_id or ""
            payload = harness_terminal_payload(
                item.terminal,
                error=self._terminal_errors.pop(turn_id, None),
                error_code=self._terminal_error_codes.pop(turn_id, None),
                cancellation=self._terminal_cancellations.pop(turn_id, None),
            )
            summary = (
                self._surface_projection.summary(turn_id)
                if self._surface_projection is not None
                else None
            )
            if summary is not None:
                payload = {**payload, "surface_projection": summary.record()}
                if (
                    summary.status == "unconfirmed"
                    and payload.get("event_type") == "chat.final"
                ):
                    payload = {
                        "event_type": "chat.error",
                        "error": "Surface result persistence could not be confirmed",
                        "code": _SURFACE_PERSISTENCE_UNCONFIRMED,
                        "terminal_status": "failed",
                        "provider_terminal_status": "completed",
                        "surface_projection": summary.record(),
                    }
            if state is not None and state.goal_attempt:
                state.pending_terminal = payload
                return None
            return payload
        return None

    async def __call__(self, item: ProjectedOutput) -> None:
        """Persist then push output whose live request owner has gone away."""

        turn_id = item.turn_id
        if not turn_id:
            return
        if turn_id in self._terminated_turns:
            return
        state = self._turns.get(turn_id)
        if state is None:
            if len(self._turns) >= self._max_turns:
                raise OutputBudgetExceeded("projection Turn count budget exhausted")
            state = self._fallback_state(turn_id)
            self._turns[turn_id] = state
        state.detached = True
        if state.delivery_failed:
            if item.terminal is not None:
                state.text.close()
                self._turns.pop(turn_id, None)
                self._remember_terminal(turn_id)
            return
        delivered = False
        try:
            payload = self.payload(item, state=state)
            if payload is not None:
                self._note_payload(state, payload)
                # A terminal empty final must carry the full visible prefix so
                # detached history gets one durable assistant message.
                if (
                    item.terminal is not None
                    and payload.get("event_type") == "chat.final"
                    and not payload.get("content")
                ):
                    payload = {**payload, "content": state.text.read()}
                await self._publish(
                    state,
                    payload,
                    delivery_id=self._delivery_id(turn_id, item, payload),
                )
            if (
                item.terminal is not None
                and not state.goal_attempt
                and self._on_detached_terminal is not None
            ):
                await self._on_detached_terminal(turn_id)
            delivered = True
        except OutputBudgetExceeded:
            state.delivery_failed = True
            raise
        except Exception as exc:
            logger.exception(
                "Detached External output projection failed: session_id=%s turn_id=%s",
                self._session_id,
                turn_id,
            )
            await self._report_delivery_failure(state, turn_id, exc)
        finally:
            # Keep terminal projection state after an uncertain delivery.  A
            # reconnect/replay can reuse its stable delivery id, skip the
            # already-durable row, and retry only the product push.
            if item.terminal is not None and delivered and not state.goal_attempt:
                self._terminal_errors.pop(turn_id, None)
                self._turns.pop(turn_id, None)
                state.text.close()
                self._remember_terminal(turn_id)

    async def finish_goal_turn(
        self,
        turn_id: str,
        *,
        terminal_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Release an assessed attempt; only its owner can finish the root.

        ``None`` is an intermediate attempt, with no product terminal. This
        same seam handles detached delivery without a second history writer.
        Failed persistence retains the state so a retry uses the same key.
        """
        state = self._turns.get(turn_id)
        if state is None or not state.goal_attempt:
            return None
        payload = dict(terminal_payload) if terminal_payload is not None else None
        if payload is not None and state.goal_id:
            payload.setdefault("goal_id", state.goal_id)
        if payload is not None and state.detached:
            if payload.get("event_type") == "chat.final" and not payload.get("content"):
                payload["content"] = state.text.read()
            await self._publish(
                state,
                payload,
                delivery_id=f"harness:{turn_id}:goal-root-terminal",
            )
        self._turns.pop(turn_id, None)
        state.text.close()
        self._remember_terminal(turn_id)
        return None if state.detached else payload

    async def output_failed(self, error: OutputBudgetExceeded) -> None:
        for turn_id, state in tuple(self._turns.items()):
            state.delivery_failed = True
            await self._report_delivery_failure(state, turn_id, error)

    async def close(self) -> None:
        for state in self._turns.values():
            state.text.close()
        self._turns.clear()
        self._terminal_errors.clear()
        self._terminal_error_codes.clear()
        self._terminal_cancellations.clear()
        self._terminated_turns.clear()
        self._terminal_order.clear()
        self._codex_internal_subagents.clear()
        if self._surface_projection is not None:
            self._surface_projection.close()

    async def _project_codex_internal_subagent(self, event: ProviderEvent) -> None:
        payload = dict(event.payload)
        native_id = str(payload.get("subagent_id") or "").strip()
        if not native_id:
            return
        subagent_id = f"codex:{native_id}"
        now_ms = time.time() * 1000
        current = self._codex_internal_subagents.get(subagent_id)
        if current is None:
            if len(self._codex_internal_subagents) >= self._max_turns:
                raise OutputBudgetExceeded(
                    "Codex internal subagent count budget exhausted"
                )
            current = {
                "subagent_id": subagent_id,
                "sub_session_id": subagent_id,
                "parent_session_id": self._session_id,
                "subagent_type": "codex_internal",
                "display_name": f"Codex agent {native_id[:12]}",
                "role": "Codex internal subagent",
                "task_description": "Codex internal task",
                "created_at": now_ms,
                "revision": 0,
            }
            self._codex_internal_subagents[subagent_id] = current

        if event.event_type == "internal_subagent/status":
            prompt = str(payload.get("prompt") or "").strip()
            if prompt:
                current["task_description"] = prompt
            status = _codex_internal_status(
                payload.get("status"), payload.get("message")
            )
            status["closed_at"] = now_ms if status["lifecycle"] == "closed" else None
            current.update(
                {
                    **status,
                    "updated_at": now_ms,
                    "revision": int(current["revision"]) + 1,
                    "can_send_input": False,
                    "needs_resume": False,
                    "controllable": False,
                    "provider": "codex",
                    "native_thread_id": native_id,
                }
            )
            await self.project_product_chunk(
                OutputSchema(
                    type=SUBAGENT_UPDATED_EVENT_TYPE,
                    index=self._next_codex_internal_index(),
                    payload={"subagent_updated": dict(current)},
                )
            )
            return

        activity_kind = str(payload.get("activity_kind") or "activity")
        seq = self._next_codex_internal_index()
        await self.project_product_chunk(
            OutputSchema(
                type=SUBAGENT_ACTIVITY_EVENT_TYPE,
                index=seq,
                payload={
                    "subagent_activity": {
                        "activity_id": str(
                            payload.get("activity_id") or f"{subagent_id}:{seq}"
                        ),
                        "subagent_id": subagent_id,
                        "parent_session_id": self._session_id,
                        "task_id": str(payload.get("activity_id") or "codex-internal"),
                        "seq": seq,
                        "kind": "thinking",
                        "summary": f"Codex internal agent {activity_kind}",
                        "at_ms": now_ms,
                    }
                },
            )
        )

    def _next_codex_internal_index(self) -> int:
        self._codex_internal_event_index += 1
        return self._codex_internal_event_index

    def _remember_terminal(self, turn_id: str) -> None:
        self._terminal_errors.pop(turn_id, None)
        self._terminal_error_codes.pop(turn_id, None)
        self._terminal_cancellations.pop(turn_id, None)
        if turn_id in self._terminated_turns:
            return
        if len(self._terminal_order) >= _TERMINAL_TOMBSTONES:
            expired = self._terminal_order.popleft()
            self._terminated_turns.discard(expired)
        self._terminal_order.append(turn_id)
        self._terminated_turns.add(turn_id)
        if self._surface_projection is not None:
            self._surface_projection.forget(turn_id)

    def _with_surface_activity(
        self,
        item: ProjectedOutput,
        payload: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        if payload is None or self._surface_projection is None or not item.turn_id:
            return payload
        event_type = payload.get("event_type")
        tool: dict[str, Any] | None = None
        if event_type == "chat.tool_call":
            candidate = payload.get("tool_call")
            tool = candidate if isinstance(candidate, dict) else None
            item_id = tool.get("tool_call_id") if tool is not None else None
        elif event_type == "chat.tool_result":
            item_id = payload.get("tool_call_id")
        else:
            return payload
        if not isinstance(item_id, str) or not item_id:
            return payload
        activity = self._surface_projection.activity(item.turn_id, item_id)
        if activity is None:
            return payload
        record = activity.record()
        if event_type == "chat.tool_call" and tool is not None:
            return {**payload, "tool_call": {**tool, "surface_projection": record}}
        payload = {**payload, "surface_projection": record}
        if activity.status:
            payload.setdefault("status", activity.status)
            payload.setdefault(
                "success",
                activity.status
                not in {
                    "blocked",
                    "declined",
                    "denied",
                    "error",
                    "failed",
                    "failure",
                    "rejected",
                },
            )
        return payload

    async def project_product_chunk(self, chunk: Any) -> None:
        """Reuse the Native subagent parser/history seam for product events."""

        from jiuwenswarm.server.runtime.agent_adapter.subagent_projection import (
            parse_subagent_stream_chunk,
        )

        payload = await run_history_io(
            parse_subagent_stream_chunk,
            chunk,
            parent_session_id=self._session_id,
        )
        if payload is None:
            return
        state = next(reversed(self._turns.values()), None)
        if state is None:
            state = self._fallback_state("product-subagent")
        await send_runtime_push(
            build_server_push_message(
                session_id=self._session_id,
                request_id=state.request_id,
                payload=payload,
                fallback_channel_id=state.channel_id,
            )
        )

    async def publish_product_interaction(
        self,
        payload: dict[str, Any],
        delivery_id: str,
    ) -> None:
        """Persist and push a host-owned product interaction exactly once."""

        if payload.get("event_type") != "chat.ask_user_question":
            raise ValueError("product interaction must be chat.ask_user_question")
        if not str(payload.get("request_id") or "").strip():
            raise ValueError("product interaction request_id is required")
        if not isinstance(payload.get("questions"), list):
            raise ValueError("product interaction questions must be a list")
        state = next(reversed(self._turns.values()), None)
        if state is None:
            state = self._fallback_state("product-interaction")
        from jiuwenswarm.runtime.context import get_current_runtime
        from jiuwenswarm.runtime.events import RuntimeEvent

        payload = dict(payload)
        runtime = get_current_runtime()
        if runtime is not None:
            await runtime.register_host_interaction(
                RuntimeEvent.control(
                    request_id=state.request_id,
                    channel_id=state.channel_id,
                    session_id=self._session_id,
                    payload=payload,
                )
            )
        await self._publish(state, payload, delivery_id=delivery_id)

    async def publish_product_artifact(
        self,
        artifact: Artifact,
        file_path: Path,
    ) -> None:
        """Deliver one host-projected Surface Artifact through the file service."""

        workspace = self._workspace_root
        if workspace is None:
            raise RuntimeError("product Artifact projection requires a workspace")
        lexical = Path(os.path.abspath(os.path.normpath(str(file_path))))
        resolved = file_path.resolve(strict=True)
        if lexical != resolved or not resolved.is_file():
            raise ValueError("product Artifact path must be a non-symlink file")
        try:
            relative_path = resolved.relative_to(workspace).as_posix()
        except ValueError as exc:
            raise ValueError("product Artifact path is outside the workspace") from exc
        if not isinstance(artifact, Artifact):
            raise TypeError("product Artifact must use the core Artifact model")
        artifact_id = str(artifact.artifactId or "").strip()
        if not artifact_id or len(artifact.parts) != 1:
            raise ValueError("product Artifact requires one identified file part")
        part_url = str(artifact.parts[0].url or "").strip()
        metadata_path = str(
            artifact.metadata.get("workspace_relative_path") or ""
        ).strip()
        if part_url != relative_path or metadata_path != relative_path:
            raise ValueError("product Artifact file identity does not match its path")

        state = next(reversed(self._turns.values()), None)
        if state is None:
            state = self._fallback_state("product-artifact")
        delivery = get_session_delivery_context(self._session_id) or {}
        metadata = get_session_metadata(self._session_id, enable_writeback=False) or {}
        route_metadata = delivery.get("route_metadata")
        from jiuwenswarm.agents.harness.common.tools.send_file_to_user import (
            SendFileToolkit,
        )

        toolkit = SendFileToolkit(
            request_id=state.request_id,
            session_id=self._session_id,
            channel_id=state.channel_id,
            metadata=route_metadata if isinstance(route_metadata, dict) else None,
            user_id=str(metadata.get("user_id") or ""),
            project_dir=str(metadata.get("project_dir") or "") or None,
        )
        await toolkit.deliver_projected_artifact(
            resolved,
            artifact.model_dump(mode="json", exclude_none=True),
        )

    async def replay_product_artifacts(self) -> None:
        """Schedule history recovery without coupling it to Browser admission."""
        from jiuwenswarm.agents.harness.common.tools.send_file_to_user import (
            SendFileToolkit,
        )
        from jiuwenswarm.runtime.host_services import enqueue_artifact_retry

        if enqueue_artifact_retry(self._session_id):
            return
        state = self._fallback_state("product-artifact-replay")
        delivery = get_session_delivery_context(self._session_id) or {}
        route_metadata = delivery.get("route_metadata")
        await SendFileToolkit(
            request_id=state.request_id,
            session_id=self._session_id,
            channel_id=state.channel_id,
            metadata=route_metadata if isinstance(route_metadata, dict) else None,
        ).replay_projected_artifacts(require_origin=True)

    async def _report_delivery_failure(
        self,
        state: _TurnProjection,
        turn_id: str,
        error: BaseException,
    ) -> None:
        """Best-effort product diagnostic without claiming result delivery."""

        try:
            code = (
                error.code
                if isinstance(error, OutputBudgetExceeded)
                else _HISTORY_PERSISTENCE_UNCONFIRMED
                if isinstance(error, _HistoryPersistenceUnconfirmed)
                else _DELIVERY_UNCONFIRMED
            )
            await send_runtime_push(
                build_server_push_message(
                    session_id=self._session_id,
                    request_id=state.request_id,
                    payload={
                        "event_type": "chat.error",
                        "code": code,
                        "error": "Detached result delivery could not be confirmed",
                        "turn_id": turn_id,
                        "error_type": type(error).__name__,
                    },
                    fallback_channel_id=state.channel_id,
                )
            )
        except Exception:
            logger.warning(
                "Detached External delivery diagnostic failed: session_id=%s turn_id=%s",
                self._session_id,
                turn_id,
                exc_info=True,
            )

    @staticmethod
    def _note_payload(state: _TurnProjection, payload: dict[str, Any]) -> None:
        event_type = payload.get("event_type")
        content = payload.get("content")
        if event_type == "chat.delta" and isinstance(content, str):
            state.text.append(content)
        elif event_type == "chat.final":
            state.final_seen = True
            if isinstance(content, str) and content:
                state.text.replace(content)
        elif event_type == "chat.error":
            state.error_seen = True

    def _fallback_state(self, turn_id: str) -> _TurnProjection:
        delivery = get_session_delivery_context(self._session_id) or {}
        metadata = get_session_metadata(self._session_id, enable_writeback=False) or {}
        return _TurnProjection(
            request_id=f"external-turn-{turn_id}",
            channel_id=str(
                delivery.get("channel_id") or metadata.get("channel_id") or "web"
            ),
            mode=str(metadata.get("mode") or "unknown"),
            text=OutputText(budget=self._text_budget),
        )

    async def _publish(
        self,
        state: _TurnProjection,
        payload: dict[str, Any],
        *,
        delivery_id: str,
        push: bool = True,
    ) -> None:
        event_type = str(payload.get("event_type") or "")
        if (
            event_type.startswith("chat.")
            or event_type == "context.usage"
            or event_type == "harness.activate_interaction"
            or event_type == "team.member_turn"
        ):
            extra = {
                key: value
                for key, value in payload.items()
                if key not in {"event_type", "content", "error"}
            }
            history_kwargs = dict(
                session_id=self._session_id,
                request_id=state.request_id,
                channel_id=state.channel_id,
                role="assistant",
                event_type=event_type,
                content=payload.get("content") or payload.get("error") or "",
                timestamp=time.time(),
                extra=extra or None,
                mode=state.mode,
            )
            if event_type in _DURABLE_EVENT_TYPES:
                receipt = await run_history_io(
                    append_history_record_durable,
                    **history_kwargs,
                    delivery_id=delivery_id,
                )
                if receipt is not None:
                    try:
                        await wait_for_history_receipt(receipt)
                    except Exception as exc:
                        raise _HistoryPersistenceUnconfirmed(
                            "detached history persistence was not confirmed"
                        ) from exc
            else:
                await run_history_io(append_history_record, **history_kwargs)
        if push:
            await send_runtime_push(
                build_server_push_message(
                    session_id=self._session_id,
                    request_id=state.request_id,
                    payload=payload,
                    fallback_channel_id=state.channel_id,
                )
            )

    @staticmethod
    def _delivery_id(
        turn_id: str,
        item: ProjectedOutput,
        payload: dict[str, Any],
    ) -> str:
        event_type = str(payload.get("event_type") or "unknown")
        if item.chunk is not None:
            chunk_type = getattr(item.chunk, "type", "unknown")
            chunk_type = getattr(chunk_type, "value", chunk_type)
            chunk_index = getattr(item.chunk, "index", "unknown")
            return f"harness:{turn_id}:chunk:{chunk_type}:{chunk_index}:{event_type}"
        terminal = getattr(item.terminal, "value", item.terminal)
        return f"harness:{turn_id}:terminal:{terminal}:{event_type}"


__all__ = ["ExternalEventProjection"]
