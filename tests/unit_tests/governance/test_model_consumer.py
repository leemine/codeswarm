from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.governance.model_consumer import NativeModelRequestAuthority, model_request_authority
from jiuwenswarm.governance.model_consumer import runtime_model_kwargs
from jiuwenswarm.governance.model_credentials import ModelCredentialBinding
from jiuwenswarm.governance.resources import ResourceAccessDenied
from jiuwenswarm.governance.tool_context import ExecutionResourceAuthorities, tool_authority_scope


@pytest.mark.asyncio
async def test_logical_call_never_uses_later_runtime_credential_callback():
    binding = ModelCredentialBinding('model', 'https://model.example/v1')
    factory = NativeModelRequestAuthority(binding)
    first = AsyncMock(return_value={'Authorization': 'Bearer first'})
    second = AsyncMock(return_value={'Authorization': 'Bearer second'})
    with tool_authority_scope(None, provider_authorizers=ExecutionResourceAuthorities({}, first)):
        call = factory.bind_for_call()
        assert await call(SimpleNamespace()) == {'Authorization': 'Bearer first'}
    with tool_authority_scope(None, provider_authorizers=ExecutionResourceAuthorities({}, second)):
        with pytest.raises(ResourceAccessDenied):
            await call(SimpleNamespace())
        second.assert_not_awaited()
        new_call = factory.bind_for_call()
        assert await new_call(SimpleNamespace()) == {'Authorization': 'Bearer second'}
    with pytest.raises(ResourceAccessDenied):
        await new_call(SimpleNamespace())


def test_factory_requires_host_scope_without_falling_back_to_config_key():
    with pytest.raises(ResourceAccessDenied):
        NativeModelRequestAuthority(ModelCredentialBinding('model', 'https://model.example/v1')).bind_for_call()


def test_explicit_resource_scope_without_model_callback_cannot_become_legacy(monkeypatch):
    import jiuwenswarm.governance.organization_auth as auth
    monkeypatch.setattr(auth, 'configured_authenticator', lambda: None)
    with tool_authority_scope(None, provider_authorizers={}):
        with pytest.raises(ResourceAccessDenied):
            model_request_authority(None)


@pytest.mark.parametrize('base', ['https://model.example/v1/chat/completions/',
                                 'https://MODEL.example:443/v1'])
def test_model_destination_matches_actual_core_normalization(base):
    import httpx
    from openjiuwen.core.foundation.llm.model_clients.openai_model_client import _chat_completions_url
    binding = ModelCredentialBinding('model', base)
    assert binding.destination == str(httpx.URL(_chat_completions_url(base)))
    assert binding == ModelCredentialBinding('model', 'https://model.example/v1')


def test_legacy_factory_preserves_unbound_behavior(monkeypatch):
    import jiuwenswarm.governance.organization_auth as auth
    monkeypatch.setattr(auth, 'configured_authenticator', lambda: None)
    assert model_request_authority({'api_key': 'legacy-only'}) is None


def test_organization_factory_cannot_opt_out_with_model_fields(monkeypatch):
    import jiuwenswarm.governance.organization_auth as auth
    monkeypatch.setattr(auth, 'configured_authenticator', lambda: object())
    with pytest.raises(ValueError):
        model_request_authority({'api_key': 'host-default', 'model_name': 'model',
                                 'api_base': 'https://model.example/v1', 'request_authority': None})


def test_runtime_factory_copies_config_without_retaining_shared_secret(monkeypatch):
    from openjiuwen.core.foundation.llm import ModelClientConfig, ModelRequestConfig
    import jiuwenswarm.governance.organization_auth as auth
    monkeypatch.setattr(auth, 'configured_authenticator', lambda: object())
    raw = dict(model_name='model', api_base='https://model.example/v1',
               client_provider='OpenAI', api_key='synthetic-host-secret',
               credential_reference='model:alice', credential_encoding='plain')
    client = ModelClientConfig(**raw)
    request = ModelRequestConfig(model='model')
    kwargs = runtime_model_kwargs(client, request, binding_config=raw)
    assert client.api_key == raw['api_key']
    assert kwargs['model_client_config'].api_key == 'MODEL_REQUEST_AUTHORITY'
    assert kwargs['model_config'] is request
    assert kwargs['request_authority'].binding.reference == 'model:alice'
    assert 'synthetic-host-secret' not in repr(kwargs)


@pytest.mark.parametrize('change', ['endpoint', 'model', 'headers', 'no_metadata'])
def test_runtime_factory_denies_mismatched_or_missing_metadata(monkeypatch, change):
    from openjiuwen.core.foundation.llm import ModelClientConfig, ModelRequestConfig
    import jiuwenswarm.governance.organization_auth as auth
    monkeypatch.setattr(auth, 'configured_authenticator', lambda: object())
    raw = dict(model_name='model', api_base='https://model.example/v1',
               client_provider='OpenAI', api_key='synthetic', credential_encoding='plain')
    client = ModelClientConfig(**raw)
    request = ModelRequestConfig(model='model')
    if change == 'endpoint':
        client.api_base = 'https://other.example/v1'
    elif change == 'model':
        request = ModelRequestConfig(model='other')
    elif change == 'headers':
        client.custom_headers = {'Authorization': 'Bearer other'}
    else:
        raw = None
    with pytest.raises(ResourceAccessDenied):
        runtime_model_kwargs(client, request, binding_config=raw)


def test_runtime_factory_legacy_preserves_config_and_constructor_shape(monkeypatch):
    import jiuwenswarm.governance.organization_auth as auth
    monkeypatch.setattr(auth, 'configured_authenticator', lambda: None)
    original = object()
    assert runtime_model_kwargs(original) == {'model_client_config': original}


def test_organization_catalog_never_decrypts_or_resolves_login_environment(monkeypatch):
    import jiuwenswarm.common.config as config
    import jiuwenswarm.governance.organization_auth as auth
    monkeypatch.setattr(auth, 'configured_authenticator', lambda: object())
    raw = {'models': {'defaults': [{
        'model_client_config': {
            'model_name': 'model', 'api_base': 'https://model.example/v1',
            'api_key': '${PRIVATE_KEY}', 'client_provider': 'OpenAI',
            'credential_reference': 'model:alice', 'credential_encoding': 'host_crypto',
            'custom_headers': {'Authorization': 'Bearer synthetic-header-secret'},
        }, 'model_config_obj': {},
    }], 'agentos': [{'model_client_config': {'model_name': 'ambient'}}]}}
    monkeypatch.setattr(config, 'get_config_raw', lambda: raw)
    def forbidden(*args, **kwargs):
        raise AssertionError('metadata must not resolve credentials')
    monkeypatch.setattr(config, 'get_config', forbidden)
    monkeypatch.setattr(config, '_decrypt_model_entries', forbidden)
    monkeypatch.setattr(config, 'get_agentos_models', forbidden)
    monkeypatch.setenv('API_KEY', 'ambient-synthetic-key')
    entries = config.get_available_models(session_id='unrelated-login')
    assert len(entries) == 1
    client = entries[0]['model_client_config']
    assert client['credential_reference'] == 'model:alice'
    assert client['credential_encoding'] == 'host_crypto'
    assert client['api_key'] == 'MODEL_REQUEST_AUTHORITY'
    assert client['custom_headers']  # Unsupported transport stays denied.
    assert 'synthetic-header-secret' not in repr(entries)
    assert 'PRIVATE_KEY' not in repr(entries)
    assert raw['models']['defaults'][0]['model_client_config']['api_key'] == '${PRIVATE_KEY}'


@pytest.mark.asyncio
async def test_original_entry_fingerprint_reaches_same_http_authority():
    binding = ModelCredentialBinding('model', 'https://model.example/v1')
    fingerprint = 'e' * 64
    authority = AsyncMock(return_value={'Authorization': 'Bearer synthetic'})
    with tool_authority_scope(None, provider_authorizers=ExecutionResourceAuthorities({}, authority)):
        request = object()
        call = NativeModelRequestAuthority(binding, fingerprint).bind_for_call()
        await call(request)
        authority.assert_awaited_once_with(binding, request, model_entry_fingerprint=fingerprint)


def test_entry_fingerprint_ignores_key_rotation_but_pins_destination_reference_and_settings():
    from jiuwenswarm.governance.model_credentials import model_entry_fingerprint
    client = {'model_name': 'model', 'api_base': 'https://model.example/v1',
              'credential_reference': 'account:bob', 'api_key': 'synthetic-old'}
    original = model_entry_fingerprint(client, {'temperature': 0.1})
    assert model_entry_fingerprint({**client, 'api_key': 'synthetic-new'}, {'temperature': 0.1}) == original
    for change in ({'api_base': 'https://other.example/v1'}, {'credential_reference': 'account:alice'}):
        assert model_entry_fingerprint({**client, **change}, {'temperature': 0.1}) != original
    assert model_entry_fingerprint(client, {'temperature': 0.9}) != original
    with pytest.raises(ResourceAccessDenied):
        model_entry_fingerprint(client, {'custom_headers': {'Authorization': 'synthetic'}})
