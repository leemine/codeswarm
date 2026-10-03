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
        request.model = 'other'
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
