"""Durable Browser Artifact acceptance and per-target retry boundaries."""
from __future__ import annotations

import pytest

from jiuwenswarm.gateway.routing.artifact_delivery import ArtifactInbox, ArtifactDeliveryQueue


def message():
    return {"session_id": "session", "delivery_id": "browser-artifact:one", "payload": {"files": [{"name": "a.txt"}]}}


@pytest.mark.asyncio
async def test_acceptance_failure_does_not_acknowledge(tmp_path, monkeypatch):
    inbox = ArtifactInbox(tmp_path / "inbox.sqlite3")
    queue = ArtifactDeliveryQueue(inbox)
    def fail(*args):
        raise OSError("disk unavailable")
    monkeypatch.setattr(inbox, "accept", fail)
    with pytest.raises(OSError):
        await queue.accept(message(), [{"id": "web"}])


@pytest.mark.asyncio
async def test_restart_recovers_accepted_but_unsent_artifact(tmp_path):
    path = tmp_path / "inbox.sqlite3"
    await ArtifactDeliveryQueue(ArtifactInbox(path)).accept(message(), [{"id": "web"}])
    received = []
    async def send(msg, target):
        received.append((msg, target))
    replacement = ArtifactDeliveryQueue(ArtifactInbox(path))
    await replacement.drain(send)
    await replacement.drain(send)
    assert len(received) == 1


@pytest.mark.asyncio
async def test_partial_fanout_retries_only_failed_target_after_restart(tmp_path):
    path = tmp_path / "inbox.sqlite3"
    queue = ArtifactDeliveryQueue(ArtifactInbox(path))
    await queue.accept(message(), [{"id": "web"}, {"id": "tui"}])
    sent = []
    async def partial(msg, target):
        if target["id"] == "tui":
            raise ConnectionError("disconnected")
        sent.append(target["id"])
    await queue.drain(partial)
    replacement = ArtifactDeliveryQueue(ArtifactInbox(path))
    # Replay must retain the original fanout; current routing cannot add users.
    await replacement.accept(message(), [{"id": "new-user"}])
    async def recovered(msg, target):
        sent.append(target["id"])
    await replacement.drain(recovered)
    assert sent == ["web", "tui"]


@pytest.mark.asyncio
async def test_success_before_receipt_crash_replays_same_identity(tmp_path, monkeypatch):
    inbox = ArtifactInbox(tmp_path / "inbox.sqlite3")
    queue = ArtifactDeliveryQueue(inbox)
    await queue.accept(message(), [{"id": "web"}])
    sent = []
    async def send(msg, target):
        sent.append(msg["delivery_id"])
    original = inbox.complete_target
    def crash(*args):
        raise OSError("receipt write failed")
    monkeypatch.setattr(inbox, "complete_target", crash)
    await queue.drain(send)
    monkeypatch.setattr(inbox, "complete_target", original)
    await queue.drain(send)
    assert sent == ["browser-artifact:one", "browser-artifact:one"]


@pytest.mark.asyncio
async def test_conflicting_payload_or_empty_fanout_is_not_accepted(tmp_path):
    queue = ArtifactDeliveryQueue(ArtifactInbox(tmp_path / "inbox.sqlite3"))
    with pytest.raises(ValueError):
        await queue.accept(message(), [])
    await queue.accept(message(), [{"id": "web"}])
    altered = {**message(), "payload": {"files": [{"name": "different.txt"}]}}
    with pytest.raises(ValueError):
        await queue.accept(altered, [{"id": "web"}])


@pytest.mark.asyncio
async def test_failed_page_does_not_starve_later_deliveries(tmp_path):
    queue = ArtifactDeliveryQueue(ArtifactInbox(tmp_path / 'inbox.sqlite3'))
    await queue.accept(message(), [{'id': str(i)} for i in range(129)])
    received = []
    async def send(msg, target):
        if target['id'] != '128':
            raise ConnectionError('offline')
        received.append(target['id'])
    await queue.drain(send)
    await queue.drain(send)
    assert received == ['128']


def test_commit_survives_abrupt_process_exit(tmp_path):
    import os
    import subprocess
    import sys
    path = tmp_path / 'crashed-inbox.sqlite3'
    source = '''
import os,sys
from pathlib import Path
from jiuwenswarm.gateway.routing.artifact_delivery import ArtifactInbox
ArtifactInbox(Path(sys.argv[1])).accept(
    {"session_id":"session", "delivery_id":"browser-artifact:crash", "payload":{}}, [{"id":"web"}])
os._exit(17)
'''
    env = dict(os.environ)
    env.pop('PYTHONPATH', None)
    result = subprocess.run([sys.executable, '-c', source, str(path)], env=env,
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 17, result.stderr
    pending = ArtifactInbox(path).pending()
    assert len(pending) == 1
    assert pending[0][1]['delivery_id'] == 'browser-artifact:crash'
