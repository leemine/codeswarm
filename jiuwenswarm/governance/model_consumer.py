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
from .tool_context import current_model_authorizer


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
    if configured_authenticator() is None and current_model_authorizer() is None:
        return None
    return NativeModelRequestAuthority(ModelCredentialBinding.from_config(config))
