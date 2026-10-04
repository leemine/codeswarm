"""The existing claim lock isolates complete trusted identities and inputs."""
import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest

from jiuwenswarm.governance.contracts import TrustedIdentity
from jiuwenswarm.governance.session_claim import session_create_claim_scope, current_session_create_claim
from jiuwenswarm.runtime.session_provisioner import SessionCreateInput
from jiuwenswarm.server.runtime.agent_manager import AgentManager
from jiuwenswarm.server.runtime.agent_warm_pool import AgentWarmPool


ALICE = TrustedIdentity('alice', 'alice-login', 'host')
PARAMS = dict(channel_id='web', project_id='p', project_dir='', work_mode='work',
              is_swarm=False, prewarm_eligible=True, create_token='same')
INPUT = SessionCreateInput(channel_id='web', project_id='p', create_token='same')


def manager():
    state = SimpleNamespace(count=0)
    async def claim(key):
        state.count += 1
        return object()
    result = object.__new__(AgentManager)
    result.warm_pool = SimpleNamespace(make_key=AgentWarmPool.make_key, claim=claim)
    result._session_create_token_lock = asyncio.Lock()
    result._session_create_tokens = {}
    return result, state


@pytest.mark.asyncio
@pytest.mark.parametrize('identity', [TrustedIdentity('bob', 'bob-login', 'host'),
    TrustedIdentity('alice', 'other-login', 'host'), TrustedIdentity('alice', 'alice-login', 'other-host')])
async def test_same_token_isolated_by_entire_trusted_identity(identity):
    host, state = manager()
    with session_create_claim_scope(lambda: ALICE, INPUT):
        first = await host.claim_prewarmed_session(**PARAMS)
        assert await host.claim_prewarmed_session(**PARAMS) is first
    with session_create_claim_scope(lambda: identity, INPUT):
        second = await host.claim_prewarmed_session(**PARAMS)
        assert await host.claim_prewarmed_session(**PARAMS) is second
    assert first is not second and state.count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('changed', [dict(title='changed'), dict(model_name='other'),
    dict(user_id='wire-is-data'), dict(execution_profile_id='other'), dict(mode='code')])
async def test_same_identity_and_token_cannot_change_any_normalized_input(changed):
    host, state = manager()
    with session_create_claim_scope(lambda: ALICE, INPUT):
        await host.claim_prewarmed_session(**PARAMS)
    with session_create_claim_scope(lambda: ALICE, replace(INPUT, **changed)):
        with pytest.raises(ValueError, match='different session parameters'):
            await host.claim_prewarmed_session(**PARAMS)
    assert state.count == 1


@pytest.mark.asyncio
async def test_revoked_identity_while_waiting_for_existing_lock_cannot_claim():
    host, state = manager()
    await host._session_create_token_lock.acquire()
    actor = [ALICE]
    ready = asyncio.Event()
    async def create():
        with session_create_claim_scope(lambda: actor[0], INPUT):
            ready.set()
            return await host.claim_prewarmed_session(**PARAMS)
    pending = asyncio.create_task(create())
    await ready.wait()
    actor[0] = None
    host._session_create_token_lock.release()
    with pytest.raises(PermissionError):
        await pending
    assert state.count == 0 and not host._session_create_tokens


@pytest.mark.asyncio
async def test_inherited_or_closed_scope_cannot_become_legacy_claim():
    host, state = manager()
    gate = asyncio.Event()
    async def inherited():
        with pytest.raises(PermissionError):
            await host.claim_prewarmed_session(**PARAMS)
        await gate.wait()
        with pytest.raises(PermissionError):
            await host.claim_prewarmed_session(**PARAMS)
    with session_create_claim_scope(lambda: ALICE, INPUT):
        pending = asyncio.create_task(inherited())
        await asyncio.sleep(0)
    gate.set()
    await pending
    assert current_session_create_claim() is None
    assert state.count == 0 and not host._session_create_tokens
