from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.governance.model_consumer import NativeModelRequestAuthority, model_request_authority
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
