"""Bind current resource authority to actual, immutable Provider operations.

Operation-to-resource mapping belongs to the trusted host extension. It must
resolve actual executors/arguments; client-declared resource lists are never
inputs to this boundary. Missing mappings deny rather than invent a scope.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

from openjiuwen.harness_protocol import BeforeToolContext

from .contracts import TrustedIdentity
from .resources import ResourceAuthorizer, ResourceGuard, ResourceRequest, normalized


@dataclass(frozen=True, slots=True)
class ResourceExecutionContext:
    project_id: str
    identity: TrustedIdentity
    session_id: str
    workspace: str
    provider_id: str

    def __post_init__(self):
        for name in ('project_id', 'session_id', 'workspace', 'provider_id'):
            normalized(getattr(self, name), name)
        if not isinstance(self.identity, TrustedIdentity) or not Path(self.workspace).is_absolute():
            raise ValueError('trusted identity and absolute workspace required')


@dataclass(frozen=True, slots=True)
class ToolResourceUse:
    request: ResourceRequest
    reference: str

    def __post_init__(self):
        if not isinstance(self.request, ResourceRequest):
            raise ValueError('resource request required')
        normalized(self.reference, 'expected resource reference')


class ToolResourceResolver(Protocol):
    def resources_for_tool(
        self, execution: ResourceExecutionContext, tool: BeforeToolContext,
    ) -> tuple[ToolResourceUse, ...]:
        """Return every required resource for a supported actual operation.

        An empty result, unknown schema/executor, or exception denies. Reference
        values must come from host bindings, never from user-supplied metadata.
        The host re-resolves after policy evaluation: unchanged execution must
        yield the same ordered requirements, and mapping must never grant access.
        """
        ...


class BoundToolResourceAuthority:
    """One private execution's policy; grants are checked anew for every call."""

    def __init__(
        self, execution: ResourceExecutionContext, *, authorizer: ResourceAuthorizer | None,
        resolver: ToolResourceResolver | None,
        current_identity: Callable[[], TrustedIdentity | None],
        is_current_execution: Callable[[], bool],
    ):
        self.execution = execution
        self._guard = ResourceGuard(authorizer)
        self._resolver = resolver
        self._identity = current_identity
        self._current = is_current_execution

    async def __call__(self, tool: BeforeToolContext) -> bool:
        return self.check(tool)

    def check(self, tool: BeforeToolContext) -> bool:
        """Recheck the same actual operation without scheduling or granting it."""
        try:
            if (not isinstance(tool, BeforeToolContext)
                    or self._identity() != self.execution.identity
                    or self._current() is not True or self._resolver is None):
                return False
            uses = self._resolver.resources_for_tool(self.execution, tool)
            if not isinstance(uses, tuple) or not uses:
                return False
            for use in uses:
                if not isinstance(use, ToolResourceUse):
                    return False
                decision = self._guard.check(self.execution.project_id, self.execution.identity, use.request)
                if decision.reference != use.reference:
                    return False
            # A host policy may itself mutate external state; never accept an
            # identity/generation transition during its synchronous resolution.
            if self._identity() != self.execution.identity or self._current() is not True:
                return False
            # Policies are synchronous but may mutate executor/resource context.
            # Re-resolve after all policy calls instead of trusting the first map.
            final_uses = self._resolver.resources_for_tool(self.execution, tool)
            return (final_uses == uses and self._identity() == self.execution.identity
                    and self._current() is True)
        except Exception:
            return False
