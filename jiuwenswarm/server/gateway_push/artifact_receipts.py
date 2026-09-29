"""Connection-scoped acknowledgement of Gateway durable Artifact acceptance."""
from __future__ import annotations

import asyncio
import secrets


class ArtifactAcceptanceReceipts:
    def __init__(self) -> None:
        self._pending: dict[str, tuple[object, asyncio.Future[bool]]] = {}

    def begin(self, ws: object) -> tuple[str, asyncio.Future[bool]]:
        nonce = secrets.token_hex(24)
        future = asyncio.get_running_loop().create_future()
        self._pending[nonce] = (ws, future)
        return nonce, future

    def settle(self, ws: object, nonce: object) -> bool:
        current = self._pending.get(nonce) if isinstance(nonce, str) else None
        if current is None or current[0] is not ws or current[1].done():
            return False
        current[1].set_result(True)
        return True

    def discard(self, nonce: str) -> None:
        current = self._pending.pop(nonce, None)
        if current and not current[1].done():
            current[1].cancel()
