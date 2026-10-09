"""Authenticated RSI inventory is independent of chat sharing and transport IDs."""
from dataclasses import asdict, replace
from types import SimpleNamespace

import pytest

from jiuwenswarm.agents.harness.common.rsi import build_rsi_service_context
from jiuwenswarm.agents.harness.common.rsi.models import RsiTask
from jiuwenswarm.governance.application_boundary import admit_application_request
from jiuwenswarm.governance.contracts import TrustedIdentity
from jiuwenswarm.governance.session_boundary import delivery_scope, set_delivery_permit
from jiuwenswarm.governance.session_sharing import SessionSharingDenied
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.rsi import RsiAgentServerHandlers

ALICE = TrustedIdentity('alice', 'alice', 'test:inventory')
BOB = TrustedIdentity('bob', 'bob', 'test:inventory')


@pytest.fixture
def inventory(tmp_path, monkeypatch):
    context = build_rsi_service_context(tmp_path)
    handlers = RsiAgentServerHandlers(context)
    for name, identity in [('alice', ALICE), ('bob', BOB), ('legacy', None),
                           ('other-authority', replace(ALICE, authority='other'))]:
        context.store.create(RsiTask(
            task_id='rsi-' + name, name=name, scenario='HARNESS', status='CREATED',
            created_at='2026-10-08', owner_identity=asdict(identity) if identity else None,
        ))
    monkeypatch.setattr('jiuwenswarm.governance.organization_auth.configured_authenticator', lambda: object())
    policy = {}
    monkeypatch.setattr('jiuwenswarm.governance.application_boundary.host_application_policy', lambda _: policy)
    return context, handlers, policy


def request():
    return SimpleNamespace(req_method=ReqMethod.RSI_TASK_LIST,
                           params={'session_id': 'rsi-browser-route'}, session_id='rsi-browser-route')


@pytest.mark.parametrize('identity,expected', [(ALICE, 'alice'), (BOB, 'bob')])
def test_both_users_list_only_their_records_without_a_chat_or_sharing_host(inventory, identity, expected):
    context, handlers, policy = inventory
    with delivery_scope():
        permit = admit_application_request('rsi.task.list', request().params,
            identity_resolver=lambda: identity, policy_supplier=lambda _: policy)
        set_delivery_permit(permit)
        result = handlers.handle(request())
    assert result['ok']
    assert [t['name'] for t in result['payload']['tasks']] == [expected]
    assert 'owner_identity' not in result['payload']['tasks'][0]
    # Status/config updates and a fresh store read preserve durable ownership.
    context.store.merge_config('rsi-' + expected, {'example': True})
    assert context.store.get('rsi-' + expected).owner_identity == asdict(identity)


def test_explicit_operator_can_see_legacy_but_not_another_users_data(inventory):
    _, handlers, policy = inventory
    policy['rsi'] = ['manage']
    with delivery_scope():
        permit = admit_application_request('rsi.task.list', {},
            identity_resolver=lambda: ALICE, policy_supplier=lambda _: policy)
        set_delivery_permit(permit)
        result = handlers.handle(request())
        assert {t['name'] for t in result['payload']['tasks']} == {'alice', 'legacy'}
        policy.clear()
        assert not permit.revalidate()
        assert not handlers.handle(request())['ok']


def test_direct_handler_cannot_bypass_request_admission(inventory):
    _, handlers, _ = inventory
    with delivery_scope():
        assert not handlers.handle(request())['ok']


@pytest.mark.parametrize('params', [{'owner_identity': asdict(BOB)}, {'user_id': 'bob'},
                                    {'include_legacy': True}, {'path': '/private'},
                                    {'task_id': 'rsi-bob'}])
def test_inventory_rejects_forged_audience(params):
    with pytest.raises(SessionSharingDenied):
        admit_application_request('rsi.task.list', params, identity_resolver=lambda: ALICE,
                                  policy_supplier=lambda _: {})


def test_identity_rotation_invalidates_original_delivery():
    current = [ALICE]
    permit = admit_application_request('rsi.task.list', {}, identity_resolver=lambda: current[0],
                                      policy_supplier=lambda _: {})
    current[0] = BOB
    assert not permit.revalidate()


def test_unauthenticated_legacy_instance_retains_inventory(inventory, monkeypatch):
    _, handlers, _ = inventory
    monkeypatch.setattr('jiuwenswarm.governance.organization_auth.configured_authenticator', lambda: None)
    assert len(handlers.handle(request())['payload']['tasks']) == 4


@pytest.mark.parametrize('method', ['rsi.task.get', 'rsi.report.get', 'rsi.usage.get',
                                    'rsi.tree.get', 'rsi.artifact.files.list',
                                    'rsi.artifact.files.get'])
def test_private_task_admission_and_final_delivery(inventory, method):
    context, _, policy = inventory
    params = {'task_id': 'rsi-alice', 'session_id': 'arbitrary-browser-route'}
    permit = admit_application_request(method, params, identity_resolver=lambda: ALICE,
                                      policy_supplier=lambda _: policy, rsi_store=context.store)
    assert permit.revalidate()
    for identity in [BOB, replace(ALICE, authority='different')]:
        with pytest.raises(SessionSharingDenied):
            admit_application_request(method, params, identity_resolver=lambda: identity,
                                      policy_supplier=lambda _: {'rsi': ['manage']}, rsi_store=context.store)
    # A buffered result loses its delivery authority if the original record
    # disappears or is replaced, even though login itself remains valid.
    task = context.store.get('rsi-alice')
    task.owner_identity = asdict(BOB)
    context.store.create(task)
    assert not permit.revalidate()


@pytest.mark.parametrize('task_id', ['../outside', '/tmp/outside', '.', '..', 'rsi-alice/task.json'])
def test_task_store_rejects_path_selectors(inventory, task_id):
    context, _, _ = inventory
    with pytest.raises(Exception):
        context.store.get(task_id)


def test_task_store_never_follows_foreign_task_symlink(inventory, tmp_path):
    context, _, _ = inventory
    target = tmp_path / 'rsi-alias'
    target.symlink_to(tmp_path / 'rsi-bob', target_is_directory=True)
    with pytest.raises(Exception):
        context.store.get('rsi-alias')
    assert 'rsi-alias' not in [t.task_id for t in context.store.list()]


def test_task_creation_cannot_bypass_application_admission(inventory):
    context, _, _ = inventory
    with delivery_scope(), pytest.raises(SessionSharingDenied):
        context.task_service.create({'owner_identity': asdict(ALICE)})
