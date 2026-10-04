"""Owner-only audit reads with real credentials/sidecar and bounded delivery."""
import asyncio
import hashlib
import json
from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.governance import organization_auth, session_boundary
from jiuwenswarm.governance.organization_auth import authenticated_scope, current_identity
from jiuwenswarm.governance.project_boundary import authorize_resource_request, request_project_action
from jiuwenswarm.server.runtime.gateway_adapter import AdapterRegistry
from jiuwenswarm.server.runtime.gateway_adapter.session_sharing_adapter import SessionSharingAdapter
from jiuwenswarm.server.runtime.session import session_history
from tests.unit_tests.governance import test_workspace_download as existing

credentials, setup = existing.credentials, existing.setup
METHOD = 'session.share.audit.list'


@pytest.fixture
def audit(setup, monkeypatch):
    s = setup
    monkeypatch.setattr(session_history, 'get_agent_sessions_dir', lambda: s.tmp_path / 'sessions')
    path = s.tmp_path / 'sessions/alice-session/history.jsonl'
    path.write_text('{"role":"user","content":"private audit fixture body"}\n')
    history = s.host.prepare_source('alice-session', s.alice.identity())
    share = s.host.store.grant('alice-session', s.alice.identity(), s.bob.identity(),
        actions=('view',), history=history, expires_at=None)
    adapter = SessionSharingAdapter(s.host.store, identity_resolver=lambda _: current_identity(),
        target_resolver=s.host.target_resolver, compile_history=s.host.compile_history,
        audit_owner_revision=s.host.owner_revision)
    return SimpleNamespace(**locals())


def request(**params):
    return AgentRequest(request_id='audit-request', channel_id='web', session_id='alice-session',
                        req_method=ReqMethod.SESSION_SHARE_AUDIT_LIST,
                        params={'session_id': 'alice-session', **params})


@contextmanager
def admitted(audit, req, person=None):
    s = audit.s
    with authenticated_scope(person or s.alice), session_boundary.delivery_scope():
        permit = session_boundary.admit_session_request(METHOD, req.params,
            identity_resolver=current_identity, host=s.host, envelope_session=req.session_id)
        session_boundary.set_delivery_permit(permit)
        authorize_resource_request(req, current_identity(), access_store=s.access, session_permit=permit)
        yield permit


@pytest.mark.asyncio
async def test_real_owner_bounded_projection_revoke_and_no_history_dependency(audit):
    s = audit.s
    updated = s.host.store.revise(audit.share['share_id'], s.alice.identity(), actions=('view',),
        history=audit.history, expires_at=None, expected_revision=1)
    s.host.store.revoke(updated['share_id'], s.alice.identity(), expected_revision=2)
    audit.path.unlink()  # Audit current-owner authority never requires history to exist.
    before = s.access.path.read_bytes()
    with admitted(audit, request(limit=2)):
        response = await audit.adapter.handle(request(limit=2))
        assert response.ok
        response._delivery_guard()
    payload = response.payload
    assert payload['session_id'] == 'alice-session' and payload['has_more'] is True
    assert payload['coverage'] == 'confirmed_mutations_and_publications_only'
    assert [e['action'] for e in payload['events']] == ['revoke', 'update']
    assert [e['sequence'] for e in payload['events']] == [3, 2]
    assert all(set(e) == {'sequence', 'event_id', 'recorded_at', 'action', 'phase', 'result',
        'share_id', 'share_revision', 'before_revision', 'after_revision', 'actor_id', 'target_actor_id',
        'request_id', 'method'} for e in payload['events'])
    assert all(e['actor_id'] == 'alice' and e['target_actor_id'] == 'bob' for e in payload['events'])
    assert 'private audit fixture body' not in json.dumps(payload)
    assert s.access.path.read_bytes() == before
    with admitted(audit, request(limit=3)):
        assert (await audit.adapter.handle(request(limit=3))).payload['has_more'] is False
    assert request_project_action(METHOD) == 'read'


@pytest.mark.parametrize('params', [{'limit': True}, {'limit': 0}, {'limit': 101}, {'limit': '2'},
    {'cursor': 'x'}, {'subject_id': 'bob'}, {'share_id': 'x'}, {'files': []}, {'history': {}}, {'session_id': ''}])
def test_invalid_wire_rejected_at_admission_before_gateway_file_processing(audit, params):
    with pytest.raises((ValueError, PermissionError)), admitted(audit, request(**params)):
        pytest.fail('invalid selectors admitted')


@pytest.mark.parametrize('identity_change', ['recipient', 'subject', 'authority', 'retired', 'source', 'acl'])
def test_shared_view_and_obsolete_owner_never_authorize_audit(audit, identity_change):
    s = audit.s
    person = s.alice
    if identity_change == 'recipient':
        person = s.bob
    elif identity_change in {'subject', 'authority'}:
        field = 'subject_id' if identity_change == 'subject' else 'authority'
        person = replace(person, bound_identity=replace(person.bound_identity, **{field: 'other'}))
    elif identity_change == 'retired':
        s.host.store.retire_owner('alice-session', expected_revision=1)
    elif identity_change == 'source':
        s.host.invalidate_source('alice-session', expected_epoch=s.host.source_epoch('alice-session'))
    else:
        s.access.replace_acl(s.project.project_id, 'admin', acl={'bob': ['read']}, expected_revision=2)
    with pytest.raises(PermissionError), admitted(audit, request(), person):
        pytest.fail('non-current owner admitted')


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['credential', 'source', 'permit', 'request_id', 'method', 'session', 'params', 'principal'])
async def test_original_response_guard_rejects_changes_after_read(audit, change):
    req = request()
    with admitted(audit, req) as permit:
        response = await audit.adapter.handle(req)
        assert response.ok
        if change == 'credential':
            audit.s.auth.revoke(audit.s.alice)
        elif change == 'source':
            audit.s.host.invalidate_source('alice-session', expected_epoch=audit.s.host.source_epoch('alice-session'))
        elif change == 'permit':
            session_boundary.set_delivery_permit(replace(permit))
        elif change == 'request_id':
            req.request_id = 'another'
        elif change == 'method':
            req.req_method = ReqMethod.SESSION_SHARE_LIST
        elif change == 'session':
            req.session_id = 'bob-session'
        elif change == 'params':
            req.params['limit'] = 1
        else:
            with authenticated_scope(replace(audit.s.alice)), pytest.raises(PermissionError):
                response._delivery_guard()
            return
        with pytest.raises(PermissionError):
            response._delivery_guard()


@pytest.mark.asyncio
async def test_no_principal_no_permit_and_legacy_adapter_cannot_query(audit):
    assert not (await audit.adapter.handle(request())).ok
    with authenticated_scope(audit.s.alice):
        assert not (await audit.adapter.handle(request())).ok
    audit.adapter.audit_owner_revision = None
    with admitted(audit, request()):
        assert not (await audit.adapter.handle(request())).ok


@pytest.mark.asyncio
@pytest.mark.parametrize('corrupt', [None, {'schema_version': 2}, {'schema_version': 1, 'events': [], 'next_sequence': 2}])
async def test_corrupt_audit_fails_without_reset_or_partial_event(audit, corrupt):
    data = audit.s.access._load()
    data['sharing_audit'] = corrupt
    audit.s.access._save(data)
    before = audit.s.access.path.read_bytes()
    with admitted(audit, request()):
        response = await audit.adapter.handle(request())
        assert not response.ok and 'events' not in response.payload
    assert audit.s.access.path.read_bytes() == before


@pytest.mark.asyncio
async def test_legacy_missing_audit_is_empty_and_new_owner_epoch_does_not_read_old_history(audit):
    s = audit.s
    data = s.access._load()
    data['session_sharing']['owners']['alice-session']['revision'] += 1
    s.access._save(data)
    with admitted(audit, request()):
        result = (await audit.adapter.handle(request())).payload
        assert result['events'] == [] and result['has_more'] is False
    data.pop('sharing_audit')
    s.access._save(data)
    before = s.access.path.read_bytes()
    with admitted(audit, request()):
        assert (await audit.adapter.handle(request())).payload['events'] == []
    assert s.access.path.read_bytes() == before


@pytest.mark.asyncio
async def test_agentserver_actual_send_lock_guard_withdraws_completed_query(audit):
    from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer
    server = AgentWebSocketServer.__new__(AgentWebSocketServer)
    server._organization_session_host = audit.s.host
    server._adapter_registry = AdapterRegistry()
    server._adapter_registry.register(audit.adapter)
    ws = SimpleNamespace(send=AsyncMock())

    class SendLock:
        async def __aenter__(self):
            audit.s.auth.revoke(audit.s.alice)
        async def __aexit__(self, *_):
            return False

    with admitted(audit, request()):
        assert await server._dispatch_gateway_adapter_request(ws, request(), SendLock())
    # send_wire_payload's original delivery authority can suppress even the denial.
    assert all('events' not in call.args[0] for call in ws.send.await_args_list)


@pytest.mark.asyncio
@pytest.mark.parametrize('change', [None, 'credential', 'permit', 'principal', 'source', 'response_id', 'response_session'])
async def test_actual_gateway_handler_proxy_and_writer_keep_exact_owner_permit(audit, monkeypatch, change):
    from jiuwenswarm.gateway.channel_manager.base import RobotMessageRouter
    from jiuwenswarm.gateway.channel_manager.web.web_connect import WebChannel, WebChannelConfig
    from jiuwenswarm.gateway.channel_manager.web.app_web_handlers import WebHandlersBindParams, _register_web_handlers

    current = [audit.s.alice]
    monkeypatch.setattr(organization_auth, 'connection_principal', lambda _: current[0])
    monkeypatch.setattr(session_boundary, 'organization_sharing_host', lambda: audit.s.host)
    channel = WebChannel(WebChannelConfig(enabled=True), RobotMessageRouter())
    channel.register_ws = AsyncMock(side_effect=AssertionError('audit subscribed Session'))
    channel._process_files = AsyncMock(side_effect=AssertionError('audit processed files'))
    ws = SimpleNamespace(_jiuwen_ws_id='audit-browser', closed=False, send=AsyncMock())
    channel._send_queues['audit-browser'] = asyncio.Queue()
    calls = []

    class Client:
        async def send_request(self, envelope):
            calls.append(envelope)
            req = AgentRequest(request_id=envelope.request_id, channel_id='web', session_id=envelope.session_id,
                req_method=ReqMethod(envelope.method), params=envelope.params)
            with admitted(audit, req):
                return await audit.adapter.handle(req)

    _register_web_handlers(WebHandlersBindParams(channel=channel, agent_client=Client()))
    await channel._handle_raw_message(ws, json.dumps({'type': 'req', 'id': 'audit-request',
        'method': METHOD, 'params': {'session_id': 'alice-session', 'limit': 1}}), {})
    assert len(calls) == 1 and calls[0].request_id == 'audit-request'
    channel.register_ws.assert_not_awaited()
    channel._process_files.assert_not_awaited()
    assert not channel._clients_by_key and not channel._ws_sessions
    if change == 'credential':
        audit.s.auth.revoke(audit.s.alice)
    elif change == 'permit':
        ws._jiuwen_session_permits['audit-request'] = replace(ws._jiuwen_session_permits['audit-request'])
    elif change == 'principal':
        current[0] = replace(audit.s.alice)
    elif change == 'source':
        audit.s.host.invalidate_source('alice-session', expected_epoch=audit.s.host.source_epoch('alice-session'))
    if change in {'response_id', 'response_session'}:
        frame = channel._send_queues['audit-browser']._queue[0]
        if change == 'response_id':
            frame.data['id'] = 'other-request'
        else:
            frame.data['payload']['session_id'] = 'bob-session'
    channel._send_queues['audit-browser'].put_nowait(None)
    await channel._writer_loop(ws, 'audit-browser')
    frames = [json.loads(call.args[0]) for call in ws.send.await_args_list]
    if change is None:
        assert len(frames) == 1 and frames[0]['ok'] is True
        assert frames[0]['payload']['events'][0]['action'] == 'create'
    else:
        assert frames == []


@pytest.mark.asyncio
async def test_default_and_maximum_limit_filter_unrelated_sessions_and_publication_private_facts(audit):
    from dataclasses import replace as changed
    from jiuwenswarm.server.runtime.session.sharing_audit import (
        SharingAuditBounds, SharingAuditContext, SharingAuditFacts, append_sharing_audit,
    )
    s = audit.s
    for _ in range(100):
        s.host.store.grant('alice-session', s.alice.identity(), s.bob.identity(),
            actions=('view',), history=audit.history, expires_at=None)
    # Typed host append fixture only for publication projection; original publication
    # transaction atomicity is covered separately by the existing publication tests.
    with s.access._locked():
        data = s.access._load()
        facts = SharingAuditFacts(action='continue', source_session_id='alice-session',
            share_id=audit.share['share_id'], target=s.bob.identity(), share_revision=1,
            owner_revision=1, source_revision=1, target_session_id='private-target-session',
            target_project_id='private-target-project', target_revision=1,
            publication_id='a' * 64, seed_digest='b' * 64, phase='publication',
            bounds_after=SharingAuditBounds(('view',), audit.history, None))
        append_sharing_audit(data, SharingAuditContext(s.bob.identity()), facts)
        append_sharing_audit(data, SharingAuditContext(s.bob.identity()), changed(facts,
            source_session_id='bob-session', bounds_after=SharingAuditBounds(('view',),
            changed(audit.history, session_id='bob-session',
                    stream=hashlib.sha256(b'bob-session\0').hexdigest()), None)))
        s.access._save(data)
    for limit, count in [(None, 50), (100, 100)]:
        req = request(**({} if limit is None else {'limit': limit}))
        with admitted(audit, req):
            response = await audit.adapter.handle(req)
            response._delivery_guard()
        assert response.ok and len(response.payload['events']) == count
        assert response.payload['has_more'] is True
        newest = response.payload['events'][0]
        assert newest['sequence'] == 102 and newest['action'] == 'continue'
        assert newest['actor_id'] == 'bob' and newest['method'] == 'host_api'
        assert not any(value in json.dumps(response.payload) for value in
            ('private-target-session', 'private-target-project', 'a' * 64, 'b' * 64, 'subject_id', 'history'))


@pytest.mark.asyncio
async def test_after_thread_read_guard_rejects_before_adapter_returns(audit, monkeypatch):
    from jiuwenswarm.server.runtime.gateway_adapter import session_sharing_adapter as module
    original = module.run_history_io

    async def read_then_revoke(*args, **kwargs):
        response = await original(*args, **kwargs)
        audit.s.auth.revoke(audit.s.alice)
        return response

    monkeypatch.setattr(module, 'run_history_io', read_then_revoke)
    with admitted(audit, request()):
        response = await audit.adapter.handle(request())
    assert not response.ok and 'events' not in response.payload
