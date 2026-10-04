import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from openjiuwen.harness.goal.schema import GoalRecord
from openjiuwen.harness.engine.config import config_fingerprint
from jiuwenswarm.common import config
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.governance.organization_auth import authenticated_scope
from jiuwenswarm.governance import session_boundary as boundary
from jiuwenswarm.runtime.harness.config_source import load_execution_catalog
from jiuwenswarm.runtime.native_goal_idle import capture_control
from jiuwenswarm.governance.preparation import GovernanceError
from jiuwenswarm.runtime.session import SessionWorkKind
from jiuwenswarm.runtime.session.model import SessionExecutionState
from jiuwenswarm.server.runtime.session import session_metadata
from jiuwenswarm.server.ws_send import send_wire_payload
from tests.unit_tests.runtime import test_native_goal_readmission_runtime as base

credentials = base.credentials
goal_case = base.goal_case
native_case = base.native_case
full_case = base.full_case
case = base.case

@pytest.fixture
async def ready(case, monkeypatch):
    f = case.f
    cfg = {'execution': {'default_profile_id': 'profile', 'profiles': {'profile': {
        'provider_id': 'native', 'config_revision': 'initial-goal'}}}}
    monkeypatch.setattr(config, 'get_config', lambda: cfg)
    spec = load_execution_catalog(cfg).source(explicit_profile_id='profile').resolve()
    metadata = session_metadata.get_session_metadata(f.sid, cache_bust=True, enable_writeback=False)
    metadata.update(execution_profile_id='profile', execution_config_revision=spec.config_revision,
                    execution_config_fingerprint=config_fingerprint(spec), model='')
    session_metadata._write_metadata_sync(f.sid, metadata)
    yield case


def request(x, rid='idle-clear'):
    f = x.f
    return AgentRequest(rid, session_id=f.sid, channel_id='web', is_stream=False,
        req_method=ReqMethod.COMMAND_GOAL, params={'session_id': f.sid, 'action': 'clear',
            'project_id': f.project.project_id})


def permit(x, req):
    p = boundary.admit_session_request('command.goal', req.params,
        identity_resolver=x.f.bob.identity, host=x.f.host)
    boundary.set_delivery_permit(p)
    return p


async def sent(req, body):
    ws = SimpleNamespace(send=AsyncMock())
    await send_wire_payload(ws, {'request_id': req.request_id, 'channel': 'web',
        'session_id': req.session_id, 'payload': body})
    return json.loads(ws.send.call_args.args[0])


@pytest.mark.asyncio
@pytest.mark.parametrize('stream', [False, True])
async def test_original_producer_finished_idle_clear_ack_is_allowed(ready, stream):
    x, f = ready, ready.f
    req = request(x)
    req.is_stream = stream
    with authenticated_scope(f.bob), boundary.delivery_scope():
        original = permit(x, req)
        result = ([v async for v in x.runtime.stream(req)] if stream else await x.runtime.invoke(req))
        assert len(result) == 1 and result[0].ok
        owner, = x.coordinator._registry.select(session_id=f.sid, request_id=req.request_id)
        assert (owner.task is None or owner.task.done()) and owner.state is SessionExecutionState.SUCCEEDED
        live = boundary._delivery.get()
        assert live is not original and live.goal_mutation_result is not None
        assert boundary.delivery_authorized()
        wire = await sent(req, result[0].payload)
        assert wire['payload']['action'] == 'clear' and 'FORBIDDEN' not in json.dumps(wire)
    assert f.c.outer.goal_manager.peek() is None and not f.c.side_effects


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['credential', 'owner', 'new_goal'])
async def test_final_agentserver_sink_denies_changed_original_ack(ready, change):
    x, f = ready, ready.f
    req = request(x)
    original_auth = f.authpath.read_bytes()
    try:
        with authenticated_scope(f.bob), boundary.delivery_scope():
            permit(x, req)
            result = await x.runtime.invoke(req)
            assert result[0].ok and boundary.delivery_authorized()
            if change == 'credential':
                f.auth.revoke(f.bob)
            elif change == 'owner':
                f.host.invalidate_source(f.sid, expected_epoch=f.host.source_epoch(f.sid))
            else:
                # Trusted fixture writes a distinct subsequent Goal; old clear
                # ACK must not describe this new original manager state.
                f.c.outer.goal_manager._store.save(GoalRecord.create(session_id=f.sid, objective='new'))
            assert not boundary.delivery_authorized()
            wire = await sent(req, result[0].payload)
            assert 'FORBIDDEN' in json.dumps(wire)
            assert 'cleared_goal' not in json.dumps(wire)
    finally:
        f.authpath.write_bytes(original_auth)


@pytest.mark.asyncio
async def test_finish_cannot_resurrect_failed_original_producer(ready):
    x, f = ready, ready.f
    req = request(x, 'failed-after-clear')
    saved = {}
    async def body():
        _, cap, finish = capture_control(x.runtime, req)
        await cap.clear()
        saved['receipt'] = finish()
        saved['finish'] = finish
        raise RuntimeError('synthetic producer failure after actual clear')
    with authenticated_scope(f.bob), boundary.delivery_scope():
        permit(x, req)
        with pytest.raises(RuntimeError, match='synthetic producer failure'):
            await x.coordinator.run_unary(f.sid, req.request_id, SessionWorkKind.GOAL_CONTROL, body)
        owner, = x.coordinator._registry.select(session_id=f.sid, request_id=req.request_id)
        assert (owner.task is None or owner.task.done()) and owner.state is SessionExecutionState.FAILED
        with pytest.raises(GovernanceError):
            saved['receipt'].final_check()
        with pytest.raises(GovernanceError):
            saved['finish']()
        with pytest.raises(GovernanceError):
            boundary.bind_goal_mutation_delivery(f.sid, f.bob.identity(), saved['receipt'])
        assert boundary._delivery.get().goal_mutation_result is None


@pytest.mark.asyncio
async def test_success_state_cannot_hide_actual_producer_finally_exception(ready):
    x, f = ready, ready.f
    req = request(x, 'failed-in-finally')
    saved = {}
    class ActualOperation:
        emitted = False
        def __aiter__(self):
            return self
        async def __anext__(self):
            if self.emitted:
                raise StopAsyncIteration
            self.emitted = True
            _, cap, finish = capture_control(x.runtime, req)
            await cap.clear()
            saved['receipt'] = finish()
            saved['producer'] = asyncio.current_task()
            return 'cleared'
        async def aclose(self):
            raise RuntimeError('synthetic original producer finally failure')
    with authenticated_scope(f.bob), boundary.delivery_scope():
        permit(x, req)
        stream = x.coordinator.run_stream(f.sid, req.request_id, SessionWorkKind.GOAL_CONTROL, ActualOperation)
        try:
            assert await asyncio.wait_for(anext(stream), 3) == 'cleared'
            producer = saved['producer']
            await asyncio.wait_for(asyncio.shield(asyncio.gather(producer, return_exceptions=True)), 3)
            owner, = x.coordinator._registry.select(session_id=f.sid, request_id=req.request_id)
            assert owner.state is SessionExecutionState.SUCCEEDED
            assert isinstance(producer.exception(), RuntimeError)
            with pytest.raises(GovernanceError):
                saved['receipt'].final_check()
        finally:
            await stream.aclose()
