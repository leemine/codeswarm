# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Project External output through the existing history and runtime push path."""

from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass
from typing import Any

from openjiuwen.harness_protocol import (
    HarnessEvent,
    TurnEventKind,
    TurnLifecycleEvent,
)
from openjiuwen.harness_providers.io_adapter import ProjectedOutput

from jiuwenswarm.runtime.host_services import send_runtime_push
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
_TERMINAL_TOMBSTONES = 1024
_DURABLE_EVENT_TYPES = frozenset(
    {
        "chat.ask_user_question",
        "chat.error",
        "chat.final",
        "chat.tool_result",
        "context.usage",
        "harness.activate_interaction",
    }
)


@dataclass(slots=True)
class _TurnProjection:
    request_id: str
    channel_id: str
    mode: str
    text: str = ""
    final_seen: bool = False
    error_seen: bool = False


class _HistoryPersistenceUnconfirmed(RuntimeError):
    pass


class ExternalEventProjection:
    """Track owned output and persist/push only after request ownership ends."""

    def __init__(self, session_id: str) -> None:
        self._session_id = session_id
        self._turns: dict[str, _TurnProjection] = {}
        self._terminal_errors: dict[str, str] = {}
        self._terminal_error_codes: dict[str, str] = {}
        self._terminal_cancellations: dict[str, str] = {}
        self._terminated_turns: set[str] = set()
        self._terminal_order: deque[str] = deque()

    async def observe(self, envelope: HarnessEvent) -> None:
        """Retain normalized terminal failures before IO projection drops them."""

        turn_id = envelope.turn_id
        event = envelope.event
        if turn_id and isinstance(event, TurnLifecycleEvent) and event.result is not None:
            if event.kind is TurnEventKind.FAILED and event.result.error is not None:
                self._terminal_errors[turn_id] = event.result.error.message
                if event.result.error.code:
                    self._terminal_error_codes[turn_id] = event.result.error.code
            elif (
                event.kind is TurnEventKind.ABORTED
                and event.result.termination is not None
            ):
                self._terminal_cancellations[turn_id] = (
                    event.result.termination.message
                )

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
            self._remember_terminal(turn_id)
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
        if item.terminal is not None:
            turn_id = item.turn_id or ""
            return harness_terminal_payload(
                item.terminal,
                error=self._terminal_errors.pop(turn_id, None),
                error_code=self._terminal_error_codes.pop(turn_id, None),
                cancellation=self._terminal_cancellations.pop(turn_id, None),
            )
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
            state = self._fallback_state(turn_id)
            self._turns[turn_id] = state
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
                    payload = {**payload, "content": state.text}
                await self._publish(
                    state,
                    payload,
                    delivery_id=self._delivery_id(turn_id, item, payload),
                )
            delivered = True
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
            if item.terminal is not None and delivered:
                self._terminal_errors.pop(turn_id, None)
                self._turns.pop(turn_id, None)
                self._remember_terminal(turn_id)

    async def close(self) -> None:
        self._turns.clear()
        self._terminal_errors.clear()
        self._terminal_error_codes.clear()
        self._terminal_cancellations.clear()
        self._terminated_turns.clear()
        self._terminal_order.clear()

    def _remember_terminal(self, turn_id: str) -> None:
        if turn_id in self._terminated_turns:
            return
        if len(self._terminal_order) >= _TERMINAL_TOMBSTONES:
            expired = self._terminal_order.popleft()
            self._terminated_turns.discard(expired)
        self._terminal_order.append(turn_id)
        self._terminated_turns.add(turn_id)

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

    async def _report_delivery_failure(
        self,
        state: _TurnProjection,
        turn_id: str,
        error: BaseException,
    ) -> None:
        """Best-effort product diagnostic without claiming result delivery."""

        try:
            code = (
                _HISTORY_PERSISTENCE_UNCONFIRMED
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
        *,
        delivery_id: str,
    ) -> None:
        event_type = str(payload.get("event_type") or "")
        if (
            event_type.startswith("chat.")
            or event_type == "context.usage"
            or event_type == "harness.activate_interaction"
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
            return (
                f"harness:{turn_id}:chunk:{chunk_type}:{chunk_index}:{event_type}"
            )
        terminal = getattr(item.terminal, "value", item.terminal)
        return f"harness:{turn_id}:terminal:{terminal}:{event_type}"


__all__ = ["ExternalEventProjection"]
