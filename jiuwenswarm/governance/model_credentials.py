"""Explicit host catalog credentials for Native model HTTP consumers.

Catalog presence is not authorization. The host must register the opaque
reference and grant credential/use in the existing project resource store.
This adapter never selects a credential from request headers or model inputs.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from typing import Callable

from .credential_resources import BoundCredentialAuthority, CredentialUse
from .resources import ResourceAccessDenied, ResourceDefinition
from .tool_resources import ResourceExecutionContext


@dataclass(frozen=True, slots=True)
class ModelCredentialBinding:
    model: str
    api_base: str
    implementation: str = 'OpenAI'
    credential_reference: str | None = None
    credential_encoding: str = 'plain'

    def __post_init__(self):
        if (not isinstance(self.model, str) or not self.model or self.model.strip() != self.model
                or self.implementation != 'OpenAI' or not isinstance(self.api_base, str)
                or not self.api_base.startswith(('http://', 'https://'))
                or self.credential_encoding not in {'plain', 'host_crypto'}
                or '${' in self.model or '${' in self.api_base):
            raise ValueError('supported host model binding required')
        object.__setattr__(self, 'api_base', self.api_base.rstrip('/'))
        ResourceDefinition('model', 'credential', self.reference)
        # Reuse the sink validation, including inline secret/query rejection.
        CredentialUse('model', self.reference, 'model', self.destination)

    @property
    def destination(self):
        return self.api_base.rstrip('/') + '/chat/completions'

    @property
    def reference(self):
        if self.credential_reference is not None:
            return self.credential_reference
        material = json.dumps([self.implementation, self.model, self.api_base.rstrip('/')], separators=(',', ':'))
        return 'model-catalog:' + hashlib.sha256(material.encode()).hexdigest()

    @classmethod
    def from_config(cls, config):
        if (not isinstance(config, dict) or config.get('client_provider', 'OpenAI') not in {'', 'OpenAI'}
                or config.get('api_mode', 'chat_completions') not in {None, '', 'chat_completions'}
                or config.get('auth_mode', 'api_key') not in {None, '', 'api_key'}
                or config.get('custom_headers')):
            raise ResourceAccessDenied('model credential transport is unsupported')
        return cls(config.get('model_name', ''), config.get('api_base', ''),
                   credential_reference=config.get('credential_reference'),
                   credential_encoding=config.get('credential_encoding'))


class ConfiguredModelCredentialResolver:
    """Resolve one explicit catalog entry after ResourceGuard authorization.

    No defaults-to-environment, OAuth registry, AgentOS catalog or alternate
    model fallback. A duplicate destination/model entry is ambiguous and denied.
    Only the selected entry is decrypted after matching nonsecret metadata.
    """
    def __init__(self, binding: ModelCredentialBinding, *, config_source: Callable | None = None,
                 credential_decoder: Callable | None = None):
        self._binding = binding
        self._config_source = config_source
        self._decoder = credential_decoder

    def resolve_credential(self, reference):
        from jiuwenswarm.common.config import get_config_raw
        if reference != self._binding.reference:
            raise ResourceAccessDenied('model credential reference mismatch')
        config = (self._config_source or get_config_raw)()
        models = config.get('models', {})
        entries = models.get('defaults')
        if not isinstance(entries, list):
            entry = models.get('default')
            entries = [entry] if isinstance(entry, dict) else []
        matches = []
        for entry in entries:
            try:
                if ModelCredentialBinding.from_config(entry.get('model_client_config')) == self._binding:
                    matches.append(entry)
            except (ValueError, TypeError, AttributeError, ResourceAccessDenied):
                continue
        if len(matches) != 1:
            raise ResourceAccessDenied('explicit model credential unavailable')
        secret = matches[0]['model_client_config'].get('api_key')
        if (not isinstance(secret, str) or not secret.strip()
                or secret.startswith('jiuwen-login:') or '${' in secret):
            raise ResourceAccessDenied('explicit model credential unavailable')
        if self._binding.credential_encoding == 'host_crypto':
            try:
                if self._decoder is None:
                    raise ResourceAccessDenied('credential decoder unavailable')
                secret = self._decoder(secret)
            except Exception:
                raise ResourceAccessDenied('credential decoding denied') from None
        if not isinstance(secret, str) or not secret or '${' in secret or secret.startswith('jiuwen-login:'):
            raise ResourceAccessDenied('decoded credential unavailable')
        return secret


class NativeModelCredentialAuthority:
    def __init__(self, execution: ResourceExecutionContext, *, resource_authorizer,
                 current_identity, is_current_execution, owns_execution=None, config_source=None,
                 credential_decoder=None):
        self.execution = execution
        self._resources = resource_authorizer
        self._identity = current_identity
        self._current = is_current_execution
        self._config_source = config_source
        self._owns_execution = owns_execution
        self._credential_decoder = credential_decoder

    async def __call__(self, binding: ModelCredentialBinding, target, *, native_session=None):
        try:
            if (type(binding) is not ModelCredentialBinding or self.execution.provider_id != 'native'
                    or target.method != 'POST' or target.url != binding.destination
                    or target.model != binding.model or target.api_mode != 'chat_completions'
                    or target.implementation != 'OpenAIModelClient'
                    or target.operation not in {'invoke', 'stream'}):
                raise ResourceAccessDenied('model request target mismatch')
            def current():
                return (self._current() is True and self._owns_execution is not None
                        and self._owns_execution(self.execution, native_session) is True)
            if self._identity() != self.execution.identity or not current():
                raise ResourceAccessDenied('model execution unavailable')
            grants = self._resources.resource_grants(self.execution.project_id, self.execution.identity)
            matches = [r for r in grants.get('resources', ())
                       if r.get('kind') == 'credential' and r.get('action') == 'use'
                       and r.get('reference') == binding.reference]
            if len(matches) != 1:
                raise ResourceAccessDenied('model credential grant unavailable')
            use = CredentialUse(matches[0]['resource_id'], binding.reference, 'model', binding.destination)
            authority = BoundCredentialAuthority(
                self.execution, uses=(use,), authorizer=self._resources,
                resolver=ConfiguredModelCredentialResolver(binding, config_source=self._config_source,
                                                           credential_decoder=self._credential_decoder),
                current_identity=self._identity, is_current_execution=current,
            )
            credential = await authority.resolve_for_request(use, destination=target.url)
            return {'Authorization': 'Bearer ' + credential}
        except asyncio.CancelledError:
            raise asyncio.CancelledError() from None
        except Exception:
            raise ResourceAccessDenied('model credential consumption denied') from None
