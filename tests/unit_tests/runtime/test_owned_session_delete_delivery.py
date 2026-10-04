"""Real Single deletion/sidecar -> AgentServer codec -> Gateway queued writer.

The in-process transport carries production E2A envelopes and wire responses.
Provider exit/allocation is synthetic in the shared deletion fixture; no host
receipt confirmation, lifecycle transaction or authorization is stubbed here.
"""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.common.e2a.agent_compat import e2a_to_agent_request
from jiuwenswarm.common.e2a.wire_codec import parse_agent_server_wire_unary
from jiuwenswarm.governance import organization_auth, session_boundary
from jiuwenswarm.gateway.channel_manager.base import RobotMessageRouter
from jiuwenswarm.gateway.channel_manager.web.app_web_handlers import (
    WebHandlersBindParams, _register_web_handlers,
)
from jiuwenswarm.gateway.channel_manager.web.web_connect import WebChannel, WebChannelConfig
from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer
from jiuwenswarm.server.runtime.session import lifecycle as lc
from tests.unit_tests.runtime.test_owned_session_delete import deletion, setup, transaction
from tests.unit_tests.runtime import test_continuation_transaction as txns


@pytest.fixture
async def delivery(deletion, monkeypatch):
    d = deletion
    host = d.tx.setup.host
    identity = d.tx.setup.identities
    principal = SimpleNamespace(identity=lambda: identity[0])
    monkeypatch.setattr(organization_auth, 'configured_authenticator', lambda: object())
    monkeypatch.setattr(organization_auth, 'connection_principal', lambda ws: principal)
    monkeypatch.setattr(session_boundary, 'organization_sharing_host', lambda: host)
    server = object.__new__(AgentWebSocketServer)
    server._archive_service = d.service
    server._runtime = d.tx.runtime
    server._agent_manager = d.tx.manager
    server._organization_session_host = host
    lock = asyncio.Lock()
    agent_wires, requests, permits = [], [], []

    async def send_request(envelope):
        # This is the same conversion/admission performed immediately before
        # the real AgentServer lifecycle handler. No wire private attrs survive.
        assert envelope.request_id.startswith('delete-wire-')
        assert envelope.session_id == d.sid and envelope.method == 'session.delete'
        assert organization_auth.current_identity() == identity[0]
        request = e2a_to_agent_request(envelope)
        permit = session_boundary.admit_session_request(
            'session.delete', request.params, identity_resolver=lambda: identity[0],
            host=host, envelope_session=request.session_id,
        )
        d.tx.runtime.prepare_session_deletion(request, permit)
        requests.append(request)
        permits.append(permit)
        wires = []
        async def send(raw):
            wires.append(json.loads(raw))
        agent_ws = SimpleNamespace(send=send)
        with session_boundary.delivery_scope():
            session_boundary.set_delivery_permit(permit)
            assert await server._handle_lifecycle_request(agent_ws, request, lock) is True
        assert len(wires) == 1
        agent_wires.append(wires[0])
        return parse_agent_server_wire_unary(wires[0])

    channel = WebChannel(WebChannelConfig(enabled=True), RobotMessageRouter())
    channel._process_files = AsyncMock(side_effect=AssertionError('delete cannot import files'))
    channel.register_ws = AsyncMock(side_effect=AssertionError('delete cannot subscribe history'))
    channel._on_message_cb = AsyncMock(side_effect=AssertionError('delete cannot enter message queue'))
    browser = SimpleNamespace(_jiuwen_ws_id='delete-browser', closed=False, send=AsyncMock())
    channel._send_queues['delete-browser'] = asyncio.Queue()
    client = SimpleNamespace(server_ready=True, send_request=send_request)
    _register_web_handlers(WebHandlersBindParams(channel=channel, agent_client=client))

    async def dispatch(number=1):
        await channel._handle_raw_message(browser, json.dumps({
            'type': 'req', 'id': f'delete-wire-{number}', 'method': 'session.delete',
            'params': {'session_id': d.sid},
        }), {})

    async def drain():
        browser.send.reset_mock()
        channel._send_queues['delete-browser'].put_nowait(None)
        await channel._writer_loop(browser, 'delete-browser')
        return [json.loads(call.args[0]) for call in browser.send.await_args_list]

    yield SimpleNamespace(**locals())


def fixed_payload(d):
    return {'session_id': d.sid, 'deleted': True, 'exit_confirmed': True, 'audit_pending': False}


def assert_success_frames(frames, d, number=1):
    assert frames == [
        {'type': 'res', 'id': f'delete-wire-{number}', 'ok': True, 'payload': fixed_payload(d)},
        {'type': 'event', 'event': 'session.deleted', 'payload': fixed_payload(d)},
    ]


async def wait_for_commit(d):
    async with asyncio.timeout(5):
        while (lc.state('session', d.sid).get('operation') or {}).get('status') != 'completed':
            await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_real_retired_owner_receipt_traverses_agentserver_and_gateway_and_retries(delivery):
    x, d = delivery, delivery.d
    # Source revocation closes view/execute, but cannot remove owned cleanup.
    x.host.store.revoke(d.tx.request.share_id, txns.ALICE, expected_revision=1)
    assert not x.host.owner_current(d.sid, txns.BOB)
    await x.dispatch()
    assert txns.owner(d.tx)['retired'] is True
    assert not (d.tx.root / d.sid).exists()
    assert not x.permits[0].revalidate()
    gateway_permit = x.browser._jiuwen_session_permits['delete-wire-1']
    assert not gateway_permit.revalidate()
    assert x.host.confirm_deletion_for_permit(gateway_permit) is True
    wire_result = parse_agent_server_wire_unary(x.agent_wires[0])
    assert wire_result.ok and wire_result.request_id == 'delete-wire-1'
    assert wire_result.payload == fixed_payload(d)
    assert_success_frames(await x.drain(), d)
    assert not x.channel._clients_by_key and not x.channel._ws_sessions
    x.channel._process_files.assert_not_awaited()
    x.channel.register_ws.assert_not_awaited()
    x.channel._on_message_cb.assert_not_awaited()

    # Same owner can retry after all metadata is gone, without reallocating or
    # stopping another Provider. A new original wire ID receives only the ACK.
    await x.dispatch(2)
    assert x.permits[1].deletion_receipt is not None
    assert_success_frames(await x.drain(), d, 2)
    d.release.assert_awaited_once_with(d.sid)
    d.tx.manager.stop_existing_session_runtime.assert_awaited_once()
    assert d.tx.allocated == [d.sid]


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['identity', 'recreated_directory'])
async def test_agentserver_rechecks_after_real_send_lock_wait(delivery, change):
    x, d = delivery, delivery.d
    await x.lock.acquire()
    task = asyncio.create_task(x.dispatch())
    try:
        await wait_for_commit(d)
        assert not task.done() and not x.agent_wires
        if change == 'identity':
            x.identity[0] = txns.ALICE
        else:
            path = d.tx.root / d.sid
            path.mkdir()
            (path / 'new-owner-marker').write_text('new target must remain')
        x.lock.release()
        await asyncio.wait_for(task, 5)
        response = parse_agent_server_wire_unary(x.agent_wires[0])
        assert response.ok is False
        assert response.payload == {'code': 'DELETE_UNCONFIRMED', 'error': 'Deletion result is unavailable.'}
        frames = await x.drain()
        if change == 'identity':
            assert frames == []
            x.identity[0] = txns.BOB
            await x.dispatch(2)
            assert_success_frames(await x.drain(), d, 2)
        else:
            assert len(frames) == 1 and frames[0]['ok'] is False
            assert frames[0]['payload']['deleted'] is False
            assert (d.tx.root / d.sid / 'new-owner-marker').read_text() == 'new target must remain'
        d.release.assert_awaited_once()
    finally:
        if x.lock.locked():
            x.lock.release()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['identity', 'recreated_directory'])
async def test_gateway_writer_rechecks_real_receipt_after_enqueue(delivery, change):
    x, d = delivery, delivery.d
    await x.dispatch()
    assert parse_agent_server_wire_unary(x.agent_wires[0]).ok is True
    if change == 'identity':
        x.identity[0] = txns.ALICE
    else:
        (d.tx.root / d.sid).mkdir()
    assert await x.drain() == []
    d.release.assert_awaited_once()


@pytest.mark.asyncio
async def test_delete_receipt_cannot_send_generic_metadata_history_or_wrong_id(delivery):
    x, d = delivery, delivery.d
    await x.dispatch()
    permit = x.browser._jiuwen_session_permits['delete-wire-1']
    assert x.host.confirm_deletion_for_permit(permit) is True
    await x.channel.send_response(x.browser, 'delete-wire-1', ok=True,
                                  payload={'session_id': d.sid, 'metadata': 'private'})
    await x.channel.send_event(x.browser, 'session.deleted',
                               {'session_id': d.sid, 'history': ['private']}, session_id=d.sid)
    x.channel.send_deletion_result(x.browser, 'another-id', permit, success=True)
    assert_success_frames(await x.drain(), d)


@pytest.mark.asyncio
async def test_unconfirmed_provider_exit_cannot_cross_as_success(delivery):
    x, d = delivery, delivery.d
    d.tx.manager.stop_existing_session_runtime.side_effect = RuntimeError('synthetic exit unconfirmed')
    await x.dispatch()
    frames = await x.drain()
    assert len(frames) == 1 and frames[0]['ok'] is False
    assert frames[0]['payload'] == {'session_id': d.sid, 'deleted': False, 'exit_confirmed': False}
    assert (d.tx.root / d.sid).exists()
    assert txns.owner(d.tx).get('retired') is not True
    d.release.assert_not_awaited()
    d.tx.manager.stop_existing_session_runtime.side_effect = None
    await x.dispatch(2)
    assert_success_frames(await x.drain(), d, 2)
    d.release.assert_awaited_once_with(d.sid)


@pytest.mark.asyncio
async def test_real_server_composition_keeps_original_delete_host_on_rebuild(deletion, monkeypatch):
    """The real constructor must not create a second, receipt-incompatible host."""
    from copy import copy
    from jiuwenswarm.runtime.plan import PlanModeController

    d = deletion
    hosts = []
    def factory():
        host = copy(d.tx.setup.host)
        hosts.append(host)
        return host
    monkeypatch.setattr(session_boundary, 'organization_sharing_host', factory)
    monkeypatch.setattr(organization_auth, 'configured_authenticator', lambda: None)
    server = AgentWebSocketServer(trusted_identity_resolver=lambda _: d.tx.setup.identities[0])
    original = server.get_runtime()
    rebuilt = None
    try:
        assert len(hosts) == 1
        host = server._organization_session_host
        assert original._organization_session_host is host
        assert original._owner_publication.host is host
        # Exercise the receipt capture that rejected the real browser request.
        request = d.request()
        permit = session_boundary.admit_session_request(
            'session.delete', request.params,
            identity_resolver=lambda: d.tx.setup.identities[0],
            host=host, envelope_session=d.sid,
        )
        authority = original.prepare_session_deletion(request, permit)
        assert authority.host is permit.host is host
        rebuilt = server._build_runtime(plan_controller=PlanModeController())
        assert len(hosts) == 1
        assert rebuilt._organization_session_host is host
        assert rebuilt.prepare_session_deletion(d.request(), permit).host is host
    finally:
        if rebuilt is not None:
            await rebuilt.close()
        await original.close()
