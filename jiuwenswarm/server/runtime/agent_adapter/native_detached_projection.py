# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Project Native Turn output after its request stream has released ownership.

The protocol reader chooses exactly one owner for every projected envelope.
This product-side sink uses the same chunk parser, history writer and push
transport as request-owned output; it never reads protocol events itself.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

from openjiuwen.harness_protocol import TurnEventKind
from openjiuwen.harness_providers.io_adapter import ProjectedOutput

from jiuwenswarm.runtime.host_services import send_runtime_push
from jiuwenswarm.server.runtime.session.session_history import append_history_record
from jiuwenswarm.server.runtime.session.session_metadata import (
    build_server_push_message,
    get_session_delivery_context,
    get_session_metadata,
)

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class _DetachedTurn:
    text: str = ""
    streamed: bool = False
    final_seen: bool = False
    error_seen: bool = False
    runtime_execution_id: str | None = None


class NativeDetachedProjection:
    """Persist then push envelopes with no live request owner."""

    def __init__(self, session_id: str, adapter: Any, *, runtime: Any = None) -> None:
        self._session_id = session_id
        self._adapter = adapter
        self._runtime = runtime
        self._turns: dict[str, _DetachedTurn] = {}

    async def close(self) -> None:
        """Settle detached Runtime owners if their provider Session stops early."""
        if self._runtime is not None:
            for state in self._turns.values():
                if state.runtime_execution_id is not None:
                    self._runtime.finish_detached_native_turn(
                        self._session_id,
                        state.runtime_execution_id,
                        TurnEventKind.ABORTED,
                    )
        self._turns.clear()

    async def __call__(self, item: ProjectedOutput) -> None:
        turn_id = item.turn_id
        if not turn_id:
            return
        state = self._turns.setdefault(turn_id, _DetachedTurn())
        try:
            if self._runtime is not None and state.runtime_execution_id is None:
                snapshot = self._runtime.begin_detached_native_turn(
                    self._session_id, turn_id
                )
                state.runtime_execution_id = snapshot.execution_id
            if item.chunk is not None:
                from jiuwenswarm.server.runtime.session.history_io import (
                    run_stream_parser,
                )

                chunk = item.chunk
                if chunk.type in {"llm_output", "content_chunk"}:
                    payload = chunk.payload
                    content = (
                        payload.get("content", "")
                        if isinstance(payload, dict)
                        else str(payload)
                    )
                    if isinstance(content, str):
                        state.text += content
                    state.streamed = True
                parsed = await run_stream_parser(
                    self._adapter._parse_stream_chunk,
                    chunk,
                    _has_streamed_content=state.streamed,
                    _parent_session_id=self._session_id,
                )
                if isinstance(parsed, dict):
                    if parsed.get("event_type") == "chat.final":
                        state.final_seen = True
                    if parsed.get("event_type") == "chat.error":
                        state.error_seen = True
                    await self._publish(turn_id, parsed)
            if item.terminal is not None:
                if item.terminal is TurnEventKind.FINISHED and state.text and not state.final_seen:
                    await self._publish(
                        turn_id, {"event_type": "chat.final", "content": state.text}
                    )
                elif item.terminal is TurnEventKind.FAILED and not state.error_seen:
                    await self._publish(
                        turn_id,
                        {"event_type": "chat.error", "error": "Native execution Turn failed"},
                    )
        except Exception:
            logger.exception(
                "Detached Native output projection failed: session_id=%s turn_id=%s",
                self._session_id, turn_id,
            )
        finally:
            if item.terminal is not None:
                if self._runtime is not None and state.runtime_execution_id is not None:
                    self._runtime.finish_detached_native_turn(
                        self._session_id, state.runtime_execution_id, item.terminal
                    )
                self._turns.pop(turn_id, None)

    async def _publish(self, turn_id: str, payload: dict[str, Any]) -> None:
        from jiuwenswarm.server.runtime.session.history_io import run_history_io

        state = self._turns[turn_id]
        if self._runtime is not None and (
            state.runtime_execution_id is None
            or not await self._runtime.observe_detached_native_turn(
                self._session_id, state.runtime_execution_id, payload
            )
        ):
            return

        delivery = get_session_delivery_context(self._session_id) or {}
        metadata = get_session_metadata(
            self._session_id, enable_writeback=False
        ) or {}
        channel_id = str(
            delivery.get("channel_id") or metadata.get("channel_id") or "web"
        )
        mode = str(metadata.get("mode") or "unknown")
        request_id = f"native-turn-{turn_id}"
        event_type = payload.get("event_type")
        if event_type == "goal.updated":
            from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
                JiuWenSwarmDeepAdapter,
            )

            await JiuWenSwarmDeepAdapter._record_goal_completed_history_if_needed(
                session_id=self._session_id,
                channel_id=channel_id,
                channel_metadata=delivery.get("route_metadata"),
                mode=mode,
                goal_payload=payload.get("goal"),
            )
        if isinstance(event_type, str) and (
            event_type.startswith("chat.") or event_type == "context.usage"
        ):
            extra = {
                key: value for key, value in payload.items()
                if key not in {"event_type", "content"}
            }
            await run_history_io(
                append_history_record,
                session_id=self._session_id,
                request_id=request_id,
                channel_id=channel_id,
                role="assistant",
                event_type=event_type,
                content=payload.get("content") or payload.get("error") or "",
                timestamp=time.time(),
                extra=extra or None,
                mode=mode,
            )
        await send_runtime_push(
            build_server_push_message(
                session_id=self._session_id,
                request_id=request_id,
                payload=payload,
                fallback_channel_id=channel_id,
            )
        )
