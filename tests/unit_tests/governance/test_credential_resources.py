from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from jiuwenswarm.governance.contracts import TrustedIdentity
from jiuwenswarm.governance.credential_resources import BoundCredentialAuthority, CredentialUse
from jiuwenswarm.governance.resources import ResourceAccessDenied, ResourceDecision
from jiuwenswarm.governance.tool_resources import ResourceExecutionContext


@pytest.fixture
def bound():
    identity = TrustedIdentity('bob', 'bob', 'organization')
    state = SimpleNamespace(identity=identity, current=True, allowed=True, revision=1, reference='private:bob')
    use = CredentialUse('bob-model', 'private:bob', 'model', 'https://model.example/v1/chat/completions')
    resolver = SimpleNamespace(resolve_credential=Mock(return_value='bob-test-credential'))
    def authorize(project, actor, request):
        return ResourceDecision(state.allowed, project, actor.actor_id, actor.subject_id,
                                request, 1, state.revision, reference=state.reference)
    authority = BoundCredentialAuthority(
        ResourceExecutionContext('p', identity, 'bob-private', '/workspace', 'native'), uses=(use,),
        authorizer=SimpleNamespace(authorize_resource=authorize), resolver=resolver,
        current_identity=lambda: state.identity, is_current_execution=lambda: state.current,
    )
    return authority, use, state, resolver


@pytest.mark.asyncio
async def test_explicit_credential_use_rechecks_each_actual_request(bound):
    authority, use, state, resolver = bound
    assert await authority.resolve_for_request(use, destination=use.destination) == 'bob-test-credential'
    state.allowed = False
    with pytest.raises(ResourceAccessDenied):
        await authority.resolve_for_request(use, destination=use.destination)
    assert resolver.resolve_credential.call_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['identity', 'authority', 'subject', 'generation', 'reference', 'unknown', 'destination'])
async def test_denies_before_resolving_any_secret(bound, change):
    authority, use, state, resolver = bound
    destination = use.destination
    if change in {'identity', 'authority', 'subject'}:
        state.identity = {
            'identity': TrustedIdentity('alice', 'alice', 'organization'),
            'authority': TrustedIdentity('bob', 'bob', 'other'),
            'subject': TrustedIdentity('bob', 'alice', 'organization'),
        }[change]
    elif change == 'generation':
        state.current = False
    elif change == 'reference':
        state.reference = 'private:alice'
    elif change == 'unknown':
        use = replace(use, resource_id='alice-model', reference='private:alice')
    elif change == 'destination':
        destination += '/other'
    with pytest.raises(ResourceAccessDenied):
        await authority.resolve_for_request(use, destination=destination)
    resolver.resolve_credential.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['revoke', 'regrant', 'identity', 'generation'])
async def test_resolution_wait_cannot_deliver_stale_credential(bound, change):
    authority, use, state, resolver = bound
    async def resolve(_):
        if change == 'revoke':
            state.allowed = False
        elif change == 'regrant':
            state.revision += 2
        elif change == 'identity':
            state.identity = None
        else:
            state.current = False
        return 'must-not-be-delivered'
    resolver.resolve_credential = resolve
    with pytest.raises(ResourceAccessDenied):
        await authority.resolve_for_request(use, destination=use.destination)


@pytest.mark.asyncio
async def test_resolver_diagnostics_do_not_expose_secret(bound):
    import traceback
    authority, use, _, resolver = bound
    resolver.resolve_credential.side_effect = RuntimeError('secret-sentinel-error')
    with pytest.raises(ResourceAccessDenied) as error:
        await authority.resolve_for_request(use, destination=use.destination)
    assert 'secret-sentinel-error' not in ''.join(traceback.format_exception(error.value))


@pytest.mark.parametrize('destination', ['https://user:secret@model.example/v1', 'https://model.example/v1?token=x',
                                         'https://model.example/v1#x', 'file:///tmp/secret'])
def test_destination_cannot_contain_credential_or_ambient_file(destination):
    with pytest.raises(ValueError):
        CredentialUse('model', 'private:bob', 'model', destination)


@pytest.mark.asyncio
async def test_cancellation_keeps_semantics_without_resolver_secret(bound):
    import asyncio
    import traceback
    authority, use, _, resolver = bound
    resolver.resolve_credential.side_effect = asyncio.CancelledError('cancel-secret-sentinel')
    with pytest.raises(asyncio.CancelledError) as error:
        await authority.resolve_for_request(use, destination=use.destination)
    assert not str(error.value)
    assert 'cancel-secret-sentinel' not in ''.join(traceback.format_exception(error.value))


def test_secret_free_request_check_reuses_current_decision_without_resolving(bound):
    authority, use, state, resolver = bound
    before = authority.check_for_request(use, destination=use.destination)
    assert before.allowed and before.reference == use.reference
    resolver.resolve_credential.assert_not_called()
    state.revision += 1
    assert authority.check_for_request(use, destination=use.destination) != before
    state.allowed = False
    with pytest.raises(ResourceAccessDenied):
        authority.check_for_request(use, destination=use.destination)


@pytest.mark.parametrize('invalid', ['destination', 'use', 'identity'])
def test_secret_free_check_rejects_wrong_sink_or_original_identity(bound, invalid):
    authority, use, state, resolver = bound
    destination = use.destination
    if invalid == 'destination':
        destination += '/other'
    elif invalid == 'use':
        use = replace(use, reference='mcp:other')
    else:
        state.identity = None
    with pytest.raises(ResourceAccessDenied) as error:
        authority.check_for_request(use, destination=destination)
    assert error.value.__context__ is None
    resolver.resolve_credential.assert_not_called()
