"""Real Runtime/sidecar through adapter, E2A codec and final browser writer.

Only Session allocation and sockets/principal lookup are synthetic. Runtime,
Provisioner, claim/publication, source/target authority and delivery checkers are
real. These are local integration tests, not real Provider or browser acceptance.
"""
import asyncio
import json
from dataclasses import asdict, replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.common.e2a.wire_codec import parse_agent_server_wire_unary
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.governance import organization_auth, session_boundary
from jiuwenswarm.governance.session_boundary import admit_session_request
from jiuwenswarm.gateway.channel_manager.base import RobotMessageRouter
from jiuwenswarm.gateway.channel_manager.web.web_connect import WebChannel, WebChannelConfig
from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer
from jiuwenswarm.server.runtime.gateway_adapter.base import AdapterRegistry
from jiuwenswarm.server.runtime.session.continuation_publication import read_approval
from tests.unit_tests.runtime import test_continuation_transaction as transactions

setup = transactions.setup
transaction = transactions.transaction
ALICE, BOB = transactions.ALICE, transactions.BOB
OPTIONS = 'session.share.continuation.options'
CONTINUE = 'session.share.continue'


def wire_params(tx, method):
    result = asdict(tx.request)
    if method == OPTIONS:
        return {key: result[key] for key in ('session_id', 'share_id', 'expected_revision', 'target_project_id')}
    return result


@pytest.fixture
async def hops(transaction, monkeypatch):
    tx = transaction
    browser_identity = [BOB]
    principal = SimpleNamespace(identity=lambda: browser_identity[0])
    monkeypatch.setattr(organization_auth, 'configured_authenticator', lambda: object())
    monkeypatch.setattr(organization_auth, 'connection_principal', lambda _: principal)
    browser = SimpleNamespace(_jiuwen_ws_id='bob-browser', closed=False, send=AsyncMock(), _jiuwen_session_permits={})
    channel = WebChannel(WebChannelConfig(enabled=True), RobotMessageRouter())
    queue = asyncio.Queue()
    channel._send_queues['bob-browser'] = queue
    server = object.__new__(AgentWebSocketServer)
    server._organization_session_host = tx.setup.host
    server._trusted_identity_resolver = lambda _: tx.setup.identities[0]
    server._execution_runtime = lambda: tx.runtime
    server._adapter_registry = AdapterRegistry()
    server._install_sharing_adapters()
    await tx.runtime.start()
    yield SimpleNamespace(**locals())


async def first_hop(hops, method, data=None, request_id='one'):
    h = hops
    data = wire_params(h.tx, method) if data is None else data
    permit = admit_session_request(method, data, identity_resolver=h.principal.identity, host=h.tx.setup.host)
    h.browser._jiuwen_session_permits[request_id] = permit
    request = SimpleNamespace(req_method=ReqMethod(method), params=data, request_id=request_id,
                              channel_id='web', metadata={}, agent_ref=None)
    server_socket = SimpleNamespace(send=AsyncMock())
    with session_boundary.delivery_scope():
        session_boundary.set_delivery_permit(permit)
        assert await h.server._dispatch_gateway_adapter_request(server_socket, request, asyncio.Lock())
    server_socket.send.assert_awaited_once()
    raw = server_socket.send.call_args.args[0]
    # This real encode/decode boundary intentionally drops private guard objects.
    response = parse_agent_server_wire_unary(json.loads(raw))
    assert not hasattr(response, '_delivery_guard')
    return response, raw


async def enqueue(hops, response):
    await hops.channel.send_response(hops.browser, response.request_id, ok=response.ok,
                                    payload=response.payload,
                                    code=response.payload.get('code') if not response.ok else None)


async def drain(hops):
    hops.queue.put_nowait(None)
    await hops.channel._writer_loop(hops.browser, 'bob-browser')


def revoke_resource(h):
    h.tx.setup.access.revoke_resource(h.tx.setup.target.project_id, BOB, 'model',
                                     subject_id=BOB.subject_id, expected_revision=4)


def assert_no_private_payload(raw):
    for forbidden in ('SYNTHETIC-UNCONSUMED', 'model-account:bob', 'models.example',
                      'child-secret', 'never-copy', 'not-context', 'text-0', '_delivery_guard'):
        assert forbidden not in raw


@pytest.mark.asyncio
async def test_options_and_continue_cross_both_real_authority_boundaries(hops):
    h = hops
    options, raw_options = await first_hop(h, OPTIONS)
    assert options.ok and options.payload['options'] == [{
        'execution_profile_id': 'native', 'provider_id': 'native', 'mode': 'agent.work.normal',
        'model_name': 'synthetic#0', 'label': 'synthetic',
    }]
    assert not h.tx.allocated
    await enqueue(h, options)
    source_before = h.tx.setup.path.read_bytes()
    created, raw_created = await first_hop(h, CONTINUE, request_id='create')
    assert created.ok, created.payload
    sid = created.payload['session_id']
    assert h.tx.setup.host.owner_current(sid, BOB)
    assert not h.tx.setup.host.owner_current(sid, ALICE)
    assert h.tx.setup.host.owner_current(h.tx.request.session_id, ALICE)
    approval = read_approval(h.tx.setup.host, sid, BOB)
    assert approval['state'] == 'committed' and approval['proof']['request'] == asdict(h.tx.request)
    assert h.tx.setup.path.read_bytes() == source_before
    await enqueue(h, created)
    await drain(h)
    assert h.browser.send.await_count == 2
    assert not h.channel._clients_by_key and not h.channel._ws_sessions
    assert h.tx.manager.warm_pool.claim.await_count == 0
    for raw in (raw_options, raw_created, *(call.args[0] for call in h.browser.send.await_args_list)):
        assert_no_private_payload(raw)


@pytest.mark.asyncio
@pytest.mark.parametrize('method', [OPTIONS, CONTINUE])
@pytest.mark.parametrize('change', ['source', 'resource', 'catalog', 'identity', 'revision'])
async def test_queued_real_response_is_withdrawn_after_authority_change(hops, method, change):
    h = hops
    response, raw = await first_hop(h, method)
    assert response.ok, response.payload
    await enqueue(h, response)
    assert h.queue.qsize() == 1
    if change == 'source':
        h.tx.setup.host.store.revoke(h.tx.request.share_id, ALICE, expected_revision=1)
    elif change == 'resource':
        revoke_resource(h)
    elif change == 'catalog':
        h.tx.catalog['models']['defaults'][0]['model_config_obj']['temperature'] = 0.8
    elif change == 'identity':
        # AgentServer's principal remains Bob; the final independent browser
        # principal changes. Actor-name equality cannot conceal subject change.
        h.browser_identity[0] = replace(BOB, subject_id='other-browser-subject')
    else:
        h.tx.setup.host.store.revise(h.tx.request.share_id, ALICE, actions={'view', 'execute'},
            history=h.tx.setup.scope, expires_at=180, expected_revision=1)
    await drain(h)
    h.browser.send.assert_not_awaited()
    assert_no_private_payload(raw)
    assert not h.channel._clients_by_key
    if method == CONTINUE:
        # Loss of a response never compensates an already committed business record.
        assert transactions.owner(h.tx)['continuation']['state'] == 'committed'
        assert h.tx.released == []


@pytest.mark.asyncio
@pytest.mark.parametrize('method', [OPTIONS, CONTINUE])
async def test_target_revocation_between_sockets_rejects_before_enqueue(hops, method):
    response, _ = await first_hop(hops, method)
    assert response.ok
    revoke_resource(hops)
    await enqueue(hops, response)
    assert hops.queue.empty()
    await drain(hops)
    hops.browser.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_real_failed_continue_sends_only_safe_error(hops):
    revoke_resource(hops)
    response, raw = await first_hop(hops, CONTINUE)
    assert not response.ok and response.payload['code'] == 'FORBIDDEN'
    assert not hops.tx.allocated
    await enqueue(hops, response)
    await drain(hops)
    hops.browser.send.assert_awaited_once()
    wire = json.loads(hops.browser.send.call_args.args[0])
    assert wire['ok'] is False and wire['payload'] == {} and wire['code'] == 'FORBIDDEN'
    assert_no_private_payload(raw)
    assert_no_private_payload(hops.browser.send.call_args.args[0])


@pytest.mark.asyncio
async def test_real_options_filter_missing_credential_without_allocating(hops):
    revoke_resource(hops)
    response, raw = await first_hop(hops, OPTIONS)
    assert response.ok and response.payload['options'] == []
    assert not hops.tx.allocated
    await enqueue(hops, response)
    await drain(hops)
    hops.browser.send.assert_awaited_once()
    assert_no_private_payload(raw)


@pytest.mark.asyncio
async def test_same_token_after_lost_response_uses_one_real_committed_session(hops):
    first, _ = await first_hop(hops, CONTINUE, request_id='lost')
    assert first.ok
    second, _ = await first_hop(hops, CONTINUE, request_id='retry')
    assert second.ok and second.payload == first.payload
    assert hops.tx.allocated == [first.payload['session_id']]
    await enqueue(hops, second)
    await drain(hops)
    hops.browser.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_other_owned_continuation_cannot_replace_original_token_result(hops):
    first, _ = await first_hop(hops, CONTINUE, request_id='first')
    changed = asdict(replace(hops.tx.request, create_token='different-attempt'))
    second, _ = await first_hop(hops, CONTINUE, changed, request_id='second')
    assert first.ok and second.ok and first.payload['session_id'] != second.payload['session_id']
    first.payload['session_id'] = second.payload['session_id']
    await enqueue(hops, first)
    assert hops.queue.empty()
    await drain(hops)
    hops.browser.send.assert_not_awaited()
