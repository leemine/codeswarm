"""Actual AgentServer wire sink after queued original Goal result checks."""
import asyncio
import json
from types import SimpleNamespace
import pytest
from jiuwenswarm.common.schema.agent import AgentResponse
from jiuwenswarm.governance.organization_auth import authenticated_scope
from jiuwenswarm.governance.session_boundary import admit_session_request, delivery_scope, set_delivery_permit
from jiuwenswarm.server.ws_send import send_wire_payload
from jiuwenswarm.common.e2a.wire_codec import encode_agent_response_for_wire
from tests.unit_tests.runtime import test_native_goal_read_runtime as component

case = component.case
source = component.source
credentials = component.credentials


@pytest.mark.parametrize('change', ['none', 'child', 'acl'])
async def test_goal_delivery_actual_wire_sink_rechecks_after_send_lock(case, change):
    f = case
    sent = []
    async def send(frame):
        sent.append(json.loads(frame))
    socket = SimpleNamespace(send=send)
    lock = asyncio.Lock()
    with authenticated_scope(f.bob), delivery_scope():
        permit = admit_session_request('command.goal', f.request.params,
            identity_resolver=f.bob.identity, host=f.host)
        set_delivery_permit(permit)
        event, = await f.runtime.invoke(f.request)
        response = AgentResponse(request_id=f.request.request_id, channel_id='web',
                                  ok=True, payload=event.payload)
        wire = encode_agent_response_for_wire(response, response_id=f.request.request_id)
        await lock.acquire()
        async def delayed_send():
            async with lock:
                return await send_wire_payload(socket, wire)
        task = asyncio.create_task(delayed_send())
        await asyncio.sleep(0)
        if change == 'child':
            f.root._session_adapters[f.sid] = object()
        elif change == 'acl':
            f.access.replace_acl(f.project.project_id, 'alice', acl={}, expected_revision=2)
        lock.release()
        await task
    assert len(sent) == 1
    if change == 'none':
        assert 'private objective' in json.dumps(sent)
    else:
        assert 'private objective' not in json.dumps(sent)
        assert 'FORBIDDEN' in json.dumps(sent)


@pytest.mark.parametrize('change', ['none', 'metadata', 'acl', 'credential'])
async def test_gateway_original_writer_rechecks_owner_and_route(case, monkeypatch, change):
    from jiuwenswarm.gateway.channel_manager.base import RobotMessageRouter
    from jiuwenswarm.gateway.channel_manager.web.web_connect import WebChannel, WebChannelConfig
    from jiuwenswarm.governance import organization_auth
    from jiuwenswarm.server.runtime.session import session_metadata
    f, sent = case, []
    async def send(frame):
        sent.append(json.loads(frame))
    socket = SimpleNamespace(_jiuwen_ws_id='goal-reader', closed=False, send=send)
    monkeypatch.setattr(organization_auth, 'connection_principal', lambda ws: f.bob)
    with authenticated_scope(f.bob):
        permit = admit_session_request('command.goal', f.request.params,
            identity_resolver=f.bob.identity, host=f.host)
        socket._jiuwen_session_permits = {f.request.request_id: permit}
        event, = await f.runtime.invoke(f.request)
    channel = WebChannel(WebChannelConfig(enabled=True), RobotMessageRouter())
    queue = asyncio.Queue()
    channel._send_queues['goal-reader'] = queue
    channel._enqueue_send(socket, {'type': 'res', 'id': f.request.request_id,
                                  'ok': True, 'payload': event.payload})
    assert queue.qsize() == 1
    if change == 'metadata':
        metadata = session_metadata.get_session_metadata(f.sid, cache_bust=True, enable_writeback=False)
        metadata['execution_config_revision'] = 'replacement'
        session_metadata._write_metadata_sync(f.sid, metadata)
    elif change == 'acl':
        f.access.replace_acl(f.project.project_id, 'alice', acl={}, expected_revision=2)
    elif change == 'credential':
        f.auth.revoke(f.bob)
    await queue.put(None)
    await asyncio.wait_for(channel._writer_loop(socket, 'goal-reader'), 2)
    if change == 'none':
        assert len(sent) == 1 and 'private objective' in json.dumps(sent)
    else:
        assert not sent
