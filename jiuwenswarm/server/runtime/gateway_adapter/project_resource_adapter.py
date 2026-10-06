"""Organization-only project resource delegation on the original RPC registry."""
from __future__ import annotations

import inspect
import asyncio
import json
import time

from jiuwenswarm.common.schema.agent import AgentResponse
from jiuwenswarm.governance.contracts import TrustedIdentity
from jiuwenswarm.governance.organization_auth import current_principal
from jiuwenswarm.governance.resources import valid_expiry
from jiuwenswarm.server.runtime.session.history_io import run_history_io
from jiuwenswarm.server.runtime.session.project_access import ProjectRevisionConflict
from jiuwenswarm.server.runtime.session.project_resource_delegation import (
    ResourceMutationUnknown, delegation_inventory, mutate_delegation, public_id,
)
from .base import GatewayAdapter, build_error_response

_BASE = {'project_id', 'resource_id', 'target_actor', 'expected_acl_revision', 'expected_resource_revision'}
_FIELDS = {'project.resources.list': {'project_id'},
           'project.resources.grant': _BASE | {'actions', 'expires_at'},
           'project.resources.revoke': _BASE}


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


class ProjectResourceAdapter(GatewayAdapter):
    methods = frozenset(_FIELDS)

    def __init__(self, store, *, identity_resolver, target_resolver, after_mutation=None):
        self.store = store
        self.identity_resolver = identity_resolver
        self.target_resolver = target_resolver
        self.after_mutation = after_mutation

    @staticmethod
    def _parse(request):
        method = getattr(request.req_method, 'value', request.req_method)
        params = request.params
        if method not in _FIELDS or type(params) is not dict or set(params) != _FIELDS[method]:
            raise ValueError('invalid resource management fields')
        for key in ('project_id', 'resource_id', 'target_actor'):
            if key in params:
                public_id(params[key])
        if method != 'project.resources.list':
            for key in ('expected_acl_revision', 'expected_resource_revision'):
                maximum = 2 ** 53 - (2 if key == 'expected_resource_revision' else 1)
                if type(params[key]) is not int or not 1 <= params[key] <= maximum:
                    raise ValueError('invalid resource management revision')
        if method == 'project.resources.grant':
            actions = params['actions']
            if (type(actions) is not list or not actions or len(actions) > 4
                    or any(type(action) is not str or action not in {'read', 'write', 'invoke', 'use', 'execute'} for action in actions)
                    or len(set(actions)) != len(actions)
                    or not valid_expiry(params['expires_at'])
                    or params['expires_at'] is not None and params['expires_at'] <= time.time()):
                raise ValueError('invalid resource delegation bounds')
        return method, json.loads(_canonical(params))

    def _capture(self, request, method, params):
        resolver, target_resolver = self.identity_resolver, self.target_resolver
        store, after, principal = self.store, self.after_mutation, current_principal()
        path = str(store.path)
        facts = (request.request_id, request.channel_id, getattr(request, 'session_id', None), method, _canonical(params))

        def static():
            if (self.store is not store or self.identity_resolver is not resolver
                    or self.target_resolver is not target_resolver or self.after_mutation is not after
                    or str(store.path) != path
                    or (request.request_id, request.channel_id, getattr(request, 'session_id', None),
                        getattr(request.req_method, 'value', request.req_method), _canonical(request.params)) != facts):
                raise PermissionError('original resource request changed')
        static()
        identity = resolver(request)
        static()
        if type(identity) is not TrustedIdentity or identity.actor_id != identity.subject_id:
            raise PermissionError('authenticated resource identity required')

        def check():
            static()
            if current_principal() is not principal:
                raise PermissionError('original resource credential changed')
            if principal is not None and principal.identity() != identity:
                raise PermissionError('original resource credential changed')
            if resolver(request) != identity:
                raise PermissionError('original resource identity changed')
            static()
        check()
        return identity, check, target_resolver, after

    async def handle(self, request):
        result = None
        try:
            method, params = self._parse(request)
            identity, check, target, after = self._capture(request, method, params)
            if method == 'project.resources.list':
                result = await run_history_io(delegation_inventory, self.store,
                    params['project_id'], identity, check_current=check)
            else:
                # A missing/synchronous callback must deny before storage IO.
                if not callable(after) or not inspect.iscoroutinefunction(after):
                    raise PermissionError('resource exit verification unavailable')
                async def mutate_and_exit():
                    try:
                        committed = await run_history_io(mutate_delegation, self.store, method, params, identity,
                            check_current=check, target_resolver=target)
                    except ResourceMutationUnknown:
                        # The write may already exist. Keep its original Runtime
                        # drain in this same retained Task through caller cancellation.
                        committed = None
                    try:
                        await after()
                    except Exception:
                        return committed, False
                    return committed, True

                # Keep the accepted write and its existing Runtime drain together
                # even if the requesting Task is cancelled during worker IO.
                task = asyncio.create_task(mutate_and_exit())
                cancelled = None
                while True:
                    try:
                        result, exited = await asyncio.shield(task)
                        break
                    except asyncio.CancelledError as exc:
                        if task.cancelled():
                            raise
                        cancelled = exc
                if cancelled is not None:
                    raise cancelled
                if result is None:
                    return build_error_response(request,
                        'Resource change outcome unknown; refresh before any further action.',
                        code='MUTATION_OUTCOME_UNKNOWN')
                if not exited:
                    response = build_error_response(request,
                        'Resource change committed; execution exit remains unconfirmed. Refresh; do not repeat.',
                        code='EXIT_UNCONFIRMED', extra={**result.payload, 'exit_confirmed': False})
                    return self._bind(response, result)
            response = AgentResponse(request_id=request.request_id, channel_id=request.channel_id,
                                     ok=True, payload=result.payload, metadata=request.metadata)
            return self._bind(response, result)
        except ProjectRevisionConflict:
            return build_error_response(request, 'Resource versions changed; refresh.', code='CONFLICT')
        except PermissionError:
            if result is not None and 'mutation' in result.payload:
                return build_error_response(request, 'Resource change committed; current result authorization changed. Refresh; do not repeat.',
                    code='MUTATION_OUTCOME_UNKNOWN')
            return build_error_response(request, 'Resource authorization denied.', code='FORBIDDEN')
        except (ValueError, TypeError):
            return build_error_response(request, 'Invalid resource management request.', code='BAD_REQUEST')
        except Exception:
            if result is not None and 'mutation' in result.payload:
                return build_error_response(request, 'Resource change outcome unavailable; refresh; do not repeat.',
                    code='MUTATION_OUTCOME_UNKNOWN')
            return build_error_response(request, 'Resource authority unavailable.', code='FORBIDDEN')

    @staticmethod
    def _bind(response, result):
        expected = _canonical(response.payload)

        def check():
            if _canonical(response.payload) != expected:
                raise PermissionError('resource result changed')
            result.check()
        response._delivery_guard = check
        response._resource_mutation = result.payload.get('mutation')
        check()
        return response
