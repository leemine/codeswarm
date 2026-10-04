from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from openjiuwen.harness.engine import ExecutionBinding
from openjiuwen.harness_protocol import AgentExecutionSpec
from jiuwenswarm.governance.contracts import TrustedIdentity
from jiuwenswarm.governance.model_credentials import ModelCredentialBinding
from jiuwenswarm.governance.opencode_model_credentials import OpenCodeModelCredentialAuthority
from jiuwenswarm.governance.resources import ResourceAccessDenied
from jiuwenswarm.governance.tool_context import (
    ExecutionResourceAuthorities, submitted_external_model_authorizer, tool_authority_scope,
)
from jiuwenswarm.governance.tool_resources import ResourceExecutionContext
from jiuwenswarm.runtime.harness.binding_store import BoundExecution
from tests.unit_tests.governance import test_opencode_model_http as http_cases

model = http_cases.model
request = http_cases.request


@pytest.fixture
def case(tmp_path):
    identity = TrustedIdentity('bob', 'bob-subject', 'organization')
    execution = ResourceExecutionContext('project', identity, 'private', str(tmp_path), 'opencode')
    spec = AgentExecutionSpec(provider_id='opencode', config_revision='fixture', provider_config={
        'model': {'model': 'same-model', 'api_base': 'https://model.invalid/v1'},
    })
    binding = ExecutionBinding.create(spec, subject_id=identity.subject_id,
                                      host_session_id='private', workspace=str(tmp_path))
    route = SimpleNamespace(provider_id='opencode', bound=BoundExecution(binding, spec),
                            trusted_subject_id=identity.subject_id)
    entries = [{'model_client_config': {
        'model_name': 'same-model', 'api_base': 'https://model.invalid/v1', 'client_provider': 'OpenAI',
        'credential_reference': 'model:' + actor, 'credential_encoding': 'plain',
        'api_key': 'MODEL_REQUEST_AUTHORITY',
    }, 'model_config_obj': {}} for actor in ('alice', 'bob')]
    live = [True]
    secret_source = Mock(side_effect=AssertionError('metadata selection must not read secrets'))
    factory = OpenCodeModelCredentialAuthority(execution, resource_authorizer=Mock(),
        current_identity=lambda: identity, is_current_execution=lambda: live[0],
        capture_binding=lambda selected: lambda: selected is binding, model_selection='same-model#1',
        metadata_source=lambda: deepcopy(entries), config_source=secret_source)
    return SimpleNamespace(**locals())


def test_exact_selection_keeps_bobs_reference_without_resolving_any_key(case):
    selected = case.factory(case.route)
    assert selected == ModelCredentialBinding.from_config(case.entries[1]['model_client_config'])
    assert selected.reference == 'model:bob'
    case.secret_source.assert_not_called()


@pytest.mark.parametrize('change', ['no_selection', 'name_alias', 'duplicate', 'wrong_model',
                                  'revoked', 'subject', 'session', 'workspace', 'trusted', 'static_key'])
def test_ambiguous_or_changed_original_scope_denied_before_secret_access(case, change, tmp_path):
    if change == 'no_selection':
        case.factory._selection = None
    elif change == 'name_alias':
        case.factory._selection = 'same-model'
    elif change == 'duplicate':
        case.entries.append(deepcopy(case.entries[1]))
    elif change == 'wrong_model':
        case.entries[1]['model_client_config']['model_name'] = 'changed-model'
    elif change == 'revoked':
        case.live[0] = False
    elif change == 'trusted':
        case.route.trusted_subject_id = 'alice'
    else:
        spec = case.spec
        if change == 'static_key':
            spec = AgentExecutionSpec(provider_id='opencode', config_revision='other', provider_config={
                'model': {'model': 'same-model', 'api_base': 'https://model.invalid/v1',
                          'api_key': 'synthetic-inline-must-not-reach-cli'},
            })
        binding = ExecutionBinding.create(spec,
            subject_id='alice' if change == 'subject' else case.identity.subject_id,
            host_session_id='other' if change == 'session' else 'private',
            workspace=str(tmp_path / 'other') if change == 'workspace' else str(tmp_path))
        case.route.bound = BoundExecution(binding, spec)
    with pytest.raises(ResourceAccessDenied, match='authority unavailable'):
        case.factory(case.route)
    case.secret_source.assert_not_called()


def test_original_continuation_binding_checker_runs_without_key_resolution(case):
    expected = case.factory(case.route)
    observed = []
    def check(binding, fingerprint):
        observed.append((binding, fingerprint))
        if binding != expected:
            raise PermissionError('changed original approval')
    case.factory._binding_checker = check
    assert case.factory(case.route) == expected
    case.entries[1]['model_client_config']['credential_reference'] = 'model:replacement'
    with pytest.raises(ResourceAccessDenied):
        case.factory(case.route)
    assert len(observed) == 2 and len(observed[0][1]) == 64
    case.secret_source.assert_not_called()


@pytest.mark.parametrize('cancel', [False, True])
def test_catalog_failure_context_and_cancellation_text_do_not_escape(case, cancel):
    import asyncio
    def fail():
        raise (asyncio.CancelledError if cancel else ValueError)('synthetic-secret')
    case.factory._metadata = fail
    with pytest.raises(asyncio.CancelledError if cancel else ResourceAccessDenied) as raised:
        case.factory(case.route)
    assert raised.value.__context__ is None
    assert 'synthetic-secret' not in str(raised.value)


def test_external_factory_scope_restores_exact_original_on_nested_exit(case):
    other = Mock()
    assert submitted_external_model_authorizer() is None
    with tool_authority_scope(None, provider_authorizers=ExecutionResourceAuthorities({}, external_model_authorizer=case.factory)):
        assert submitted_external_model_authorizer() is case.factory
        with tool_authority_scope(None, provider_authorizers=ExecutionResourceAuthorities({}, external_model_authorizer=other)):
            assert submitted_external_model_authorizer() is other
        assert submitted_external_model_authorizer() is case.factory
    assert submitted_external_model_authorizer() is None


def actual_factory(m):
    """Use the existing real ASGI/store fixture, with a host catalog resolver."""
    from dataclasses import asdict
    spec = AgentExecutionSpec(provider_id='opencode', config_revision='r1', provider_config={
        'model': asdict(m.harness._config.model),
    })
    binding = m.session.binding
    binding.validate_spec(spec)
    route = SimpleNamespace(provider_id='opencode', bound=BoundExecution(binding, spec),
                            trusted_subject_id=m.identity.subject_id)
    client = {'model_name': m.binding.model, 'api_base': m.binding.api_base,
              'credential_reference': m.binding.reference, 'credential_encoding': 'plain',
              'client_provider': 'OpenAI', 'api_key': 'MODEL_REQUEST_AUTHORITY'}
    metadata = [{'model_client_config': client, 'model_config_obj': {}}]
    source = Mock(return_value={'models': {'defaults': [{'model_client_config': {
        **client, 'api_key': 'actual-host-synthetic-key'}, 'model_config_obj': {}}]}})
    live = [True]
    factory = OpenCodeModelCredentialAuthority(m.authority.execution, resource_authorizer=m.store,
        current_identity=lambda: m.identity, is_current_execution=lambda: live[0],
        capture_binding=lambda original: lambda: original is binding, model_selection='fixture#0',
        metadata_source=lambda: deepcopy(metadata), config_source=source)
    return SimpleNamespace(**locals())


@pytest.mark.asyncio
async def test_catalog_factory_actual_asgi_http_sink_resolves_on_every_request(model):
    case = actual_factory(model)
    selected = case.factory(case.route)
    model.records['turn-one'] = case.factory(case.route, selected)
    case.source.assert_not_called()
    for _ in range(2):
        assert (await request(model)).status_code == 200
    assert case.source.call_count == 2
    assert all(req.headers['authorization'] == 'Bearer actual-host-synthetic-key'
               for req in model.state.requests)
    model.resolver.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['credential_revoked', 'catalog_changed', 'execution_revoked', 'binding_replaced'])
async def test_captured_http_record_rejects_changes_without_new_secret_lookup(model, change):
    case = actual_factory(model)
    selected = case.factory(case.route)
    model.records['turn-one'] = case.factory(case.route, selected)
    assert (await request(model)).status_code == 200
    if change == 'credential_revoked':
        model.store.revoke_resource(model.project, model.identity, 'credential',
                                   subject_id=model.identity.subject_id, expected_revision=1)
    elif change == 'catalog_changed':
        case.metadata[0]['model_config_obj']['temperature'] = 0.3
    elif change == 'execution_revoked':
        case.live[0] = False
    else:
        case.route.bound = BoundExecution(ExecutionBinding.create(case.spec,
            subject_id=model.identity.subject_id, host_session_id=model.session.binding.host_session_id,
            workspace=model.session.binding.workspace), case.spec)
    case.source.reset_mock()
    assert (await request(model)).status_code == 403
    case.source.assert_not_called()
    assert len(model.state.requests) == 1


@pytest.mark.asyncio
async def test_same_identity_original_credential_scope_revocation_does_not_revoke_other_scope(model):
    first, second = actual_factory(model), actual_factory(model)
    first_record = first.factory(first.route, first.factory(first.route))
    second_record = second.factory(second.route, second.factory(second.route))
    assert first_record.credential_authority.execution.identity == second_record.credential_authority.execution.identity
    first.live[0] = False
    with pytest.raises(ResourceAccessDenied):
        await first_record.credential_authority.resolve_for_request(first_record.use,
                                                                   destination=model.binding.destination)
    model.records['turn-one'] = second_record
    assert (await request(model)).status_code == 200
    first.source.assert_not_called()
    second.source.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['principal', 'owner', 'adapter', 'session'])
async def test_real_runtime_factory_http_uses_original_identity_context_not_asgi_ambient(model, monkeypatch, change):
    from contextvars import ContextVar
    from unittest.mock import AsyncMock
    from jiuwenswarm.common.schema.agent import AgentRequest
    from jiuwenswarm.common.schema.message import ReqMethod
    from jiuwenswarm.runtime import AgentRuntime
    from jiuwenswarm.runtime.service import _StoredResourceAuthority
    from jiuwenswarm.runtime.session.model import RuntimeSessionState, SessionExecutionState
    from jiuwenswarm.governance import opencode_model_credentials
    from jiuwenswarm.common import config

    case = actual_factory(model)
    principal = ContextVar('model_test_principal', default=None)
    original = SimpleNamespace(live=True, identity=model.identity)
    other = SimpleNamespace(live=True, identity=model.identity)
    foreign = SimpleNamespace(live=True, identity=TrustedIdentity('foreign', 'foreign', 'fixture-auth'))
    def resolve(_request):
        value = principal.get()
        return value.identity if value is not None and value.live else None
    owner = SimpleNamespace(_adapter=SimpleNamespace(execution_session=model.session))
    manager = SimpleNamespace(get_agent_for_session_nowait=lambda channel, sid: owner
                              if (channel, sid) == ('web', 'parent') else None)
    runtime = AgentRuntime(initializer=AsyncMock(), agent_manager=manager,
                           trusted_identity_resolver=resolve, resource_authorizer=_StoredResourceAuthority())
    runtime._started = True
    executions = tuple(SimpleNamespace(execution_id=rid, request_id=rid,
        state=SessionExecutionState.RUNNING, cancellation_requested=False) for rid in ('a', 'b'))
    snapshot = SimpleNamespace(generation=1, state=RuntimeSessionState.ACTIVE, executions=executions)
    monkeypatch.setattr(runtime, '_governance_project', lambda *_args, **_kwargs: model.project)
    monkeypatch.setattr(runtime._session_coordinator, 'snapshot_session', lambda _: snapshot)
    monkeypatch.setattr(opencode_model_credentials, 'configured_model_metadata', lambda: deepcopy(case.metadata))
    monkeypatch.setattr(config, 'get_config_raw', case.source)
    factories = []
    for rid, value in (('a', original), ('b', other)):
        token = principal.set(value)
        try:
            req = AgentRequest(rid, channel_id='web', session_id='parent', req_method=ReqMethod.CHAT_SEND,
                               params={'model_name': 'fixture#0'})
            factories.append(runtime._resource_authorizers_for(req).external_model_authorizer)
        finally:
            principal.reset(token)
    selected = factories[0](case.route)
    assert selected.reference == model.binding.reference
    assert factories[0]._capture_binding(case.binding)() is True
    first = factories[0](case.route, selected)
    second = factories[1](case.route, selected)
    token = principal.set(foreign)
    try:
        model.records['turn-one'] = first
        assert (await request(model)).status_code == 200
        if change == 'principal':
            original.live = False
        elif change == 'owner':
            owner = SimpleNamespace(_adapter=owner._adapter)
        elif change == 'adapter':
            owner._adapter = SimpleNamespace(execution_session=model.session)
        else:
            owner._adapter.execution_session = SimpleNamespace(
                binding=model.session.binding, closed=False, exit_state=model.session.exit_state)
        assert (await request(model)).status_code == 403
        model.records['turn-one'] = second
        assert (await request(model)).status_code == (200 if change == 'principal' else 403)
    finally:
        principal.reset(token)
    expected = 2 if change == 'principal' else 1
    assert len(model.state.requests) == expected
    assert case.source.call_count == expected


def test_cached_legacy_adapter_requires_gateway_before_governed_request(tmp_path):
    from jiuwenswarm.server.runtime.agent_adapter.agent_adapters import create_adapter
    from tests.unit_tests.runtime.harness.test_external_execution_route import _route
    route = _route(tmp_path, provider_id='opencode')
    with tool_authority_scope(None):
        adapter = create_adapter(execution_route=route)
    assert adapter._model_gateway_binding is None
    async def mandatory(_):
        return False
    with tool_authority_scope(None, provider_authorizers={'opencode': mandatory}):
        with pytest.raises(PermissionError):
            adapter._capture_model_authority()


def test_governed_team_rejects_missing_member_model_gateway_before_adapter_construction():
    from jiuwenswarm.server.runtime.agent_adapter.agent_adapters import create_adapter
    route = SimpleNamespace(provider_id='opencode',
                            surface=SimpleNamespace(identity=SimpleNamespace(topology='team')))
    async def mandatory(_):
        return False
    with tool_authority_scope(None, provider_authorizers={'opencode': mandatory}):
        with pytest.raises(ResourceAccessDenied, match='Team model gateway is not bound'):
            create_adapter(execution_route=route)
