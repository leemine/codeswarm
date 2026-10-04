"""Host catalog binding for one original OpenCode request; no secret caching."""
from __future__ import annotations

import asyncio
from pathlib import Path

from .credential_resources import BoundCredentialAuthority, CredentialUse
from .model_credentials import (
    ConfiguredModelCredentialResolver, ModelCredentialBinding,
    configured_model_metadata, model_entry_fingerprint,
)
from .resources import ResourceAccessDenied


class OpenCodeModelCredentialAuthority:
    """The submitting Runtime owns identity, catalog selection and live checks.

    Initial adapter construction selects nonsecret metadata only. Each later
    request captures its own authority from the submitting Runtime scope. The
    HTTP consumer resolves credentials anew for each actual request/retry.
    """

    def __init__(self, execution, *, resource_authorizer, current_identity,
                 is_current_execution, capture_binding, model_selection=None,
                 metadata_source=None, config_source=None, credential_decoder=None,
                 binding_checker=None):
        if execution.provider_id != 'opencode':
            raise ValueError('OpenCode model execution required')
        self.execution = execution
        self._resources = resource_authorizer
        self._identity = current_identity
        self._current = is_current_execution
        self._capture_binding = capture_binding
        self._selection = model_selection
        self._metadata = metadata_source or configured_model_metadata
        self._config = config_source
        self._decoder = credential_decoder
        self._binding_checker = binding_checker

    def _select(self, route):
        from openjiuwen.harness_providers.opencode.config import OpenCodeHarnessConfig
        from jiuwenswarm.runtime.model_catalog import build_model_catalog, resolve_model_selection

        original = route.bound.binding
        original.validate_spec(route.bound.spec)
        execution = self.execution
        if (route.provider_id != 'opencode' or original.provider_id != 'opencode'
                or original.host_session_id != execution.session_id
                or original.subject_id != execution.identity.subject_id
                or original.workspace != str(Path(execution.workspace).resolve())
                or route.trusted_subject_id != execution.identity.subject_id
                or self._identity() != execution.identity or self._current() is not True):
            raise ResourceAccessDenied('original OpenCode model scope unavailable')
        model = OpenCodeHarnessConfig.from_mapping(route.bound.spec.provider_config).model
        if model is None or model.api_key is not None:
            raise ResourceAccessDenied('host model gateway requires a credential-free provider config')
        entries = tuple(self._metadata())
        selected_index = None
        if self._selection:
            selected = resolve_model_selection(build_model_catalog(entries), self._selection)
            if selected.selection_key != self._selection or selected.is_agentos:
                raise ResourceAccessDenied('exact configured model selection required')
            selected_index = int(selected.selection_key.rpartition('#')[2])
        matches = []
        for index, entry in enumerate(entries):
            try:
                binding = ModelCredentialBinding.from_config(entry['model_client_config'])
            except (KeyError, TypeError, ValueError, ResourceAccessDenied):
                continue
            if binding.model == model.model and binding.api_base == model.api_base:
                matches.append((index, entry, binding))
        choices = [item for item in matches if selected_index is None or item[0] == selected_index]
        if len(choices) != 1:
            raise ResourceAccessDenied('explicit unambiguous OpenCode model catalog binding required')
        _, entry, binding = choices[0]
        if sum(item[2] == binding for item in matches) != 1:
            raise ResourceAccessDenied('ambiguous model credential reference')
        fingerprint = model_entry_fingerprint(entry['model_client_config'], entry.get('model_config_obj') or {})
        if self._binding_checker is not None:
            self._binding_checker(binding, fingerprint)
        return binding, fingerprint

    def __call__(self, route, binding=None):
        """Select once for construction, or capture one actual request's owner."""
        try:
            selected, fingerprint = self._select(route)
            if binding is None:
                return selected
            if type(binding) is not ModelCredentialBinding or binding != selected:
                raise ResourceAccessDenied('original model binding changed')
            original = route.bound.binding
            owner_current = self._capture_binding(original)
            if not callable(owner_current) or owner_current() is not True:
                raise ResourceAccessDenied('original model owner unavailable')

            def current():
                now_binding, now_fingerprint = self._select(route)
                return (now_binding == binding and now_fingerprint == fingerprint
                        and route.bound.binding is original
                        and owner_current() is True)

            grants = self._resources.resource_grants(self.execution.project_id, self.execution.identity)
            matches = [resource for resource in grants.get('resources', ())
                       if resource.get('kind') == 'credential' and resource.get('action') == 'use'
                       and resource.get('reference') == binding.reference]
            if len(matches) != 1:
                raise ResourceAccessDenied('model credential grant unavailable')
            use = CredentialUse(matches[0]['resource_id'], binding.reference, 'model', binding.destination)
            authority = BoundCredentialAuthority(
                self.execution, uses=(use,), authorizer=self._resources,
                resolver=ConfiguredModelCredentialResolver(binding, config_source=self._config,
                                                           credential_decoder=self._decoder),
                current_identity=self._identity, is_current_execution=current,
            )
            from .opencode_model_http import OpenCodeModelOperationAuthority
            return OpenCodeModelOperationAuthority(authority, use, current)
        except asyncio.CancelledError:
            cancelled = True
        except Exception:
            cancelled = False
        # Host catalog/decoder extensions can attach secret-bearing diagnostics.
        # Neither exception context nor cancellation text crosses this boundary.
        if cancelled:
            raise asyncio.CancelledError()
        raise ResourceAccessDenied('OpenCode model authority unavailable')
