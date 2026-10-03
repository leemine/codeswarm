"""Bind a governed Model call to its original Native host request.

The core transport owns final HTTP request construction and retry checks. This
host factory supplies the current exact-owner credential callback once per
logical invocation; it cannot borrow a newer Turn's callback after an await.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

from .model_credentials import ModelCredentialBinding
from .resources import ResourceAccessDenied
from .tool_context import current_model_authorizer, current_tool_authorizer


@dataclass(frozen=True, slots=True)
class NativeModelRequestAuthority:
    binding: ModelCredentialBinding

    def bind_for_call(self):
        authority = current_model_authorizer()
        if not callable(authority):
            raise ResourceAccessDenied('model request authority unavailable')

        async def authorize(target):
            try:
                if current_model_authorizer() is not authority:
                    raise ResourceAccessDenied('model request authority changed')
                headers = await authority(self.binding, target)
                if current_model_authorizer() is not authority:
                    raise ResourceAccessDenied('model request authority changed')
                return headers
            except asyncio.CancelledError:
                raise asyncio.CancelledError() from None
            except Exception:
                raise ResourceAccessDenied('model credential consumption denied') from None
        return authorize


def model_request_authority(config):
    """Select mandatory governance only from host state, never a wire flag."""
    from .organization_auth import configured_authenticator
    if (configured_authenticator() is None and current_model_authorizer() is None
            and current_tool_authorizer() is None):
        return None
    return NativeModelRequestAuthority(ModelCredentialBinding.from_config(config))


def runtime_model_kwargs(model_client_config, model_config=None, *, binding_config=None):
    """Keep model factories on the same mandatory host request boundary.

    The explicit host catalog metadata must survive until this point. A typed
    client config or a literal API key alone is not a credential grant.
    """
    authority = model_request_authority(binding_config)
    result = {'model_client_config': model_client_config}
    if model_config is not None:
        result['model_config'] = model_config
    if authority is None:
        return result
    try:
        actual = model_client_config.model_dump(mode='json')
        actual.update(model_name=authority.binding.model,
                      credential_reference=authority.binding.credential_reference,
                      credential_encoding=authority.binding.credential_encoding)
        if ModelCredentialBinding.from_config(actual) != authority.binding:
            raise ResourceAccessDenied('model factory binding mismatch')
        configured_name = getattr(model_config, 'model_name', None)
        if configured_name is not None and configured_name != authority.binding.model:
            raise ResourceAccessDenied('model factory model mismatch')
        result['model_client_config'] = model_client_config.model_copy(
            update={'api_key': 'MODEL_REQUEST_AUTHORITY'})
    except Exception:
        raise ResourceAccessDenied('model factory binding unavailable') from None
    result['request_authority'] = authority
    return result
