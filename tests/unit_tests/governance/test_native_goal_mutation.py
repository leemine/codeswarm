"""Real owner/project store admission and final wire sink; no Goal execution."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from jiuwenswarm.common.schema.agent import AgentResponse
from jiuwenswarm.common.e2a.wire_codec import encode_agent_response_for_wire
from jiuwenswarm.governance.goal_mutation import validate_goal_mutation
from jiuwenswarm.governance.organization_auth import authenticated_scope
from jiuwenswarm.governance.session_boundary import (
    admit_session_request, delivery_scope, set_delivery_permit,
)
from jiuwenswarm.governance.session_sharing import SessionSharingDenied
from jiuwenswarm.server.runtime.session import session_metadata
from jiuwenswarm.server.ws_send import send_wire_payload
from tests.unit_tests.runtime import test_native_goal_read_runtime as base

credentials = base.credentials
source = base.source
case = base.case


def parameters(f, action='set'):
    return {'session_id': f.sid, 'action': action, 'mode': 'agent',
            **({'objective': 'ordinary goal', 'overwrite_confirmed': True,
                'token_budget': 100, 'max_attempts': 2} if action == 'set' else {})}


def allow_execute(f):
    f.access.replace_acl(f.project.project_id, 'alice',
                         acl={'bob': ['read', 'execute']}, expected_revision=2)
    assert not f.access.authorize(f.project.project_id, 'bob', 'admin').allowed


@pytest.mark.parametrize('action', ['set', 'resume', 'pause', 'clear'])
async def test_owner_execute_without_admin_is_admitted_without_allocation(case, action):
    f = case
    params = parameters(f, action)
    with pytest.raises(SessionSharingDenied):
        admit_session_request('command.goal', params, identity_resolver=f.bob.identity, host=f.host)
    allow_execute(f)
    before = dict(f.c.store._store)
    permit = admit_session_request('command.goal', params, identity_resolver=f.bob.identity, host=f.host)
    assert permit.revalidate() and permit.goal_read_route is None
    assert permit.goal_mutation_route is not None
    assert f.c.store._store == before
    assert f.runtime._session_coordinator.snapshot_session(f.sid) is None
    assert not f.manager._agent_borrowers
    with pytest.raises(SessionSharingDenied):
        admit_session_request('command.goal', params, identity_resolver=f.alice.identity, host=f.host)


@pytest.mark.parametrize('extra', [
    {'action': 'get'}, {'action': 'SET'}, {'share_id': 'share'}, {'provider_id': 'native'},
    {'execution_profile_id': 'other'}, {'attach_goal': True}, {'query': 'input'},
    {'token_budget': True}, {'max_attempts': 1.0}, {'overwrite_confirmed': 1},
    {'objective': None}, {'objective': ' '}, {'model_name': None},
])
def test_exact_mutation_schema_rejects_unapproved_or_ambiguous_values(extra):
    params = {'action': 'set', 'objective': 'ordinary'} | extra
    with pytest.raises(SessionSharingDenied):
        validate_goal_mutation(params)


@pytest.mark.parametrize('action', ['resume', 'pause', 'clear'])
def test_control_cannot_smuggle_new_objective(action):
    with pytest.raises(SessionSharingDenied):
        validate_goal_mutation({'action': action, 'objective': 'new work'})


@pytest.mark.parametrize('change', ['params', 'route', 'storage'])
async def test_first_identity_callback_cannot_retarget_original_command(case, change):
    f = case
    allow_execute(f)
    params = parameters(f)
    first = True
    original_storage = f.host._storage
    def identity():
        nonlocal first
        if first:
            first = False
            if change == 'params':
                params['objective'] = 'replacement'
            elif change == 'route':
                metadata = session_metadata.get_session_metadata(f.sid, cache_bust=True, enable_writeback=False)
                metadata['execution_config_revision'] = 'replacement'
                session_metadata._write_metadata_sync(f.sid, metadata)
            else:
                f.host._storage = object()
        return f.bob.identity()
    try:
        with pytest.raises(SessionSharingDenied):
            admit_session_request('command.goal', params, identity_resolver=identity, host=f.host)
    finally:
        f.host._storage = original_storage


async def test_model_hint_uses_original_metadata_selection_key(case):
    f = case
    allow_execute(f)
    session_metadata.update_session_metadata(session_id=f.sid, model='model#1', sync_write=True)
    params = parameters(f) | {'model_name': 'model#1'}
    permit = admit_session_request('command.goal', params, identity_resolver=f.bob.identity, host=f.host)
    assert permit.revalidate()
    with pytest.raises(SessionSharingDenied):
        admit_session_request('command.goal', params | {'model_name': 'model#0'},
                              identity_resolver=f.bob.identity, host=f.host)
    session_metadata.update_session_metadata(session_id=f.sid, model='model#0', sync_write=True)
    assert not permit.revalidate()


@pytest.mark.parametrize('change', ['none', 'acl', 'identity', 'params', 'route'])
async def test_actual_agentserver_sink_rechecks_after_original_send_lock(case, change):
    f = case
    allow_execute(f)
    params = parameters(f)
    sent = []
    async def send(frame):
        sent.append(json.loads(frame))
    lock, socket = asyncio.Lock(), SimpleNamespace(send=send)
    with authenticated_scope(f.bob), delivery_scope():
        permit = admit_session_request('command.goal', params, identity_resolver=f.bob.identity, host=f.host)
        set_delivery_permit(permit)
        response = AgentResponse(request_id='goal', channel_id='web', ok=True,
                                  payload={'goal': {'objective': 'private result'}})
        wire = encode_agent_response_for_wire(response, response_id='goal')
        await lock.acquire()
        async def deliver():
            async with lock:
                return await send_wire_payload(socket, wire)
        task = asyncio.create_task(deliver())
        await asyncio.sleep(0)
        if change == 'acl':
            f.access.replace_acl(f.project.project_id, 'alice', acl={'bob': ['read']}, expected_revision=3)
        elif change == 'identity':
            f.auth.revoke(f.bob)
        elif change == 'params':
            params['objective'] = 'replacement'
        elif change == 'route':
            metadata = session_metadata.get_session_metadata(f.sid, cache_bust=True, enable_writeback=False)
            metadata['execution_config_revision'] = 'replacement'
            session_metadata._write_metadata_sync(f.sid, metadata)
        lock.release()
        await asyncio.wait_for(task, 2)
    assert len(sent) == 1
    assert ('private result' in json.dumps(sent)) == (change == 'none')
    if change != 'none':
        assert 'FORBIDDEN' in json.dumps(sent)


@pytest.mark.parametrize('hint', ['model#0', 'Default Label'])
async def test_empty_original_model_does_not_select_new_default_from_hint(case, hint):
    f = case
    allow_execute(f)
    with pytest.raises(SessionSharingDenied):
        admit_session_request('command.goal', parameters(f) | {'model_name': hint},
                              identity_resolver=f.bob.identity, host=f.host)


@pytest.mark.parametrize('change', ['team', 'profile', 'session'])
async def test_original_native_single_route_cannot_be_selected_from_request(case, change):
    f = case
    allow_execute(f)
    params = parameters(f)
    metadata = session_metadata.get_session_metadata(f.sid, cache_bust=True, enable_writeback=False)
    if change == 'team':
        metadata['mode'] = 'team.work.normal'
    elif change == 'profile':
        metadata['execution_profile_id'] = 'missing'
    else:
        params['session_id'] = 'different'
    session_metadata._write_metadata_sync(f.sid, metadata)
    with pytest.raises(SessionSharingDenied):
        admit_session_request('command.goal', params, identity_resolver=f.bob.identity,
                              host=f.host, envelope_session=f.sid)


@pytest.mark.parametrize('change', ['none', 'acl', 'credential'])
async def test_actual_gateway_writer_rechecks_mutation_permit(case, monkeypatch, change):
    from jiuwenswarm.gateway.channel_manager.base import RobotMessageRouter
    from jiuwenswarm.gateway.channel_manager.web.web_connect import WebChannel, WebChannelConfig
    from jiuwenswarm.governance import organization_auth
    f, sent = case, []
    allow_execute(f)
    async def send(frame):
        sent.append(json.loads(frame))
    socket = SimpleNamespace(_jiuwen_ws_id='goal-mutator', closed=False, send=send)
    monkeypatch.setattr(organization_auth, 'connection_principal', lambda ws: f.bob)
    with authenticated_scope(f.bob):
        permit = admit_session_request('command.goal', parameters(f),
                                      identity_resolver=f.bob.identity, host=f.host)
        socket._jiuwen_session_permits = {'goal': permit}
    channel = WebChannel(WebChannelConfig(enabled=True), RobotMessageRouter())
    queue = asyncio.Queue()
    channel._send_queues['goal-mutator'] = queue
    channel._enqueue_send(socket, {'type': 'res', 'id': 'goal', 'ok': True,
                                  'payload': {'goal': {'objective': 'private result'}}})
    assert queue.qsize() == 1
    if change == 'acl':
        f.access.replace_acl(f.project.project_id, 'alice', acl={'bob': ['read']}, expected_revision=3)
    elif change == 'credential':
        f.auth.revoke(f.bob)
    await queue.put(None)
    await asyncio.wait_for(channel._writer_loop(socket, 'goal-mutator'), 2)
    if change == 'none':
        assert len(sent) == 1 and 'private result' in json.dumps(sent)
    else:
        assert not sent


@pytest.fixture
def mutation_delivery_type(monkeypatch):
    """Nominal contract fixture until the separate Runtime package is integrated.

    This tests the boundary's binding only, not actual core receipt semantics.
    """
    import sys
    from dataclasses import dataclass
    from types import ModuleType
    @dataclass(frozen=True)
    class NativeGoalMutationDelivery:
        session_id: str
        identity: object
        final_check: object
    module = ModuleType('jiuwenswarm.runtime.native_goal_mutation')
    module.NativeGoalMutationDelivery = NativeGoalMutationDelivery
    monkeypatch.setitem(sys.modules, module.__name__, module)
    return NativeGoalMutationDelivery


@pytest.mark.parametrize('change', ['none', 'result', 'route'])
async def test_local_result_receipt_rechecked_at_actual_send(case, mutation_delivery_type, change):
    from jiuwenswarm.governance.session_boundary import bind_goal_mutation_delivery
    f = case
    allow_execute(f)
    params, sent, live = parameters(f), [], [True]
    def final_check():
        if not live[0]:
            raise SessionSharingDenied('Original mutation receipt changed')
    receipt = mutation_delivery_type(f.sid, f.bob.identity(), final_check)
    async def send(frame):
        sent.append(json.loads(frame))
    with authenticated_scope(f.bob), delivery_scope():
        permit = admit_session_request('command.goal', params, identity_resolver=f.bob.identity, host=f.host)
        set_delivery_permit(permit)
        bind_goal_mutation_delivery(f.sid, f.bob.identity(), receipt)
        if change == 'result':
            live[0] = False
        elif change == 'route':
            params['objective'] = 'changed after result'
        response = AgentResponse(request_id='goal', channel_id='web', ok=True,
                                  payload={'goal': {'objective': 'private result'}})
        await send_wire_payload(SimpleNamespace(send=send), encode_agent_response_for_wire(response, response_id='goal'))
    assert len(sent) == 1
    assert ('private result' in json.dumps(sent)) == (change == 'none')


@pytest.mark.parametrize('change', ['duck', 'sid', 'identity', 'truthy', 'route_reentry', 'duplicate'])
async def test_result_binding_rejects_wrong_or_reentrant_receipt(case, mutation_delivery_type, change):
    from jiuwenswarm.governance.session_boundary import bind_goal_mutation_delivery
    f = case
    allow_execute(f)
    params = parameters(f)
    def final_check():
        if change == 'route_reentry':
            params['objective'] = 'changed in receipt callback'
        return True if change == 'truthy' else None
    receipt = mutation_delivery_type('different' if change == 'sid' else f.sid,
        f.alice.identity() if change == 'identity' else f.bob.identity(), final_check)
    if change == 'duck':
        receipt = SimpleNamespace(session_id=f.sid, identity=f.bob.identity(), final_check=final_check)
    with delivery_scope():
        set_delivery_permit(admit_session_request('command.goal', params,
            identity_resolver=f.bob.identity, host=f.host))
        if change == 'duplicate':
            bind_goal_mutation_delivery(f.sid, f.bob.identity(), receipt)
        with pytest.raises((SessionSharingDenied, TypeError)):
            bind_goal_mutation_delivery(f.sid, f.bob.identity(), receipt)


def test_direct_result_checks_receipt_without_creating_wire_permit(mutation_delivery_type):
    from jiuwenswarm.governance.contracts import TrustedIdentity
    from jiuwenswarm.governance import session_boundary
    checked = []
    identity = TrustedIdentity('bob', 'bob', 'host')
    with delivery_scope():
        session_boundary.bind_goal_mutation_delivery('owned', identity,
            mutation_delivery_type('owned', identity, lambda: checked.append(True)))
        assert session_boundary._delivery.get() is None
    assert checked == [True]


@pytest.mark.parametrize('initial_mode', ['agent.work.normal', 'agent'])
@pytest.mark.parametrize('action', ['get', 'set', 'resume', 'pause', 'clear'])
async def test_original_usage_history_preserves_same_mode_delivery(case, monkeypatch, initial_mode, action):
    from jiuwenswarm.server.runtime.session import session_history
    f = case
    allow_execute(f)
    monkeypatch.setattr(session_history, 'get_agent_sessions_dir', session_metadata.get_agent_sessions_dir)
    # This fixture reuses its Session id under a fresh tmp root. Isolate the
    # production process cache so an earlier fixture cannot supply its project.
    monkeypatch.setattr(session_metadata, '_METADATA_CACHE', {})
    monkeypatch.setattr(session_metadata, '_METADATA_CACHE_GENERATIONS', {})
    metadata = session_metadata.get_session_metadata(f.sid, cache_bust=True, enable_writeback=False,
                                                    infer_defaults=False)
    metadata['mode'] = initial_mode
    session_metadata._write_metadata_sync(f.sid, metadata)
    params = parameters(f, action)
    permit = admit_session_request('command.goal', params, identity_resolver=f.bob.identity, host=f.host)
    assert permit.revalidate()
    session_history.append_history_record(session_id=f.sid, role='assistant', content='',
        event_type='context.usage', channel_id='web', mode='agent', request_id='goal-usage',
        timestamp=1.0, extra={'context_window': {}, 'parts': {}})
    assert session_metadata.flush_pending_writes()
    current = session_metadata.get_session_metadata(f.sid, cache_bust=True, enable_writeback=False,
                                                   infer_defaults=False)
    assert current['mode'] == 'agent'
    (permit.goal_read_route or permit.goal_mutation_route).check()
    assert permit.revalidate()


@pytest.mark.parametrize('change', [
    {'mode': 'agent.plan'}, {'mode': 'agent.unknown'}, {'mode': 'agent.code.normal'}, {'mode': 'team.work.normal'},
    {'work_mode': 'code'}, {'execution_profile_id': 'replacement'},
    {'execution_config_revision': 'replacement'}, {'execution_config_fingerprint': 'replacement'},
    {'channel_id': 'tui'}, {'model': 'replacement'},
])
async def test_semantic_mode_compatibility_does_not_allow_route_changes(case, change):
    f = case
    allow_execute(f)
    permit = admit_session_request('command.goal', parameters(f), identity_resolver=f.bob.identity, host=f.host)
    metadata = session_metadata.get_session_metadata(f.sid, cache_bust=True, enable_writeback=False,
                                                    infer_defaults=False)
    metadata.update(change)
    session_metadata._write_metadata_sync(f.sid, metadata)
    assert not permit.revalidate()
