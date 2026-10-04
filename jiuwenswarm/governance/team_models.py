"""Pure model construction for explicitly owned Native Team members.

This module does not admit a Team, register a Session or grant credentials. The
host must supply exact live build/slice ownership; the existing Native model
consumer checks the original resource authority at each actual HTTP attempt.
"""
from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field

from openjiuwen.core.foundation.llm import Model, ModelClientConfig
from openjiuwen.harness.execution_subject import current_execution_subject
from openjiuwen.harness.schema.build_context import BuildContext
from openjiuwen.harness.schema.deep_agent_spec import ModelSpec

from .model_consumer import NativeModelRequestAuthority
from .model_credentials import ModelCredentialBinding
from .resources import ResourceAccessDenied
from .tool_context import NativeExecutionSlice, current_model_authorizer, current_native_execution_slice

_DENIED = 'Native Team model ownership or catalog unavailable'
_CONTEXT_FIELDS = ('member_name', 'member_card_id', 'role', 'subagent_name',
                   'session_id', 'team_id', 'project_dir')


def _checked(predicate, *args):
    cancelled = False
    try:
        result = predicate(*args)
        if inspect.iscoroutine(result):
            result.close()
        allowed = result is True
    except asyncio.CancelledError:
        cancelled, allowed = True, False
    except Exception:
        allowed = False
    if cancelled:
        raise asyncio.CancelledError() from None
    return allowed


def _context_facts(context):
    if (not isinstance(context, BuildContext) or context.subagent_name is not None
            or context.role not in {'leader', 'teammate'}
            or not isinstance(context.member_name, str) or not context.member_name
            or not isinstance(context.member_card_id, str) or not context.member_card_id):
        raise ResourceAccessDenied(_DENIED)
    return tuple(getattr(context, key, None) for key in _CONTEXT_FIELDS)


@dataclass(frozen=True, slots=True, repr=False)
class _MemberModelAuthority:
    delegate: NativeModelRequestAuthority
    context: BuildContext = field(repr=False)
    facts: tuple = field(repr=False)
    owns_build: Callable = field(repr=False)
    owns_slice: Callable = field(repr=False)

    def _current(self, bound):
        if type(bound) is not NativeExecutionSlice:
            return False
        snapshot = (bound.owner, bound.subject, bound.tool_authorizer, bound.model_authorizer)
        try:
            valid = (type(bound) is NativeExecutionSlice and bound.active and bound.owner is not None
                     and current_native_execution_slice() is bound
                     and bound.subject is not None and bound.subject == current_execution_subject()
                     and getattr(bound.subject, 'kind', None) == (
                         'team_leader' if self.context.role == 'leader' else 'team_member')
                     and _context_facts(self.context) == self.facts
                     and _checked(self.owns_build, self.context)
                     and _checked(self.owns_slice, self.context, bound))
            # Host predicates cannot replace the slice or its source facts.
            return (valid and current_native_execution_slice() is bound and bound.active
                    and bound.subject == current_execution_subject()
                    and all(actual is expected for actual, expected in zip(
                        (bound.owner, bound.subject, bound.tool_authorizer, bound.model_authorizer), snapshot))
                    and current_model_authorizer() is bound.model_authorizer
                    and _context_facts(self.context) == self.facts)
        except Exception:
            return False

    def bind_for_call(self):
        # This is called by core before transforms/awaits, never at construction.
        bound = current_native_execution_slice()
        if not self._current(bound):
            raise ResourceAccessDenied(_DENIED)
        owner, subject = bound.owner, bound.subject
        tool_authorizer, model_authorizer = bound.tool_authorizer, bound.model_authorizer
        delegate = self.delegate.bind_for_call()

        def current():
            return (self._current(bound) and bound.owner is owner and bound.subject is subject
                    and bound.tool_authorizer is tool_authorizer and bound.model_authorizer is model_authorizer)

        async def authorize(target):
            cancelled, headers = False, None
            try:
                if current():
                    headers = await delegate(target)
                    if not current():
                        headers = None
            except asyncio.CancelledError:
                cancelled = True
            except Exception:
                headers = None
            # Do not retain host exceptions/secret messages in public context chains.
            if cancelled:
                raise asyncio.CancelledError() from None
            if headers is None:
                raise ResourceAccessDenied(_DENIED)
            return headers
        return authorize


def make_native_team_model_factory(*, catalog_metadata: Sequence[Mapping],
                                   owns_build_context: Callable,
                                   owns_execution_slice: Callable):
    """Return core's ``ModelSpec, BuildContext -> Model`` live factory.

    Catalog rows use configured_model_metadata's shape and contain no real key.
    Both ownership predicates must return exactly True. The slice predicate must
    prove exact member/Harness/Session objects; matching a name is insufficient.
    No factory or authority is reconstructed from a persisted BuildContext seed.
    Existing provider-based/default child factories remain a separate integration.
    """
    if (not callable(owns_build_context) or not callable(owns_execution_slice)
            or not isinstance(catalog_metadata, (list, tuple))):
        raise TypeError('explicit Native Team metadata and ownership predicates required')
    bindings = []
    for row in catalog_metadata:
        client = row.get('model_client_config') if isinstance(row, Mapping) else None
        if not isinstance(client, Mapping):
            raise ValueError('invalid Native Team model metadata')
        if client.get('api_key') not in {None, '', 'MODEL_REQUEST_AUTHORITY'}:
            raise ValueError('Native Team factory requires nonsecret catalog metadata')
        # Retain only immutable destination/credential-reference facts, never the
        # mutable metadata row, client config, key or caller's model cache.
        try:
            binding = ModelCredentialBinding.from_config(dict(client))
        except (TypeError, ValueError, ResourceAccessDenied):
            continue  # An unsupported catalog entry is never a fallback.
        bindings.append(binding)
    bindings = tuple(bindings)

    def build(spec: ModelSpec, context: BuildContext) -> Model:
        if not isinstance(spec, ModelSpec):
            raise ResourceAccessDenied(_DENIED)
        facts = _context_facts(context)
        if not _checked(owns_build_context, context) or _context_facts(context) != facts:
            raise ResourceAccessDenied(_DENIED)
        request = spec.model_request_config
        if request is None or not request.model_name:
            raise ResourceAccessDenied(_DENIED)
        client = spec.model_client_config
        if type(client) is not ModelClientConfig:
            raise ResourceAccessDenied(_DENIED)
        if set(client.model_extra or {}) - {'model_name', 'credential_reference', 'credential_encoding'}:
            raise ResourceAccessDenied(_DENIED)
        raw = client.model_dump(mode='json')
        if raw.get('model_name', request.model_name) != request.model_name:
            raise ResourceAccessDenied(_DENIED)
        raw.update(model_name=request.model_name, api_key='MODEL_REQUEST_AUTHORITY')
        raw.setdefault('credential_encoding', 'plain')
        try:
            selected = ModelCredentialBinding.from_config(raw)
        except (TypeError, ValueError, ResourceAccessDenied):
            selected = None
        if selected is None:
            raise ResourceAccessDenied(_DENIED)
        explicit_reference = raw.get('credential_reference')
        matches = [binding for binding in bindings
                   if (binding.model, binding.api_base, binding.implementation)
                   == (selected.model, selected.api_base, selected.implementation)
                   and (explicit_reference is None or binding.reference == explicit_reference)
                   and ('credential_encoding' not in (client.model_extra or {})
                        or binding.credential_encoding == selected.credential_encoding)]
        if len(matches) != 1:
            raise ResourceAccessDenied(_DENIED)
        binding = matches[0]
        # Explicit instance authority is required even without an ambient scope.
        # Model/core owns private transport, final HTTP checks and retry behavior.
        authority = _MemberModelAuthority(NativeModelRequestAuthority(binding), context, facts,
                                          owns_build_context, owns_execution_slice)
        return Model(model_client_config=client.model_copy(deep=True, update={'api_key': 'MODEL_REQUEST_AUTHORITY'}),
                     model_config=request.model_copy(deep=True), request_authority=authority)
    return build
