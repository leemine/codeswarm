"""Host-only credential consumption using the existing resource authority.

This module stores neither secrets nor grants. A trusted consumer binds the
resource/reference and exact destination; request fields cannot choose either.
The resolver is called only after authorization, and a wait or policy revision
invalidates the result before it can be delivered to a transport. Consumers must
call this for every actual request/retry, not cache the returned credential.
"""
from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass
from typing import Callable, Literal, Protocol
from urllib.parse import urlsplit

from .contracts import TrustedIdentity
from .resources import ResourceAccessDenied, ResourceAuthorizer, ResourceGuard, ResourceRequest, ResourceDecision, normalized
from .tool_resources import ResourceExecutionContext


@dataclass(frozen=True, slots=True)
class CredentialUse:
    resource_id: str
    reference: str
    purpose: Literal['model', 'mcp', 'provider']
    destination: str

    def __post_init__(self):
        for name in ('resource_id', 'reference', 'destination'):
            normalized(getattr(self, name), name)
        if self.purpose not in {'model', 'mcp', 'provider'}:
            raise ValueError('unknown credential purpose')
        # An exact host-selected sink ID/URL, never an arbitrary client URL.
        # HTTP sinks exclude inline credentials, queries and fragments.
        if '://' in self.destination:
            target = urlsplit(self.destination)
            if (target.scheme not in {'http', 'https'} or not target.hostname
                    or target.username is not None or target.password is not None
                    or target.query or target.fragment):
                raise ValueError('invalid credential destination')


class CredentialResolver(Protocol):
    def resolve_credential(self, reference: str):
        """Return a secret string or an awaitable, without ambient fallback."""
        ...


class BoundCredentialAuthority:
    """One private execution and one immutable set of trusted credential sinks."""

    def __init__(
        self, execution: ResourceExecutionContext, *, uses: tuple[CredentialUse, ...],
        authorizer: ResourceAuthorizer | None, resolver: CredentialResolver | None,
        current_identity: Callable[[], TrustedIdentity | None],
        is_current_execution: Callable[[], bool],
    ):
        if (not isinstance(execution, ResourceExecutionContext)
                or not isinstance(uses, tuple) or any(not isinstance(use, CredentialUse) for use in uses)
                or len(set(uses)) != len(uses)):
            raise ValueError('immutable host credential bindings required')
        self.execution = execution
        self._uses = frozenset(uses)
        self._guard = ResourceGuard(authorizer)
        self._resolver = resolver
        self._identity = current_identity
        self._current = is_current_execution

    def _check(self, use: CredentialUse):
        if (type(use) is not CredentialUse or use not in self._uses
                or self._identity() != self.execution.identity or self._current() is not True):
            raise ResourceAccessDenied('credential execution is unavailable')
        decision = self._guard.check(
            self.execution.project_id, self.execution.identity, ResourceRequest(use.resource_id, 'use')
        )
        if (decision.reference != use.reference
                or self._identity() != self.execution.identity or self._current() is not True):
            raise ResourceAccessDenied('credential binding is unavailable')
        return decision

    def check_for_request(self, use: CredentialUse, *, destination: str) -> ResourceDecision:
        """Revalidate a bound sink without resolving or retaining a credential.

        Consumers may compare this immutable decision across a request/response
        wait. This uses the same authority as credential resolution.
        """
        try:
            if type(use) is not CredentialUse or destination != use.destination:
                raise ResourceAccessDenied('credential destination is unavailable')
            return self._check(use)
        except asyncio.CancelledError:
            cancelled = True
        except Exception:
            cancelled = False
        # Raise outside the handler: even private exception context is secret-free.
        if cancelled:
            raise asyncio.CancelledError()
        raise ResourceAccessDenied('credential consumption denied')

    async def resolve_for_request(self, use: CredentialUse, *, destination: str) -> str:
        """Resolve immediately before a matching sink performs one operation.

        A transport redirect is another operation and must present its actual
        destination. Unknown paths/targets fail closed; no prefix matching or
        caller-selected resource/reference is performed here.
        """
        try:
            if type(use) is not CredentialUse or destination != use.destination or self._resolver is None:
                raise ResourceAccessDenied('credential destination is unavailable')
            before = self._check(use)
            secret = self._resolver.resolve_credential(use.reference)
            if inspect.isawaitable(secret):
                secret = await secret
            after = self._check(use)
            if before != after or type(secret) is not str or not secret or '\r' in secret or '\n' in secret:
                raise ResourceAccessDenied('credential resolution is unavailable')
            return secret
        except asyncio.CancelledError:
            raise asyncio.CancelledError() from None
        except Exception:
            # Host resolvers may include secrets in their exceptions. Neither
            # public text nor chained diagnostics may expose those values.
            raise ResourceAccessDenied('credential consumption denied') from None
