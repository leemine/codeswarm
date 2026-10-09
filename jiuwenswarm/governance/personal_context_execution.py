"""Host-owned PersonalContext authority over existing account resource grants.

No Session, Project, credential cache, or independent collector is created here.
The original Host and Core lifecycle own all work and its cancellation.
"""
from __future__ import annotations

import os
from pathlib import Path

from .model_credentials import (
    ConfiguredModelCredentialResolver, ModelCredentialBinding, configured_model_metadata,
)
from .organization_auth import configured_authenticator
from .resources import ResourceAccessDenied


def _account_resources(identity):
    auth = configured_authenticator()
    if auth is None or not auth.known_actor(identity):
        raise ResourceAccessDenied('personal context identity unavailable')
    policies = auth._config().get('private_session_resources', {})
    policy = policies.get(identity.actor_id, {}) if isinstance(policies, dict) else None
    if not isinstance(policy, dict) or not isinstance(policy.get('resources', []), list):
        raise ResourceAccessDenied('personal resource policy unavailable')
    return policy.get('resources', [])


def _model_binding(entry):
    return ModelCredentialBinding.from_config(entry['model_client_config'])


def authorize_model(identity, entry):
    binding = _model_binding(entry)
    matches = [r for r in _account_resources(identity)
               if isinstance(r, dict) and r.get('kind') == 'credential'
               and r.get('reference') == binding.reference
               and isinstance(r.get('actions'), list) and 'use' in r['actions']]
    if len(matches) != 1:
        raise ResourceAccessDenied('personal context model grant required')
    return binding


def private_fetch_environment(home):
    """Do not inherit another account's source credentials or configuration."""
    base = Path(home) / 'source-account'
    env = {key: os.environ[key] for key in ('PATH', 'LANG', 'LC_ALL', 'SYSTEMROOT') if key in os.environ}
    env.update(HOME=str(base), USERPROFILE=str(base), XDG_CONFIG_HOME=str(base/'config'),
               XDG_DATA_HOME=str(base/'data'), XDG_CACHE_HOME=str(base/'cache'),
               APPDATA=str(base/'config'), LOCALAPPDATA=str(base/'data'))
    return env


def _check_path(value, roots):
    path = Path(value)
    if not path.is_absolute() or any(p.is_symlink() for p in (path, *path.parents)):
        raise ResourceAccessDenied('personal context source path unavailable')
    if not any(path.resolve().is_relative_to(Path(root).resolve())
               for root in roots if isinstance(root, str) and Path(root).is_absolute()
               and not any(p.is_symlink() for p in (Path(root), *Path(root).parents))):
        raise ResourceAccessDenied('personal context source scope required')


class PersonalContextAuthority:
    def __init__(self, identity, *, credential_decoder=None):
        self.identity = identity
        self.permit = None
        self.config = None
        self.decoder = credential_decoder

    def check_identity(self):
        if self.permit is None or self.permit.identity != self.identity or not self.permit.revalidate():
            raise ResourceAccessDenied('personal context execution revoked')

    def check_provider_authorization(self, provider):
        """Gate account CLI access even before a fetch service is configured."""
        auth = configured_authenticator()
        if auth is None or not auth.known_actor(self.identity):
            raise ResourceAccessDenied('personal context identity unavailable')
        policies = auth._config().get('personal_context_sources', {})
        policy = policies.get(self.identity.actor_id, {}) if isinstance(policies, dict) else {}
        if provider == 'feishu' and (not isinstance(policy, dict) or policy.get('feishu') is not True):
            raise ResourceAccessDenied('private Feishu account authorization required')

    def validate(self, config):
        self.check_identity()
        auth = configured_authenticator()
        policies = auth._config().get('personal_context_sources', {})
        policy = policies.get(self.identity.actor_id, {}) if isinstance(policies, dict) else {}
        if not isinstance(policy, dict):
            raise ResourceAccessDenied('personal context source policy unavailable')
        for service in config.fetch_services:
            if service.provider in {'local_files', 'browser_bookmarks'}:
                field = 'root_dir' if service.provider == 'local_files' else 'bookmarks_path'
                roots = policy.get('read_roots', [])
                if not isinstance(roots, list):
                    raise ResourceAccessDenied('personal context source scope unavailable')
                _check_path(service.source[field], roots)
            elif service.provider == 'feishu':
                if policy.get('feishu') is not True:
                    raise ResourceAccessDenied('private Feishu account authorization required')
            # Repository credentials are user-supplied, retained only in their
            # private Host YAML. Public RSS/readers retain original URL checks.
        if config.model_request is not None:
            self.binding(config)

    def binding(self, config):
        client, request = config.model_client, config.model_request
        if client is None or request is None:
            raise ResourceAccessDenied('personal context model selection required')
        try:
            api_base = ModelCredentialBinding(
                model=request.model_name, api_base=str(client.api_base)).api_base
        except (ValueError, TypeError):
            raise ResourceAccessDenied('personal context model endpoint unavailable') from None
        matches = []
        for entry in configured_model_metadata():
            try:
                binding = _model_binding(entry)
                if binding.model == request.model_name and binding.api_base == api_base:
                    matches.append(entry)
            except (ValueError, TypeError, ResourceAccessDenied):
                continue
        if len(matches) != 1:
            raise ResourceAccessDenied('personal context model selection changed')
        return authorize_model(self.identity, matches[0])

    def check(self):
        self.check_identity()
        if self.config is None:
            raise ResourceAccessDenied('personal context execution not configured')
        self.validate(self.config)

    def bind_for_call(self):
        """Pin one configuration and principal for a call and all its retries."""
        self.check()
        config, permit, binding = self.config, self.permit, self.binding(self.config)
        async def authorize(target):
            if self.config is not config or self.permit is not permit or self.binding(config) != binding:
                raise ResourceAccessDenied('personal context model execution changed')
            return await self.model_request(target)
        return authorize

    async def model_request(self, target):
        self.check()
        binding = self.binding(self.config)
        if (target.method != 'POST' or target.url != binding.destination
                or target.model != binding.model or target.api_mode != 'chat_completions'
                or target.implementation != 'OpenAIModelClient'
                or target.operation not in {'invoke', 'stream'}):
            raise ResourceAccessDenied('personal context model destination mismatch')
        secret = ConfiguredModelCredentialResolver(
            binding, credential_decoder=self.decoder).resolve_credential(binding.reference)
        self.check()
        if binding != self.binding(self.config) or '\r' in secret or '\n' in secret:
            raise ResourceAccessDenied('personal context model grant changed')
        return {'Authorization': 'Bearer ' + secret}
