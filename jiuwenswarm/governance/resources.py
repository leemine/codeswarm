"""Resource authority contracts consumed immediately before an operation.

The ``governance.resources`` extension capability implements ResourceAuthorizer.
These are admission facts, not leases, queues, credentials or OS isolation.
"""

from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal, Protocol

from .contracts import TrustedIdentity

ResourceKind = Literal["workspace", "tool", "credential", "process"]
_ACTIONS = {
    "workspace": frozenset({"read", "write"}),
    "tool": frozenset({"invoke"}),
    "credential": frozenset({"use"}),
    "process": frozenset({"execute"}),
}
_REFERENCE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/@-]{0,255}\Z")


class ResourceAccessDenied(PermissionError):
    """A required resource cannot be used; never includes credential values."""


def normalized(value: str, field: str) -> str:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise ValueError(f"{field} must be a nonempty normalized string")
    return value


def valid_expiry(value: float | None) -> bool:
    return value is None or (
        type(value) in (int, float) and math.isfinite(value) and value > 0
    )


@dataclass(frozen=True, slots=True)
class ResourceDefinition:
    resource_id: str
    kind: ResourceKind
    reference: str

    def __post_init__(self) -> None:
        normalized(self.resource_id, "resource_id")
        if self.kind not in _ACTIONS:
            raise ValueError("unknown resource kind")
        normalized(self.reference, "reference")
        if self.kind == "workspace":
            path = Path(self.reference)
            if not path.is_absolute() or str(path.resolve()) != self.reference:
                raise ValueError(
                    "workspace reference must be a canonical absolute path"
                )
        elif not _REFERENCE.fullmatch(self.reference):
            raise ValueError(
                "resource reference must be an opaque identifier, not a secret value"
            )


@dataclass(frozen=True, slots=True)
class ResourceRequest:
    resource_id: str
    action: str
    path: str | None = None

    def __post_init__(self) -> None:
        normalized(self.resource_id, "resource_id")
        if self.action not in {
            action for actions in _ACTIONS.values() for action in actions
        }:
            raise ValueError("unknown resource action")
        if self.path is not None:
            normalized(self.path, "path")
            if not Path(self.path).is_absolute():
                raise ValueError("resource operation path must be absolute")


@dataclass(frozen=True, slots=True)
class ResourceDecision:
    allowed: bool
    project_id: str
    actor_id: str
    subject_id: str
    request: ResourceRequest
    acl_revision: int
    resource_revision: int
    reason: str = ""
    reference: str | None = None
    scope: str | None = None
    expires_at: float | None = None

    def __post_init__(self) -> None:
        if type(self.allowed) is not bool:
            raise ValueError("resource decision allowed must be a boolean")
        for revision in (self.acl_revision, self.resource_revision):
            if type(revision) is not int or revision < 0:
                raise ValueError(
                    "resource authorization revisions must be nonnegative integers"
                )
        if not isinstance(self.request, ResourceRequest) or not valid_expiry(
            self.expires_at
        ):
            raise ValueError("invalid resource decision")


class ResourceAuthorizer(Protocol):
    def authorize_resource(
        self,
        project_id: str,
        identity: TrustedIdentity,
        request: ResourceRequest,
    ) -> ResourceDecision: ...


class ResourceGuard:
    """Validate every host decision; do not cache grants across operations."""

    def __init__(
        self,
        authorizer: ResourceAuthorizer | None,
        *,
        clock: Callable[[], float] = time.time,
    ):
        self._authorizer = authorizer
        self._clock = clock

    def check(
        self,
        project_id: str,
        identity: TrustedIdentity | None,
        request: ResourceRequest,
    ) -> ResourceDecision:
        if (
            not isinstance(identity, TrustedIdentity)
            or not project_id
            or self._authorizer is None
        ):
            raise ResourceAccessDenied(
                "resource authority and trusted identity are required"
            )
        try:
            decision = self._authorizer.authorize_resource(
                project_id, identity, request
            )
            if not isinstance(decision, ResourceDecision):
                raise ValueError("invalid resource decision type")
            # Recheck types as third-party policies may construct malformed values.
            decision.__post_init__()
            if (
                decision.project_id,
                decision.actor_id,
                decision.subject_id,
                decision.request,
            ) != (
                project_id,
                identity.actor_id,
                identity.subject_id,
                request,
            ):
                raise ValueError("resource authority returned a mismatched decision")
            if (
                not decision.allowed
                or decision.resource_revision < 1
                or decision.acl_revision < 1
            ):
                raise ValueError("resource permission denied")
            normalized(decision.reference, "resource reference")
            if request.path is not None:
                if (
                    not isinstance(decision.scope, str)
                    or not Path(decision.scope).is_absolute()
                    or not Path(request.path)
                    .resolve()
                    .is_relative_to(Path(decision.scope).resolve())
                ):
                    raise ValueError(
                        "resource authority returned an invalid path scope"
                    )
            elif decision.scope is not None:
                raise ValueError("non-workspace operation cannot use a path scope")
            if decision.expires_at is not None and decision.expires_at <= self._clock():
                raise ValueError("resource authorization expired")
            return decision
        except Exception as exc:
            raise ResourceAccessDenied("resource authorization denied") from exc
