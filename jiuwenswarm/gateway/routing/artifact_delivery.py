"""Gateway-owned durable delivery obligations for Browser Artifacts.

History remains the transcript authority. This inbox stores only the frozen
transport envelope and target receipts needed to recover the volatile queue.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import sqlite3
from pathlib import Path

from jiuwenswarm.common.e2a.constants import E2A_ARTIFACT_ROUTE_METADATA_KEYS

logger = logging.getLogger(__name__)


def artifact_delivery_id(payload) -> str | None:
    if not isinstance(payload, dict) or payload.get("event_type") != "chat.file":
        return None
    value = payload.get("delivery_id")
    return value if isinstance(value, str) and value.startswith("browser-artifact:") else None


class ArtifactInbox:
    def __init__(self, path: Path):
        self.path = path
        self._cursor = 0

    def _connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.is_symlink():
            raise ValueError("Artifact inbox must not be a symlink")
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        os.close(fd)
        connection = sqlite3.connect(self.path, timeout=10)
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("CREATE TABLE IF NOT EXISTS deliveries (id TEXT PRIMARY KEY, envelope TEXT NOT NULL)")
        connection.execute("CREATE TABLE IF NOT EXISTS targets (delivery TEXT NOT NULL, id TEXT NOT NULL, target TEXT NOT NULL, completed INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(delivery,id))")
        return connection

    @staticmethod
    def _key(message):
        return hashlib.sha256(json.dumps([message['session_id'], message['delivery_id']]).encode()).hexdigest()

    @staticmethod
    def _check_envelope(stored: str, message: dict) -> None:
        previous = json.loads(stored)
        # Stream caches and outbound routing metadata may be gone after restart.
        # The first accepted targets remain authoritative, but content cannot change.
        for field in ('session_id', 'delivery_id', 'channel_id', 'app_id', 'id', 'payload'):
            if previous.get(field) != message.get(field):
                raise ValueError('Artifact delivery identity conflicts with its durable envelope')

    def contains(self, message: dict) -> bool:
        if not self.path.exists():
            return False
        db = self._connect()
        try:
            existing = db.execute('SELECT envelope FROM deliveries WHERE id=?', (self._key(message),)).fetchone()
            if existing:
                self._check_envelope(existing[0], message)
            return existing is not None
        finally:
            db.close()

    def accept(self, message: dict, targets: list[dict]) -> str:
        if not targets or not message.get('session_id') or not message.get('delivery_id'):
            raise ValueError('Artifact delivery requires a session and explicit targets')
        key = self._key(message)
        encoded = json.dumps(message, ensure_ascii=False, sort_keys=True)
        db = self._connect()
        try:
            with db:
                db.execute('BEGIN IMMEDIATE')
                existing = db.execute('SELECT envelope FROM deliveries WHERE id=?', (key,)).fetchone()
                if existing:
                    self._check_envelope(existing[0], message)
                    return key
                db.execute('INSERT INTO deliveries VALUES (?,?)', (key, encoded))
                for target in {target['id']: target for target in targets}.values():
                    db.execute('INSERT INTO targets(delivery,id,target) VALUES (?,?,?)', (key, target['id'], json.dumps(target, ensure_ascii=False, sort_keys=True)))
            return key
        finally:
            db.close()

    def pending(self) -> list[tuple[str, dict, dict]]:
        if not self.path.exists():
            return []
        db = self._connect()
        try:
            query = ('SELECT t.rowid,d.id,d.envelope,t.target FROM deliveries d '
                     'JOIN targets t ON d.id=t.delivery WHERE t.completed=0 AND t.rowid>? '
                     'ORDER BY t.rowid LIMIT 128')
            rows = db.execute(query, (self._cursor,)).fetchall()
            if not rows:
                rows = db.execute(query, (0,)).fetchall()
            self._cursor = rows[-1][0] if rows else 0
            return [(key, json.loads(message), json.loads(target)) for _, key, message, target in rows]
        finally:
            db.close()

    def complete_target(self, key: str, target_id: str) -> None:
        db = self._connect()
        try:
            with db:
                db.execute('UPDATE targets SET completed=1 WHERE delivery=? AND id=?', (key, target_id))
        finally:
            db.close()


class ArtifactDeliveryQueue:
    def __init__(self, inbox: ArtifactInbox):
        self.inbox = inbox
        self._drain_lock = asyncio.Lock()

    async def contains(self, message: dict) -> bool:
        return await asyncio.to_thread(self.inbox.contains, message)

    async def accept(self, message: dict, targets: list[dict]) -> str:
        return await asyncio.to_thread(self.inbox.accept, message, targets)

    async def drain(self, send) -> None:
        async with self._drain_lock:
            pending = await asyncio.to_thread(self.inbox.pending)
            limit = asyncio.Semaphore(8)

            async def deliver(key, message, target):
                async with limit:
                    await deliver_one(key, message, target)

            async def deliver_one(key, message, target):
                try:
                    async with asyncio.timeout(15):
                        await send(message, target)
                    await asyncio.to_thread(self.inbox.complete_target, key, target['id'])
                except Exception:
                    # Retain only this target's obligation; other targets can progress.
                    logger.warning('Browser Artifact target remains pending: delivery=%s target=%s', key, target['id'])

            async with asyncio.TaskGroup() as tasks:
                for entry in pending:
                    tasks.create_task(deliver(*entry))


def freeze_message(msg) -> dict:
    import dataclasses
    data = dataclasses.asdict(msg)
    data['app_id'] = msg.app_id or 'default'
    data['mode'] = msg.mode.value
    data['event_type'] = msg.event_type.value if msg.event_type is not None else None
    data['req_method'] = msg.req_method.value if msg.req_method is not None else None
    data['timestamp'] = 0  # Reconstruction time is not part of delivery identity.
    data['delivery_id'] = artifact_delivery_id(msg.payload)
    data['metadata'] = {k: v for k, v in (data.get('metadata') or {}).items()
                        if k in E2A_ARTIFACT_ROUTE_METADATA_KEYS and k != 'fan_out_targets'}
    return json.loads(json.dumps(data, ensure_ascii=False))


def restore_message(data):
    from jiuwenswarm.common.schema.message import EventType, Message, Mode, ReqMethod
    from jiuwenswarm.gateway.routing.keys import AgentRef
    values = {k: v for k, v in data.items() if k != 'delivery_id'}
    values['event_type'] = EventType.CHAT_FILE
    values['mode'] = Mode(values['mode'])
    values['req_method'] = ReqMethod(values['req_method']) if values.get('req_method') else None
    if isinstance(values.get('agent_ref'), dict):
        values['agent_ref'] = AgentRef(**values['agent_ref'])
    return Message(**values)


def freeze_target(channel_id, app_id, routing) -> dict:
    import dataclasses
    if channel_id not in {'web', 'tui'}:
        raise ValueError('Durable Browser Artifact delivery currently requires Web/TUI receipt support')
    data = {'channel_id': channel_id, 'app_id': app_id, 'routing': None}
    if routing is not None:
        data['routing'] = dataclasses.asdict(routing)
        # Socket IDs expire. Preserve exact logical identities for reconnect;
        # never redirect a stale physical socket to a different user's session.
        if data['routing'].get('delivery'):
            data['routing']['delivery']['ws_id'] = ''
    encoded = json.dumps(data, sort_keys=True, ensure_ascii=False)
    return {'id': hashlib.sha256(encoded.encode()).hexdigest(), **data}


def restore_target(target):
    from jiuwenswarm.gateway.routing.keys import AgentRef, RoutingKey, make_delivery_target
    from jiuwenswarm.gateway.routing.session_sharing import RoutingTarget
    raw = target['routing']
    if raw is None:
        return None
    values = dict(raw)
    values['routing_keys'] = [RoutingKey(**{**key, 'agent_ref': AgentRef(**key['agent_ref'])}) for key in raw['routing_keys']]
    values['member_names'] = tuple(raw['member_names'])
    values['delivery'] = make_delivery_target(target['channel_id'])
    return RoutingTarget(**values)


def freeze_origin(key):
    """Capture the registered inbound identity, never a physical socket ID."""
    from jiuwenswarm.gateway.routing.session_sharing import RoutingTarget
    return freeze_target(key.channel_id, key.app_id, RoutingTarget("godview", routing_keys=[key]))


def origin_target(msg):
    """Validate the immutable request origin before accepting an offline file."""
    from jiuwenswarm.common.e2a.constants import E2A_ARTIFACT_ORIGIN_KEY
    raw = (msg.metadata or {}).get(E2A_ARTIFACT_ORIGIN_KEY)
    if raw is None:
        return None
    routing = restore_target(raw)
    if routing is None or len(routing.routing_keys) != 1:
        raise ValueError("Artifact origin requires exactly one logical identity")
    key = routing.routing_keys[0]
    if (key.session_id != msg.session_id or key.channel_id != msg.channel_id
            or raw["channel_id"] != key.channel_id or raw["app_id"] != key.app_id):
        raise ValueError("Artifact origin does not match its envelope")
    return freeze_origin(key)
