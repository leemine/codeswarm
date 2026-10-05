"""Original persisted approval gates private continuation model consumption."""
import asyncio
import copy
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from jiuwenswarm.governance.model_consumer import NativeModelRequestAuthority
from jiuwenswarm.governance.model_credentials import (
    ConfiguredModelCredentialResolver, ModelCredentialBinding,
    NativeModelCredentialAuthority, model_entry_fingerprint,
)
from jiuwenswarm.governance.resources import ResourceAccessDenied, ResourceDefinition
from jiuwenswarm.governance.session_sharing import SessionSharingDenied
from jiuwenswarm.governance.tool_context import ExecutionResourceAuthorities, tool_authority_scope
from jiuwenswarm.governance.tool_resources import ResourceExecutionContext
from jiuwenswarm.runtime.continuation_execution import capture_continuation_execution, require_continuation_execution
from tests.unit_tests.runtime import test_continuation_transaction as transactions

setup = transactions.setup
transaction = transactions.transaction
BOB, ALICE = transactions.BOB, transactions.ALICE


async def capture(tx):
    result = await transactions.create(tx)
    request = SimpleNamespace(session_id=result.session_id, request_id='private-turn-1', params={})
    tx.owned_execution = tx.runtime.begin_detached_native_turn(
        result.session_id, 'synthetic-native-turn', request.request_id)
    return request, capture_continuation_execution(tx.runtime, request)


def target(binding):
    return SimpleNamespace(method='POST', url=binding.destination, model=binding.model,
        implementation='OpenAIModelClient', api_mode='chat_completions', operation='invoke')


def authority(tx, policy):
    execution = ResourceExecutionContext(tx.setup.target.project_id, BOB, policy.session_id,
        str(tx.setup.tmp_path / 'target'), 'native')
    native = object()
    result = NativeModelCredentialAuthority(execution, resource_authorizer=tx.setup.access,
        current_identity=lambda: tx.setup.identities[0], is_current_execution=lambda: True,
        owns_execution=lambda bound, session: bound is execution and session is native,
        config_source=lambda: tx.catalog, binding_checker=policy.check_model)
    return result, native


@pytest.mark.asyncio
async def test_capture_requires_original_persisted_approval(transaction):
    tx = transaction
    request, policy = await capture(tx)
    assert require_continuation_execution(policy, tx.runtime) is policy
    assert copy.deepcopy(policy) is policy
    assert policy.target.model_binding.reference == 'model-account:bob'
    assert policy.target.request.model_name == 'synthetic#0'
    assert 'private-turn-1' not in repr(policy)
    with pytest.raises(ResourceAccessDenied):
        require_continuation_execution(object(), tx.runtime)
    with pytest.raises(ResourceAccessDenied):
        require_continuation_execution(policy, object())
    request.request_id = 'different-turn'
    with pytest.raises(ResourceAccessDenied):
        policy.check()


@pytest.mark.asyncio
@pytest.mark.parametrize('model', ['synthetic', 'synthetic#1', 'other#0', {}, True])
async def test_request_cannot_choose_another_model(transaction, model):
    tx = transaction
    request, policy = await capture(tx)
    request.params['model_name'] = model
    with pytest.raises(ResourceAccessDenied):
        policy.check()
    with pytest.raises(ResourceAccessDenied):
        capture_continuation_execution(tx.runtime, request)


@pytest.mark.asyncio
async def test_both_refs_granted_same_endpoint_and_name_still_requires_original_binding(transaction):
    tx = transaction
    other_entry = copy.deepcopy(tx.catalog['models']['defaults'][0])
    other_entry['model_client_config']['credential_reference'] = 'model-account:other'
    other_entry['model_client_config']['api_key'] = 'SYNTHETIC-OTHER'
    tx.catalog['models']['defaults'].append(other_entry)
    tx.setup.access.register_resource(tx.setup.target.project_id,
        ResourceDefinition('other-model', 'credential', 'model-account:other'),
        owner_subject_id=BOB.actor_id, actions=('use',), expected_revision=4, delegable=True)
    tx.setup.access.grant_resource(tx.setup.target.project_id, replace(BOB, subject_id=BOB.actor_id),
        'other-model', subject_id=BOB.subject_id, actions=('use',), expected_revision=5)
    _, policy = await capture(tx)
    authorized, native = authority(tx, policy)
    original = policy.target.model_binding
    other = ModelCredentialBinding.from_config(other_entry['model_client_config'])
    assert original.model == other.model and original.destination == other.destination
    assert {r['reference'] for r in tx.setup.access.resource_grants(tx.setup.target.project_id, BOB)['resources']
            if r['kind'] == 'credential'} == {'model-account:bob', 'model-account:other'}
    assert await authorized(original, target(original), native_session=native,
        model_entry_fingerprint=policy.target.model_entry_fingerprint) == {
            'Authorization': 'Bearer SYNTHETIC-UNCONSUMED'}
    with pytest.raises(ResourceAccessDenied):
        await authorized(other, target(other), native_session=native,
            model_entry_fingerprint=model_entry_fingerprint(other_entry['model_client_config'], other_entry['model_config_obj']))
    with pytest.raises(ResourceAccessDenied):
        await authorized(original, target(original), native_session=native, model_entry_fingerprint='b' * 64)
    with pytest.raises(ResourceAccessDenied):
        await authorized(original, target(original), native_session=native)


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['endpoint', 'reference', 'request', 'profile', 'source_revoke',
                                   'source_regrant', 'target', 'identity', 'resource', 'closed'])
async def test_old_policy_and_new_capture_reject_drift(transaction, change):
    tx = transaction
    request, policy = await capture(tx)
    client = tx.catalog['models']['defaults'][0]['model_client_config']
    if change == 'endpoint':
        client['api_base'] = 'https://changed.example/v1'
    elif change == 'reference':
        client['credential_reference'] = 'model-account:other'
    elif change == 'request':
        tx.catalog['models']['defaults'][0]['model_config_obj']['temperature'] = 0.9
    elif change == 'profile':
        tx.catalog['execution']['profiles']['native']['config_revision'] = 'changed'
    elif change.startswith('source'):
        tx.setup.host.store.revoke(tx.request.share_id, ALICE, expected_revision=1)
        if change == 'source_regrant':
            tx.setup.host.store.grant('source-session', ALICE, BOB, actions={'view', 'execute'},
                history=tx.setup.scope, expires_at=200)
    elif change == 'target':
        tx.setup.access.replace_acl(tx.setup.target.project_id, 'admin', acl={}, expected_revision=2)
    elif change == 'identity':
        tx.setup.identities[0] = replace(BOB, authority='other')
    elif change == 'resource':
        tx.setup.access.revoke_resource(tx.setup.target.project_id, BOB, 'model',
            subject_id=BOB.subject_id, expected_revision=4)
    else:
        await tx.runtime.close()
    with pytest.raises((PermissionError, ValueError)):
        policy.check()
    with pytest.raises((PermissionError, ValueError)):
        capture_continuation_execution(tx.runtime, request)


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['source', 'identity', 'selection', 'entry'])
async def test_credential_await_rechecks_original_policy_before_return(transaction, monkeypatch, change):
    tx = transaction
    request, policy = await capture(tx)
    authorized, native = authority(tx, policy)
    started, release = asyncio.Event(), asyncio.Event()
    async def delayed(self, reference):
        started.set()
        await release.wait()
        return 'SYNTHETIC-DELAYED'
    monkeypatch.setattr(ConfiguredModelCredentialResolver, 'resolve_credential', delayed)
    binding = policy.target.model_binding
    pending = asyncio.create_task(authorized(binding, target(binding), native_session=native,
        model_entry_fingerprint=policy.target.model_entry_fingerprint))
    await started.wait()
    if change == 'source':
        tx.setup.host.store.revoke(tx.request.share_id, ALICE, expected_revision=1)
    elif change == 'identity':
        tx.setup.identities[0] = replace(BOB, subject_id='other')
    elif change == 'selection':
        request.params['model_name'] = 'other#0'
    else:
        tx.catalog['models']['defaults'][0]['model_config_obj']['temperature'] = 0.9
    release.set()
    with pytest.raises(ResourceAccessDenied) as caught:
        await pending
    assert 'SYNTHETIC-DELAYED' not in str(caught.value)


@pytest.mark.asyncio
async def test_factory_threads_original_entry_hash_and_does_not_borrow_new_callback(transaction):
    tx = transaction
    _, policy = await capture(tx)
    authorized, native = authority(tx, policy)
    binding = policy.target.model_binding
    async def original(bound, actual, **kwargs):
        return await authorized(bound, actual, native_session=native, **kwargs)
    factory = NativeModelRequestAuthority(binding, policy.target.model_entry_fingerprint)
    with tool_authority_scope(None, provider_authorizers=ExecutionResourceAuthorities({}, original)):
        call = factory.bind_for_call()
        assert await call(target(binding)) == {'Authorization': 'Bearer SYNTHETIC-UNCONSUMED'}
    with tool_authority_scope(None, provider_authorizers=ExecutionResourceAuthorities({}, lambda *a, **k: None)):
        with pytest.raises(ResourceAccessDenied):
            await call(target(binding))


@pytest.mark.asyncio
async def test_actual_entry_builder_keeps_original_hash_without_legacy_lookup(transaction, monkeypatch):
    from jiuwenswarm.server.runtime.agent_adapter import interface_deep
    tx = transaction
    _, policy = await capture(tx)
    constructed = Mock(side_effect=lambda **kwargs: SimpleNamespace(**kwargs))
    monkeypatch.setattr(interface_deep, 'Model', constructed)
    monkeypatch.setattr(interface_deep, 'get_available_models',
        Mock(side_effect=AssertionError('legacy catalog lookup forbidden')))
    # Enter the normal host model authority selection; Model itself is replaced
    # before construction, so no SDK client/network/process is created.
    with tool_authority_scope(None, provider_authorizers=ExecutionResourceAuthorities({}, lambda *a, **k: None)):
        model = policy.build_model()
    assert constructed.call_count == 1
    assert model.request_authority.binding == policy.target.model_binding
    assert model.request_authority.model_entry_fingerprint == policy.target.model_entry_fingerprint
    assert model.model_client_config.api_key == 'MODEL_REQUEST_AUTHORITY'
    assert 'SYNTHETIC-UNCONSUMED' not in repr(constructed.call_args)


@pytest.mark.asyncio
async def test_actual_entry_builder_rechecks_after_construction(transaction, monkeypatch):
    from jiuwenswarm.server.runtime.agent_adapter import interface_deep
    tx = transaction
    _, policy = await capture(tx)
    def revoke(**kwargs):
        tx.setup.host.store.revoke(tx.request.share_id, ALICE, expected_revision=1)
        return SimpleNamespace(**kwargs)
    monkeypatch.setattr(interface_deep, 'Model', revoke)
    with tool_authority_scope(None, provider_authorizers=ExecutionResourceAuthorities({}, lambda *a, **k: None)):
        with pytest.raises(SessionSharingDenied):
            policy.build_model()


@pytest.mark.asyncio
async def test_restarted_runtime_cannot_reapprove_changed_model_entry(transaction):
    from unittest.mock import AsyncMock
    from jiuwenswarm.runtime import AgentRuntime
    tx = transaction
    request, _ = await capture(tx)
    await tx.runtime.close()
    tx.catalog['models']['defaults'][0]['model_config_obj']['temperature'] = 0.8
    runtime = AgentRuntime(agent_manager=tx.manager, initializer=AsyncMock(),
        plan_controller=SimpleNamespace(reset_session=lambda _: None),
        trusted_identity_resolver=lambda _: tx.setup.identities[0],
        project_authorizer=tx.setup.access, resource_authorizer=tx.setup.access)
    await runtime.start()
    try:
        with pytest.raises(ResourceAccessDenied):
            capture_continuation_execution(runtime, request)
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_adapter_resolves_original_policy_before_login_cache_or_name_fallback(transaction, monkeypatch):
    from jiuwenswarm.server.runtime.agent_adapter import interface_deep
    tx = transaction
    request, policy = await capture(tx)
    request._continuation_execution = policy
    adapter = object.__new__(interface_deep.JiuWenSwarmDeepAdapter)
    adapter._requested_model_name = Mock(side_effect=AssertionError('legacy model lookup forbidden'))
    adapter._request_scoped_login_model = Mock(side_effect=AssertionError('login fallback forbidden'))
    monkeypatch.setattr(interface_deep, 'Model', lambda **kwargs: SimpleNamespace(**kwargs))
    with tool_authority_scope(None, provider_authorizers=ExecutionResourceAuthorities({}, lambda *a, **k: None)):
        model = adapter._resolve_model_for_request(request)
    assert model.request_authority.binding == policy.target.model_binding
    other = SimpleNamespace(session_id='unrelated-session', request_id=request.request_id,
                            params={}, _continuation_execution=policy)
    with pytest.raises(ResourceAccessDenied):
        adapter._resolve_model_for_request(other)


async def end_original_turn(tx, request, change):
    from openjiuwen.harness_protocol import TurnEventKind
    coordinator = tx.runtime._session_coordinator
    execution = tx.owned_execution
    if change == 'cancel_requested':
        owner = coordinator.external_execution_owner(request.session_id, request.request_id)
        await coordinator.request_external_execution_cancel(owner)
        snapshot = coordinator.get_execution(execution.execution_id)
        assert snapshot.cancellation_requested and not snapshot.state.terminal
    elif change == 'cancel':
        result = await coordinator.cancel_execution(request.session_id,
            execution_id=execution.execution_id, generation=execution.generation)
        assert result.cancelled == 1
    elif change == 'terminal':
        assert tx.runtime.finish_detached_native_turn(
            request.session_id, execution.execution_id, TurnEventKind.FINISHED)
    elif change == 'generation':
        await coordinator.close_session(request.session_id, generation=execution.generation)
        new_session = await coordinator.register_session(request.session_id, 'web')
        assert new_session.generation != execution.generation
        replacement = tx.runtime.begin_detached_native_turn(
            request.session_id, 'later-turn', request.request_id)
        assert replacement.execution_id != execution.execution_id
    else:
        raise AssertionError(change)


@pytest.mark.asyncio
async def test_capture_requires_actual_coordinator_owned_admission(transaction):
    tx = transaction
    result = await transactions.create(tx)
    request = SimpleNamespace(session_id=result.session_id, request_id='never-admitted', params={})
    with pytest.raises(ResourceAccessDenied):
        capture_continuation_execution(tx.runtime, request)


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['cancel_requested', 'cancel', 'terminal', 'generation'])
async def test_old_execution_checker_cannot_borrow_later_admission(transaction, change):
    tx = transaction
    request, policy = await capture(tx)
    assert tx.runtime._session_coordinator.get_execution(tx.owned_execution.execution_id).state.value == 'running'
    await end_original_turn(tx, request, change)
    with pytest.raises(ResourceAccessDenied):
        policy.check()


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['cancel_requested', 'cancel', 'terminal', 'generation'])
async def test_seed_read_await_cannot_publish_context_after_admission_ends(transaction, monkeypatch, change):
    import threading
    from jiuwenswarm.runtime import continuation_execution
    tx = transaction
    request, policy = await capture(tx)
    original = continuation_execution.read_seed
    entered, release = threading.Event(), threading.Event()
    def delayed(*args):
        seed = original(*args)
        entered.set()
        if not release.wait(5):
            raise RuntimeError('test seed barrier timed out')
        return seed
    monkeypatch.setattr(continuation_execution, 'read_seed', delayed)
    pending = asyncio.create_task(policy.make_context())
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        await end_original_turn(tx, request, change)
    finally:
        release.set()
    with pytest.raises(ResourceAccessDenied):
        await pending


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['cancel_requested', 'cancel', 'terminal', 'generation'])
async def test_late_warmup_clears_installed_context_when_original_turn_ends(transaction, change):
    from unittest.mock import AsyncMock
    from jiuwenswarm.agents.harness.common import session_ops_service
    from jiuwenswarm.governance.continuation_context import ContinuationContextDenied
    tx = transaction
    request, policy = await capture(tx)
    handle = await policy.make_context()
    entered, release = asyncio.Event(), asyncio.Event()
    pool = []
    async def create(**kwargs):
        pool.append(kwargs['history_messages'])
        entered.set()
        await release.wait()
    async def clear(**kwargs):
        pool.clear()
    engine = SimpleNamespace(get_context=lambda **_: pool[0] if pool else None,
        create_context=AsyncMock(side_effect=create), clear_context=AsyncMock(side_effect=clear))
    deep = SimpleNamespace(_loop_session=SimpleNamespace(get_session_id=lambda: request.session_id),
        react_agent=SimpleNamespace(context_engine=engine, _config=SimpleNamespace(context_processors=[])))
    pending = asyncio.create_task(session_ops_service.warmup_session_context(
        deep_agent=deep, session_id=request.session_id, history_before_request_id=request.request_id,
        continuation_context=handle))
    await entered.wait()
    assert pool
    try:
        await end_original_turn(tx, request, change)
    finally:
        release.set()
    with pytest.raises(ContinuationContextDenied):
        await pending
    assert pool == []
    engine.clear_context.assert_awaited_once_with(context_id='default_context_id', session_id=request.session_id)


@pytest.mark.asyncio
async def test_same_execution_waiting_for_control_retains_original_policy(transaction):
    tx = transaction
    request, policy = await capture(tx)
    assert await tx.runtime.observe_detached_native_turn(request.session_id,
        tx.owned_execution.execution_id, {'event_type': 'chat.ask_user_question', 'request_id': 'question-1'})
    assert tx.runtime._session_coordinator.get_execution(tx.owned_execution.execution_id).state.value == 'waiting_for_control'
    assert policy.check() is None
    assert await policy.make_context() is not None
