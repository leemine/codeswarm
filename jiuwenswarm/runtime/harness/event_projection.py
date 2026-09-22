# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Project External output through the existing history and runtime push path."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

from openjiuwen.harness_protocol import (
    HarnessEvent,
    TurnEventKind,
    TurnLifecycleEvent,
)
from openjiuwen.harness_providers.io_adapter import ProjectedOutput

from jiuwenswarm.runtime.host_services import send_runtime_push
from jiuwenswarm.server.runtime.session.history_io import run_history_io
from jiuwenswarm.server.runtime.session.session_history import append_history_record
from jiuwenswarm.server.runtime.session.session_metadata import (
    build_server_push_message,
    get_session_delivery_context,
    get_session_metadata,
)
from jiuwenswarm.server.utils.stream_utils import parse_stream_chunk

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class _TurnProjection:
    request_id: str
    channel_id: str
    mode: str
    text: str = ""
    final_seen: bool = False
    error_seen: bool = False


class ExternalEventProjection:
    """Track owned output and persist/push only after request ownership ends."""

    def __init__(self, session_id: str) -> None:
        self._session_id = session_id
        self._turns: dict[str, _TurnProjection] = {}
        self._terminal_errors: dict[str, str] = {}

    async def observe(self, envelope: HarnessEvent) -> None:
        """Retain normalized terminal failures before IO projection drops them."""

        turn_id = envelope.turn_id
        event = envelope.event
        if (
            turn_id
            and isinstance(event, TurnLifecycleEvent)
            and event.kind is TurnEventKind.FAILED
            and event.result is not None
            and event.result.error is not None
        ):
            self._terminal_errors[turn_id] = event.result.error.message

    def register_turn(
        self,
        turn_id: str,
        *,
        request_id: str,
        channel_id: str,
        mode: str,
    ) -> None:
        self._turns.setdefault(
            turn_id,
            _TurnProjection(
                request_id=request_id,
                channel_id=channel_id or "web",
                mode=mode or "unknown",
            ),
        )

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
            self._note_payload(state, payload)
        if item.terminal is not None:
            self._turns.pop(turn_id, None)
        return payload

    def payload(
        self,
        item: ProjectedOutput,
        *,
        state: _TurnProjection | None = None,
    ) -> dict[str, Any] | None:
        if item.chunk is not None:
            return parse_stream_chunk(
                item.chunk,
                _has_streamed_content=bool(state and state.text),
            )
        if item.terminal is TurnEventKind.FINISHED:
            return {"event_type": "chat.final", "content": ""}
        if item.terminal is TurnEventKind.FAILED:
            return {
                "event_type": "chat.error",
                "error": self._terminal_errors.pop(
                    item.turn_id or "", "External execution Turn failed"
                ),
            }
        if item.terminal is TurnEventKind.ABORTED:
            return {"event_type": "chat.final", "content": ""}
        return None

    async def __call__(self, item: ProjectedOutput) -> None:
        """Persist then push output whose live request owner has gone away."""

        turn_id = item.turn_id
        if not turn_id:
            return
        state = self._turns.get(turn_id)
        if state is None:
            state = self._fallback_state(turn_id)
            self._turns[turn_id] = state
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
                    payload = {**payload, "content": state.text}
                await self._publish(state, payload)
        except Exception:
            logger.exception(
                "Detached External output projection failed: session_id=%s turn_id=%s",
                self._session_id,
                turn_id,
            )
        finally:
            if item.terminal is not None:
                self._terminal_errors.pop(turn_id, None)
                self._turns.pop(turn_id, None)

    async def close(self) -> None:
        self._turns.clear()
        self._terminal_errors.clear()

    @staticmethod
    def _note_payload(state: _TurnProjection, payload: dict[str, Any]) -> None:
        event_type = payload.get("event_type")
        content = payload.get("content")
        if event_type == "chat.delta" and isinstance(content, str):
            state.text += content
        elif event_type == "chat.final":
            state.final_seen = True
            if isinstance(content, str) and content:
                state.text = content
        elif event_type == "chat.error":
            state.error_seen = True

    def _fallback_state(self, turn_id: str) -> _TurnProjection:
        delivery = get_session_delivery_context(self._session_id) or {}
        metadata = get_session_metadata(
            self._session_id, enable_writeback=False
        ) or {}
        return _TurnProjection(
            request_id=f"external-turn-{turn_id}",
            channel_id=str(
                delivery.get("channel_id") or metadata.get("channel_id") or "web"
            ),
            mode=str(metadata.get("mode") or "unknown"),
        )

    async def _publish(
        self,
        state: _TurnProjection,
        payload: dict[str, Any],
    ) -> None:
        event_type = str(payload.get("event_type") or "")
        if event_type.startswith("chat.") or event_type == "context.usage":
            extra = {
                key: value
                for key, value in payload.items()
                if key not in {"event_type", "content", "error"}
            }
            await run_history_io(
                append_history_record,
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
        await send_runtime_push(
            build_server_push_message(
                session_id=self._session_id,
                request_id=state.request_id,
                payload=payload,
                fallback_channel_id=state.channel_id,
            )
        )


__all__ = ["ExternalEventProjection"]
