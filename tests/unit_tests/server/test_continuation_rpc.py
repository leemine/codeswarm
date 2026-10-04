"""Continuation RPC wiring and existing final writer, with explicit host-port fakes.

Real sidecar proves source permits. Target delivery checker is a stub until the
parent Runtime package is integrated; no Provider or HTTP call is made here.
"""
import asyncio
import json
import sys
from dataclasses import replace
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.governance import organization_auth, session_boundary
from jiuwenswarm.governance.continuation import ContinuationInput
from jiuwenswarm.governance.session_boundary import admit_session_request
from jiuwenswarm.governance.session_sharing import SessionSharingConflict
from jiuwenswarm.server.runtime.gateway_adapter.continuation_adapter import ContinuationAdapter
from tests.unit_tests.server import test_sharing_host as host_tests
from tests.unit_tests.server.test_sharing_host import ALICE, BOB, grant

setup = host_tests.setup
OPTIONS = 'session.share.continuation.options'
CONTINUE = 'session.share.continue'


def params(share, method=CONTINUE):
    result = dict(session_id='session', share_id=share['share_id'], expected_revision=share['revision'],
                  target_project_id='target-project')
    if method == CONTINUE:
        result.update(create_token='fixed-attempt-token', execution_profile_id='configured-native',
                      model_name='fixture-model#0', mode='agent.code.normal', title='Private continuation')
    return result


def executable_share(setup, actions=('view', 'execute')):
    host, access, pid, *_ = setup
    access.replace_acl(pid, 'admin', acl={'alice': ['read', 'admin', 'execute']}, expected_revision=2)
    scope = host.prepare_source('session', ALICE)
    return grant(host, scope, actions=actions)


def request(method, data):
    return SimpleNamespace(req_method=ReqMethod(method), params=data, request_id='request', channel_id='web',
                           metadata={}, agent_ref=None)


@pytest.mark.parametrize('method', [OPTIONS, CONTINUE])
@pytest.mark.parametrize('rights', [('view',), ('execute',)])
def test_source_must_supply_both_actions_on_exact_share(setup, method, rights):
    share = executable_share(setup, rights)
    host = setup[0]
    # A second grant supplies the other action but must not complete this one.
    grant(host, host.prepare_source('session', ALICE), actions=tuple({'view', 'execute'} - set(rights)))
    with pytest.raises(PermissionError):
        admit_session_request(method, params(share, method), identity_resolver=lambda: BOB, host=host)


@pytest.mark.parametrize('method', [OPTIONS, CONTINUE])
def test_source_permit_captures_revision_identity_and_immutable_input(setup, method):
    share = executable_share(setup)
    host = setup[0]
    actor = [BOB]
    data = params(share, method)
    permit = admit_session_request(method, data, identity_resolver=lambda: actor[0], host=host)
    assert permit.revalidate() and not permit.owners and permit.share_actions == ('view', 'execute')
    data['target_project_id'] = 'mutated-wire'
    if method == CONTINUE:
        assert permit.continuation_input.target_project_id == 'target-project'
    else:
        assert dict(permit.continuation_options)['target_project_id'] == 'target-project'
    actor[0] = replace(BOB, subject_id='same-actor-different-subject')
    assert not permit.revalidate()
    actor[0] = BOB
    host.store.revoke(share['share_id'], ALICE, expected_revision=1)
    assert not permit.revalidate()


@pytest.mark.parametrize('method', [OPTIONS, CONTINUE])
@pytest.mark.parametrize('change', ['extra', 'bool_revision', 'wrong_revision', 'no_target'])
def test_invalid_source_inputs_denied(setup, method, change):
    share = executable_share(setup)
    data = params(share, method)
    if change == 'extra':
        data['identity'] = {'actor_id': 'alice'}
    elif change == 'bool_revision':
        data['expected_revision'] = True
    elif change == 'wrong_revision':
        data['expected_revision'] += 1
    else:
        data.pop('target_project_id')
    with pytest.raises((PermissionError, ValueError)):
        admit_session_request(method, data, identity_resolver=lambda: BOB, host=setup[0])


@pytest.mark.asyncio
@pytest.mark.parametrize('method', [OPTIONS, CONTINUE])
async def test_adapter_delegates_typed_input_and_retains_private_final_guard(method):
    actor, current = [BOB], [True]
    def check():
        if not current[0]:
            raise PermissionError('synthetic-private-diagnostic')
    result = SimpleNamespace(to_payload=lambda: {'safe': 'result'}, revalidate=check)
    runtime = SimpleNamespace(continue_session=AsyncMock(return_value=result), continuation_options=AsyncMock(return_value=result))
    adapter = ContinuationAdapter(runtime_resolver=lambda: runtime, identity_resolver=lambda _: actor[0])
    data = params({'share_id': 'share', 'revision': 1}, method)
    response = await adapter.handle(request(method, data))
    assert response.ok and response.payload == {'safe': 'result'}
    called = runtime.continue_session if method == CONTINUE else runtime.continuation_options
    bound = called.call_args.args[0]
    assert isinstance(bound, ContinuationInput if method == CONTINUE else dict)
    response._delivery_guard()
    actor[0] = None
    with pytest.raises(PermissionError):
        response._delivery_guard()
    actor[0] = BOB
    current[0] = False
    with pytest.raises(PermissionError):
        response._delivery_guard()
    assert '_delivery_guard' not in vars(response).get('metadata', {})


@pytest.mark.asyncio
@pytest.mark.parametrize('failure,code', [(PermissionError, 'FORBIDDEN'), (ValueError, 'BAD_REQUEST'),
                                         (SessionSharingConflict, 'CONFLICT'), (RuntimeError, 'FORBIDDEN')])
async def test_adapter_errors_are_static_and_never_fallback(failure, code):
    runtime = SimpleNamespace(continue_session=AsyncMock(side_effect=failure('synthetic-secret')))
    adapter = ContinuationAdapter(runtime_resolver=lambda: runtime, identity_resolver=lambda _: BOB)
    response = await adapter.handle(request(CONTINUE, params({'share_id': 'share', 'revision': 1})))
    assert response.ok is False and response.payload['code'] == code
    assert 'synthetic-secret' not in json.dumps(response.payload)


@pytest.mark.asyncio
async def test_adapter_identity_loss_after_runtime_await_denies_payload():
    actor = [BOB]
    async def create(_):
        actor[0] = None
        return SimpleNamespace(to_payload=lambda: pytest.fail('stale payload projected'), revalidate=lambda: None)
    adapter = ContinuationAdapter(runtime_resolver=lambda: SimpleNamespace(continue_session=create),
                                  identity_resolver=lambda _: actor[0])
    response = await adapter.handle(request(CONTINUE, params({'share_id': 'share', 'revision': 1})))
    assert response.ok is False and response.payload['code'] == 'FORBIDDEN'


def gateway(setup, monkeypatch, method):
    from jiuwenswarm.gateway.channel_manager.base import RobotMessageRouter
    from jiuwenswarm.gateway.channel_manager.web.web_connect import WebChannel, WebChannelConfig
    share = executable_share(setup)
    actor = [BOB]
    host = setup[0]
    monkeypatch.setattr(organization_auth, 'configured_authenticator', lambda: object())
    monkeypatch.setattr(organization_auth, 'connection_principal', lambda _: SimpleNamespace(identity=lambda: actor[0]))
    monkeypatch.setattr(session_boundary, 'organization_sharing_host', lambda: host)
    permit = admit_session_request(method, params(share, method), identity_resolver=lambda: actor[0], host=host)
    ws = SimpleNamespace(_jiuwen_ws_id='browser', closed=False, send=AsyncMock(), _jiuwen_session_permits={'r': permit})
    channel = WebChannel(WebChannelConfig(enabled=True), RobotMessageRouter())
    queue = asyncio.Queue()
    channel._send_queues['browser'] = queue
    return channel, ws, queue, permit, actor


@pytest.mark.asyncio
@pytest.mark.parametrize('method', [OPTIONS, CONTINUE])
@pytest.mark.parametrize('change', ['none', 'target_revoke', 'source_revoke', 'identity'])
async def test_gateway_final_writer_rechecks_captured_host_authority(setup, monkeypatch, method, change):
    channel, ws, queue, permit, actor = gateway(setup, monkeypatch, method)
    valid, checked, captured = [True], [], []
    def capture(host, resolver, original, payload_or_sid):
        captured.append((host, resolver(), original, payload_or_sid))
        def check():
            checked.append(True)
            if not valid[0]:
                raise PermissionError('changed-current-target')
        return check
    stub = ModuleType('jiuwenswarm.runtime.continuation_delivery')
    stub.capture_continuation_delivery = capture
    stub.capture_continuation_options_delivery = capture
    monkeypatch.setitem(sys.modules, stub.__name__, stub)
    payload = {'session_id': 'new-private'} if method == CONTINUE else {**dict(permit.continuation_options), 'options': []}
    channel._enqueue_send(ws, {'type': 'res', 'id': 'r', 'ok': True, 'payload': payload})
    assert queue.qsize() == 1 and len(captured) == len(checked) == 1
    assert captured[0][0] is setup[0] and captured[0][1] == BOB
    assert captured[0][2] == (permit.continuation_input if method == CONTINUE else dict(permit.continuation_options))
    assert captured[0][3] == ('new-private' if method == CONTINUE else payload)
    if change == 'target_revoke':
        valid[0] = False
    elif change == 'source_revoke':
        setup[0].store.revoke(permit.share[1], ALICE, expected_revision=1)
    elif change == 'identity':
        actor[0] = replace(BOB, authority='different-authority')
    queue.put_nowait(None)
    await channel._writer_loop(ws, 'browser')
    assert ws.send.await_count == (1 if change == 'none' else 0)
    assert not channel._clients_by_key  # Neither source nor target is subscribed.
    if change in ('none', 'target_revoke'):
        assert len(checked) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('method', [OPTIONS, CONTINUE])
async def test_gateway_host_checker_failure_drops_before_queue(setup, monkeypatch, method):
    channel, ws, queue, _, _ = gateway(setup, monkeypatch, method)
    capture = Mock(side_effect=PermissionError('wrong target approval/token'))
    stub = ModuleType('jiuwenswarm.runtime.continuation_delivery')
    stub.capture_continuation_delivery = stub.capture_continuation_options_delivery = capture
    monkeypatch.setitem(sys.modules, stub.__name__, stub)
    channel._enqueue_send(ws, {'type': 'res', 'id': 'r', 'ok': True, 'payload': {'session_id': 'substituted-owned'}})
    assert queue.empty() and capture.call_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('method', [OPTIONS, CONTINUE])
async def test_web_handlers_explicitly_forward_both_methods(monkeypatch, method):
    from tests.unit_tests.gateway.test_app_web_handlers import FakeWebChannel
    from jiuwenswarm.gateway.channel_manager.web.app_web_handlers import WebHandlersBindParams, _register_web_handlers
    proxy = AsyncMock()
    monkeypatch.setattr('jiuwenswarm.gateway.routing.e2a_proxy.proxy_unary_request', proxy)
    channel = FakeWebChannel()
    _register_web_handlers(WebHandlersBindParams(channel=channel, agent_client=object()))
    data = params({'share_id': 'share', 'revision': 1}, method)
    await channel.methods[method](object(), 'r', data, 'source-envelope', user_id='routing-only')
    assert proxy.call_args.kwargs['req_method'] is ReqMethod(method)
    assert proxy.call_args.kwargs['params'] is data


def test_agentserver_installs_both_runtime_methods():
    from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer
    from jiuwenswarm.server.runtime.gateway_adapter.base import AdapterRegistry
    server = object.__new__(AgentWebSocketServer)
    server._organization_session_host = SimpleNamespace(store=object(), target_resolver=lambda *_: None,
                                                        compile_history=lambda *_: None,
                                                        owner_revision=lambda *_: None)
    server._adapter_registry = AdapterRegistry()
    server._resolve_trusted_identity = lambda _: BOB
    server._execution_runtime = lambda: object()
    server._install_sharing_adapters()
    assert type(server._adapter_registry.get(OPTIONS)) is ContinuationAdapter
    assert server._adapter_registry.get(CONTINUE) is server._adapter_registry.get(OPTIONS)


@pytest.mark.asyncio
@pytest.mark.parametrize('method', [OPTIONS, CONTINUE])
async def test_agentserver_final_guard_after_send_lock(method, monkeypatch):
    from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer
    from jiuwenswarm.server.runtime.gateway_adapter.base import AdapterRegistry
    monkeypatch.setattr(organization_auth, 'configured_authenticator', lambda: None)
    valid = [True]
    def check():
        if not valid[0]:
            raise PermissionError('changed after wait')
    result = SimpleNamespace(to_payload=lambda: {'private': 'must not send'}, revalidate=check)
    runtime = SimpleNamespace(continue_session=AsyncMock(return_value=result), continuation_options=AsyncMock(return_value=result))
    adapter = ContinuationAdapter(runtime_resolver=lambda: runtime, identity_resolver=lambda _: BOB)
    server = object.__new__(AgentWebSocketServer)
    server._organization_session_host = None
    server._adapter_registry = AdapterRegistry()
    server._adapter_registry.register(adapter)
    class SendLock:
        async def __aenter__(self):
            valid[0] = False
        async def __aexit__(self, *_):
            pass
    ws = SimpleNamespace(send=AsyncMock())
    assert await server._dispatch_gateway_adapter_request(ws, request(method, params({'share_id': 'share', 'revision': 1}, method)), SendLock())
    wire = ws.send.call_args.args[0]
    assert 'FORBIDDEN' in wire and 'must not send' not in wire


@pytest.mark.asyncio
@pytest.mark.parametrize('method', [OPTIONS, CONTINUE])
async def test_web_request_does_not_register_source_subscription(setup, monkeypatch, method):
    channel, ws, _, permit, _ = gateway(setup, monkeypatch, method)
    channel._process_files = AsyncMock(side_effect=lambda params, **_: params)
    channel.register_ws = AsyncMock(side_effect=AssertionError('source subscription reached'))
    channel._on_message_cb = AsyncMock(return_value=True)
    channel._connection_user_id = lambda _: 'bob'
    data = params({'share_id': permit.share[1], 'revision': permit.share[2]}, method)
    await channel._handle_authenticated_raw_message(ws, json.dumps({'type': 'req', 'id': 'new', 'method': method, 'params': data}), {})
    channel.register_ws.assert_not_awaited()
    channel._on_message_cb.assert_awaited_once()
    assert not channel._clients_by_key and not channel._ws_sessions
