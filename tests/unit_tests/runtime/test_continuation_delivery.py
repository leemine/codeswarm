"""Final queue checks use live original sidecar facts, never payload authority."""
import copy
import json
from dataclasses import replace

import pytest

from jiuwenswarm.governance.resources import ResourceDefinition
from jiuwenswarm.runtime.continuation_delivery import (
    capture_continuation_delivery, capture_continuation_options_delivery, continuation_options,
)
from tests.unit_tests.runtime import test_continuation_transaction as transactions

setup = transactions.setup
transaction = transactions.transaction
ALICE, BOB = transactions.ALICE, transactions.BOB


def params(tx):
    return {key: getattr(tx.request, key) for key in
            ('session_id', 'share_id', 'expected_revision', 'target_project_id')}


def resolver(tx):
    return lambda: tx.setup.identities[0]


def mutate(tx, change, session_id=None):
    if change == 'source':
        tx.setup.host.store.revoke(tx.request.share_id, ALICE, expected_revision=1)
    elif change == 'share_revision':
        tx.setup.host.store.revise(tx.request.share_id, ALICE, actions={'view', 'execute'},
            history=tx.setup.scope, expires_at=190, expected_revision=1)
    elif change == 'owner':
        tx.setup.host.invalidate_source(session_id, expected_epoch=1)
    elif change in {'target', 'target_regrant'}:
        tx.setup.access.replace_acl(tx.setup.target.project_id, 'admin', acl={'bob': ['read']}, expected_revision=2)
        if change == 'target_regrant':
            tx.setup.access.replace_acl(tx.setup.target.project_id, 'admin',
                acl={'bob': ['read', 'execute']}, expected_revision=3)
    elif change == 'resource':
        tx.setup.access.revoke_resource(tx.setup.target.project_id, BOB, 'model',
            subject_id=BOB.subject_id, expected_revision=4)
    elif change == 'resource_revision':
        tx.setup.access.register_resource(tx.setup.target.project_id,
            ResourceDefinition('another', 'tool', 'synthetic:other'), owner_subject_id=BOB.actor_id,
            actions=('invoke',), expected_revision=4)
    elif change == 'identity':
        tx.setup.identities[0] = replace(BOB, authority='other')
    elif change == 'expiry':
        tx.setup.clock[0] = 200
    elif change == 'catalog':
        tx.catalog['models']['defaults'][0]['model_client_config']['api_base'] = 'https://changed.example/v1'
    elif change == 'profile':
        tx.catalog['execution']['profiles']['native']['config_revision'] = 'other'
    else:
        raise AssertionError(change)


@pytest.mark.asyncio
async def test_successful_result_and_options_capture_return_none_checkers(transaction):
    tx = transaction
    options, check = continuation_options(tx.setup.host, resolver(tx), params(tx))
    assert check() is None
    assert options['options'] == [{'execution_profile_id': 'native', 'provider_id': 'native',
        'mode': 'agent.work.normal', 'model_name': 'synthetic#0', 'label': 'synthetic'}]
    assert capture_continuation_options_delivery(tx.setup.host, resolver(tx), params(tx), options)() is None
    result = await transactions.create(tx)
    assert capture_continuation_delivery(tx.setup.host, resolver(tx), tx.request, result.session_id)() is None
    assert tx.allocated == [result.session_id]


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['source', 'share_revision', 'owner', 'target', 'target_regrant',
    'resource', 'resource_revision', 'identity', 'expiry', 'catalog', 'profile'])
async def test_final_result_checker_rejects_queue_wait_mutation(transaction, change):
    tx = transaction
    result = await transactions.create(tx)
    check = capture_continuation_delivery(tx.setup.host, resolver(tx), tx.request, result.session_id)
    mutate(tx, change, result.session_id)
    with pytest.raises((PermissionError, ValueError)):
        check()


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['source', 'share_revision', 'target', 'target_regrant',
    'resource', 'resource_revision', 'identity', 'expiry', 'catalog', 'profile'])
async def test_final_options_checkers_reject_queue_wait_mutation(transaction, change):
    tx = transaction
    payload, helper = continuation_options(tx.setup.host, resolver(tx), params(tx))
    final = capture_continuation_options_delivery(tx.setup.host, resolver(tx), params(tx), payload)
    mutate(tx, change)
    for check in (helper, final):
        with pytest.raises((PermissionError, ValueError)):
            check()


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['source_id', 'other_private_id', 'token', 'title', 'project'])
async def test_result_fields_cannot_create_authority(transaction, change):
    tx = transaction
    result = await transactions.create(tx)
    request, sid = tx.request, result.session_id
    if change == 'source_id':
        sid = tx.request.session_id
    elif change == 'other_private_id':
        other = await transactions.create(tx, replace(tx.request, create_token='other-token'))
        sid = other.session_id
    elif change == 'token':
        request = replace(request, create_token='wrong-token')
    elif change == 'title':
        request = replace(request, title='changed')
    else:
        request = replace(request, target_project_id=tx.setup.source.project_id)
    with pytest.raises((PermissionError, ValueError)):
        capture_continuation_delivery(tx.setup.host, resolver(tx), request, sid)


@pytest.mark.asyncio
@pytest.mark.parametrize('extra', ['identity', 'user_id', 'create_token', 'execution_profile_id', 'model_name', 'cursor'])
async def test_options_reject_unknown_fields_before_side_effects(transaction, extra):
    tx = transaction
    with pytest.raises(ValueError):
        continuation_options(tx.setup.host, resolver(tx), {**params(tx), extra: 'forged'})
    assert tx.allocated == []


@pytest.mark.asyncio
async def test_options_are_only_authorized_intersection_and_no_connection_metadata(transaction):
    tx = transaction
    profiles = tx.catalog['execution']['profiles']
    profiles['external'] = {'provider_id': 'opencode', 'config_revision': 'e1', 'provider_config': {}}
    profiles['unsafe-native'] = {'provider_id': 'native', 'config_revision': 'e1', 'provider_config': {'arbitrary': True}}
    entry = copy.deepcopy(tx.catalog['models']['defaults'][0])
    entry['alias'] = 'Unavailable model'
    entry['model_client_config']['credential_reference'] = 'model-account:alice'
    entry['model_client_config']['api_key'] = 'SYNTHETIC-ALICE-SECRET'
    tx.catalog['models']['defaults'].append(entry)
    payload, check = continuation_options(tx.setup.host, resolver(tx), params(tx))
    assert check() is None and len(payload['options']) == 1
    assert set(payload) == set(params(tx)) | {'options'}
    assert set(payload['options'][0]) == {'execution_profile_id', 'provider_id', 'mode', 'model_name', 'label'}
    serialized = json.dumps(payload)
    for marker in ('SYNTHETIC', 'https://', 'model-account:', 'credential', 'api_key', 'Unavailable'):
        assert marker not in serialized
    assert tx.allocated == []


@pytest.mark.asyncio
async def test_unowned_target_and_cross_share_action_union_denied(transaction):
    tx = transaction
    with pytest.raises(PermissionError):
        continuation_options(tx.setup.host, resolver(tx), {**params(tx), 'target_project_id': tx.setup.source.project_id})
    revised = tx.setup.host.store.revise(tx.request.share_id, ALICE, actions={'view'},
        history=tx.setup.scope, expires_at=200, expected_revision=1)
    tx.setup.host.store.grant('source-session', ALICE, BOB, actions={'execute'},
        history=tx.setup.scope, expires_at=200)
    with pytest.raises(PermissionError):
        continuation_options(tx.setup.host, resolver(tx), {**params(tx), 'expected_revision': revised['revision']})


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['target_project_id', 'session_id', 'share_id', 'options'])
async def test_capture_options_rejects_payload_substitution(transaction, change):
    tx = transaction
    payload, _ = continuation_options(tx.setup.host, resolver(tx), params(tx))
    payload[change] = [] if change == 'options' else 'other'
    with pytest.raises(PermissionError):
        capture_continuation_options_delivery(tx.setup.host, resolver(tx), params(tx), payload)


@pytest.mark.asyncio
async def test_same_name_other_granted_reference_cannot_replace_approved_result(transaction):
    tx = transaction
    entry = copy.deepcopy(tx.catalog['models']['defaults'][0])
    entry['model_client_config']['credential_reference'] = 'model-account:second'
    tx.catalog['models']['defaults'].append(entry)
    tx.setup.access.register_resource(tx.setup.target.project_id,
        ResourceDefinition('second-model', 'credential', 'model-account:second'),
        owner_subject_id=BOB.actor_id, actions=('use',), expected_revision=4, delegable=True)
    tx.setup.access.grant_resource(tx.setup.target.project_id, replace(BOB, subject_id=BOB.actor_id),
        'second-model', subject_id=BOB.subject_id, actions=('use',), expected_revision=5)
    payload, _ = continuation_options(tx.setup.host, resolver(tx), params(tx))
    assert {o['model_name'] for o in payload['options']} == {'synthetic#0', 'synthetic#1'}
    result = await transactions.create(tx)
    with pytest.raises(PermissionError):
        capture_continuation_delivery(tx.setup.host, resolver(tx),
            replace(tx.request, model_name='synthetic#1'), result.session_id)
    # Even the original key cannot accept a catalog reorder to the other fully
    # granted credential reference at the identical model/endpoint.
    tx.catalog['models']['defaults'].reverse()
    with pytest.raises(PermissionError):
        capture_continuation_delivery(tx.setup.host, resolver(tx), tx.request, result.session_id)


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['profile_added', 'label', 'empty_becomes_available'])
async def test_final_options_inventory_equality_and_runtime_guard(transaction, change):
    tx = transaction
    if change == 'empty_becomes_available':
        profile = tx.catalog['execution']['profiles']['native']
        profile['provider_config'] = {'unsupported': True}
    await tx.runtime.start()
    runtime_result = await tx.runtime.continuation_options(params(tx))
    payload = runtime_result.to_payload()
    final = capture_continuation_options_delivery(tx.setup.host, resolver(tx), params(tx), payload)
    assert final() is None and runtime_result.revalidate() is None
    if change == 'profile_added':
        tx.catalog['execution']['profiles']['another-native'] = copy.deepcopy(
            tx.catalog['execution']['profiles']['native'])
    elif change == 'label':
        tx.catalog['models']['defaults'][0]['alias'] = 'Changed label'
    else:
        profile['provider_config'] = {}
    with pytest.raises(PermissionError):
        final()
    with pytest.raises(PermissionError):
        runtime_result.revalidate()
    assert tx.allocated == []


@pytest.mark.asyncio
async def test_runtime_options_payload_copy_cannot_mutate_cached_guard(transaction):
    tx = transaction
    await tx.runtime.start()
    result = await tx.runtime.continuation_options(params(tx))
    wire = result.to_payload()
    wire['options'].clear()
    wire['target_project_id'] = 'forged'
    assert len(result.to_payload()['options']) == 1
    assert result.to_payload()['target_project_id'] == tx.request.target_project_id
    assert result.revalidate() is None
    await tx.runtime.close()
    with pytest.raises(PermissionError):
        result.revalidate()


@pytest.mark.asyncio
@pytest.mark.parametrize('field,value', [('expected_revision', True), ('expected_revision', 0),
    ('target_project_id', {}), ('session_id', ''), ('share_id', ' spaced ')])
async def test_options_validates_value_types_and_normalization(transaction, field, value):
    tx = transaction
    with pytest.raises(ValueError):
        continuation_options(tx.setup.host, resolver(tx), {**params(tx), field: value})
    assert tx.allocated == []
