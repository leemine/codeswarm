# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Project Native Turn output after its request stream has released ownership.

The protocol reader chooses exactly one owner for every projected envelope.
This product-side sink uses the same chunk parser, history writer and push
transport as request-owned output; it never reads protocol events itself.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from openjiuwen.harness_protocol import TurnEventKind
from openjiuwen.harness_providers.io_adapter import ProjectedOutput
from openjiuwen.harness_providers.output_buffer import OutputBudget, OutputBudgetExceeded, OutputText

from jiuwenswarm.runtime.host_services import send_runtime_push
from jiuwenswarm.runtime.terminal_outcome import harness_terminal_payload
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
class _DetachedTurn:
    text: OutputText = field(default_factory=OutputText)
    streamed: bool = False
    final_seen: bool = False
    error_seen: bool = False
    delivery_failed: bool = False
    runtime_execution_id: str | None = None
    request_id: str | None = None


    def __post_init__(self) -> None:
        if isinstance(self.text, str):
            text = self.text
            self.text = OutputText()
            self.text.append(text)


class _HistoryPersistenceUnconfirmed(RuntimeError):
    pass


class NativeDetachedProjection:
    """Persist then push envelopes with no live request owner."""

    def __init__(
        self, session_id: str, adapter: Any, *, runtime: Any = None,
        request_id_for_turn: Callable[[str], str | None] | None = None,
    ) -> None:
        self._session_id = session_id
        self._text_budget = OutputBudget()
        self._max_turns = 128
        self._adapter = adapter
        self._runtime = runtime
        self._request_id_for_turn = request_id_for_turn
        self._turns: dict[str, _DetachedTurn] = {}
        self._terminated_turns: set[str] = set()
        self._terminal_order: deque[str] = deque()

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
        for state in self._turns.values():
            state.text.close()
        self._turns.clear()
        self._terminated_turns.clear()
        self._terminal_order.clear()

    async def __call__(self, item: ProjectedOutput) -> None:
        turn_id = item.turn_id
        if not turn_id:
            return
        if turn_id in self._terminated_turns:
            return
        state = self._turns.get(turn_id)
        if state is None:
            if len(self._turns) >= self._max_turns:
                raise OutputBudgetExceeded("Native projection Turn count budget exhausted")
            state = _DetachedTurn(text=OutputText(budget=self._text_budget))
            self._turns[turn_id] = state
        if state.delivery_failed:
            return
        delivered = False
        try:
            if state.request_id is None and self._request_id_for_turn is not None:
                state.request_id = self._request_id_for_turn(turn_id)
            if self._runtime is not None and state.runtime_execution_id is None:
                snapshot = self._runtime.begin_detached_native_turn(
                    self._session_id, turn_id, state.request_id
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
                        state.text.append(content)
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
                    await self._publish(
                        turn_id,
                        parsed,
                        delivery_id=self._delivery_id(turn_id, item, parsed),
                    )
            if item.terminal is not None:
                if item.terminal is TurnEventKind.FINISHED and state.text and not state.final_seen:
                    payload = {
                        **harness_terminal_payload(TurnEventKind.FINISHED),
                        "content": state.text.read(),
                    }
                    await self._publish(
                        turn_id,
                        payload,
                        delivery_id=self._delivery_id(turn_id, item, payload),
                    )
                elif item.terminal is TurnEventKind.FAILED and not state.error_seen:
                    payload = harness_terminal_payload(
                        TurnEventKind.FAILED,
                        error="Native execution Turn failed",
                    )
                    await self._publish(
                        turn_id,
                        payload,
                        delivery_id=self._delivery_id(turn_id, item, payload),
                    )
                elif item.terminal is TurnEventKind.ABORTED and not state.error_seen:
                    payload = harness_terminal_payload(
                        TurnEventKind.ABORTED,
                        cancellation="Native execution Turn was cancelled",
                    )
                    await self._publish(
                        turn_id,
                        payload,
                        delivery_id=self._delivery_id(turn_id, item, payload),
                    )
            delivered = True
        except OutputBudgetExceeded:
            state.delivery_failed = True
            raise
        except Exception as exc:
            logger.exception(
                "Detached Native output projection failed: session_id=%s turn_id=%s",
                self._session_id, turn_id,
            )
            await self._report_delivery_failure(state, turn_id, exc)
        finally:
            if item.terminal is not None and delivered:
                if self._runtime is not None and state.runtime_execution_id is not None:
                    self._runtime.finish_detached_native_turn(
                        self._session_id, state.runtime_execution_id, item.terminal
                    )
                self._turns.pop(turn_id, None)
                state.text.close()
                self._remember_terminal(turn_id)

    async def output_failed(self, error: OutputBudgetExceeded) -> None:
        for turn_id, state in tuple(self._turns.items()):
            state.delivery_failed = True
            await self._report_delivery_failure(state, turn_id, error)

    def _remember_terminal(self, turn_id: str) -> None:
        if turn_id in self._terminated_turns:
            return
        if len(self._terminal_order) >= _TERMINAL_TOMBSTONES:
            expired = self._terminal_order.popleft()
            self._terminated_turns.discard(expired)
        self._terminal_order.append(turn_id)
        self._terminated_turns.add(turn_id)

    async def _report_delivery_failure(
        self,
        state: _DetachedTurn,
        turn_id: str,
        error: BaseException,
    ) -> None:
        """Best-effort product diagnostic without claiming result delivery."""

        delivery = get_session_delivery_context(self._session_id) or {}
        metadata = get_session_metadata(
            self._session_id, enable_writeback=False
        ) or {}
        channel_id = str(
            delivery.get("channel_id") or metadata.get("channel_id") or "web"
        )
        request_id = state.request_id or f"native-turn-{turn_id}"
        try:
            code = (
                error.code if isinstance(error, OutputBudgetExceeded) else
                _HISTORY_PERSISTENCE_UNCONFIRMED
                if isinstance(error, _HistoryPersistenceUnconfirmed)
                else _DELIVERY_UNCONFIRMED
            )
            await send_runtime_push(
                build_server_push_message(
                    session_id=self._session_id,
                    request_id=request_id,
                    payload={
                        "event_type": "chat.error",
                        "code": code,
                        "error": "Detached result delivery could not be confirmed",
                        "turn_id": turn_id,
                        "error_type": type(error).__name__,
                    },
                    fallback_channel_id=channel_id,
                )
            )
        except Exception:
            logger.warning(
                "Detached Native delivery diagnostic failed: session_id=%s turn_id=%s",
                self._session_id,
                turn_id,
                exc_info=True,
            )

    async def _publish(
        self,
        turn_id: str,
        payload: dict[str, Any],
        *,
        delivery_id: str,
    ) -> None:
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
        request_id = state.request_id or f"native-turn-{turn_id}"
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
            event_type.startswith("chat.")
            or event_type == "context.usage"
            or event_type == "harness.activate_interaction"
        ):
            extra = {
                key: value for key, value in payload.items()
                if key not in {"event_type", "content"}
            }
            history_kwargs = dict(
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
                request_id=request_id,
                payload=payload,
                fallback_channel_id=channel_id,
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
                f"native:{turn_id}:chunk:{chunk_type}:{chunk_index}:{event_type}"
            )
        terminal = getattr(item.terminal, "value", item.terminal)
        return f"native:{turn_id}:terminal:{terminal}:{event_type}"
