"""Lifecycle polling cannot become an unscoped project/session directory."""
from types import SimpleNamespace

import pytest

from jiuwenswarm.governance.application_boundary import admit_application_request
from jiuwenswarm.governance.lifecycle_inventory import project_lifecycle_projection
from jiuwenswarm.governance.session_sharing import SessionSharingDenied
from jiuwenswarm.server.runtime.session import lifecycle as lc, project_store
from tests.unit_tests.server.test_sharing_host import ALICE, BOB
from tests.unit_tests.server import test_sharing_host as host_tests

setup = host_tests.setup


def test_lifecycle_events_filter_session_owner_and_private_cleanup_results(setup, monkeypatch):
    host, _, pid, _, _, _, _ = setup
    entries = [
        {'event': 'session.lifecycle.updated', 'payload': {'session_id': 'session', 'project_id': pid},
         'result': {'deleted': True, 'private_path': '/secret'}},
        {'event': 'session.lifecycle.updated', 'payload': {'session_id': 'unknown', 'project_id': pid},
         'result': {'deleted': True}},
    ]
    monkeypatch.setattr(lc, 'event_snapshots', lambda: entries)
    for actor, count in [(ALICE, 1), (BOB, 0)]:
        permit = admit_application_request('project.lifecycle', {'events': True},
                                          identity_resolver=lambda: actor, host=host)
        payload = project_lifecycle_projection({'events': True}, permit)
        assert len(payload['events']) == count
        assert '/secret' not in str(payload)


def test_lifecycle_inventory_filters_before_reading_state_and_rejects_revoked_buffer(setup, monkeypatch):
    host, access, pid, _, _, _, _ = setup
    monkeypatch.setattr(project_store, 'list_projects', lambda **_: [
        SimpleNamespace(project_id=pid), SimpleNamespace(project_id='private-other')])
    def projection(kind, resource_id):
        assert resource_id == pid
        return {'lifecycle_operation': None}
    monkeypatch.setattr(lc, 'projection', projection)
    permit = admit_application_request('project.lifecycle', {'inventory': True},
                                      identity_resolver=lambda: ALICE, host=host)
    assert project_lifecycle_projection({'inventory': True}, permit) == {
        'projects': [{'project_id': pid, 'operation': None}]}
    access.replace_acl(pid, 'admin', acl={}, expected_revision=2)
    assert not permit.revalidate()


@pytest.mark.parametrize('params', [{'inventory': True, 'failed': True},
    {'events': True, 'project_id': 'p'}, {'events': 'true'}, {},
    {'project_id': 'p', 'completed_cron_job_ids': []}])
def test_lifecycle_read_does_not_admit_checkpoint_writes(setup, params):
    with pytest.raises(SessionSharingDenied):
        admit_application_request('project.lifecycle', params,
                                  identity_resolver=lambda: ALICE, host=setup[0])


def test_lifecycle_notification_has_bounded_payload_and_current_recipient(setup):
    from jiuwenswarm.governance.lifecycle_inventory import lifecycle_event_delivery
    host, access, pid, _, _, _, _ = setup
    frame = {'event': 'project.lifecycle.updated', 'payload': {
        'project_id': pid, 'resource_id': pid, 'revision': 1, 'private_path': '/secret',
        'lifecycle_operation': {'status': 'running', 'private_path': '/secret'}}}
    with pytest.raises(SessionSharingDenied):
        lifecycle_event_delivery(frame, lambda: BOB, host)
    public, guard = lifecycle_event_delivery(frame, lambda: ALICE, host)
    assert '/secret' not in str(public)
    assert guard()
    access.replace_acl(pid, 'admin', acl={}, expected_revision=2)
    assert not guard()


@pytest.mark.asyncio
async def test_gateway_drops_lifecycle_notification_revoked_while_queued(setup, monkeypatch):
    import asyncio
    from unittest.mock import AsyncMock
    from jiuwenswarm.governance import organization_auth, session_boundary
    from jiuwenswarm.gateway.channel_manager.base import RobotMessageRouter
    from jiuwenswarm.gateway.channel_manager.web.web_connect import WebChannel, WebChannelConfig
    host, access, pid, _, _, _, _ = setup
    monkeypatch.setattr(organization_auth, 'configured_authenticator', lambda: object())
    monkeypatch.setattr(organization_auth, 'connection_principal', lambda ws: SimpleNamespace(identity=lambda: ALICE))
    monkeypatch.setattr(session_boundary, 'organization_sharing_host', lambda: host)
    ws = SimpleNamespace(_jiuwen_ws_id='lifecycle-browser', closed=False, send=AsyncMock())
    channel = WebChannel(WebChannelConfig(enabled=True), RobotMessageRouter())
    queue = asyncio.Queue()
    channel._send_queues['lifecycle-browser'] = queue
    channel._enqueue_send(ws, {'type': 'event', 'event': 'project.lifecycle.updated',
                              'payload': {'project_id': pid, 'revision': 1}})
    assert queue.qsize() == 1
    access.replace_acl(pid, 'admin', acl={}, expected_revision=2)
    queue.put_nowait(None)
    await channel._writer_loop(ws, 'lifecycle-browser')
    ws.send.assert_not_awaited()
