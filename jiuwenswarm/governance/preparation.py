# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Bounded request receipts and authorization checks, not an execution scheduler.

Receipts last for the owning Runtime lifetime. Capacity exhaustion fails closed;
unknown submissions are never evicted and then accidentally sent a second time.
Existing Runtime/Provisioner owners still serialize execution and own cleanup.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, is_dataclass
from enum import Enum
from typing import Any, Awaitable, Callable, Literal

from .contracts import AuthorizationDecision, ProjectAction, ProjectAuthorizer, TrustedIdentity


class GovernanceError(ValueError):
    """A request cannot cross the governed admission boundary."""


class AlreadySubmitted(GovernanceError):
    def __init__(self, outcome: str) -> None:
        self.outcome = outcome
        super().__init__(f"request already {outcome}; do not resend")


def _json_default(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    raise TypeError(f"unsupported request snapshot value: {type(value).__name__}")


def snapshot_input(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False, default=_json_default)


@dataclass(frozen=True, slots=True)
class PreparedRequest:
    request_id: str
    identity: TrustedIdentity | None
    project_id: str
    action: ProjectAction
    session_id: str
    generation: int | None
    input_snapshot: str
    fingerprint: str
    authorization: AuthorizationDecision | None


@dataclass(slots=True)
class _Receipt:
    prepared: PreparedRequest
    outcome: Literal["prepared", "accepted", "unknown", "rejected"] = "prepared"


class SubmissionGuard:
    """Single-event-loop admission facts; contains no tasks, queues or locks."""
    def __init__(self, authorizer: ProjectAuthorizer | None, *, capacity: int = 4096) -> None:
        if capacity < 1:
            raise ValueError("receipt capacity must be positive")
        self._authorizer = authorizer
        self._capacity = capacity
        self._receipts: dict[tuple[str, str, str], _Receipt] = {}

    def _key(self, prepared: PreparedRequest) -> tuple[str, str, str]:
        # A request ID is scoped to the authenticated actor and execution subject.
        identity = prepared.identity
        principal = snapshot_input(asdict(identity)) if identity else ""
        return principal, prepared.session_id, prepared.request_id

    def _authorize(self, project_id: str, identity: TrustedIdentity | None,
                   action: ProjectAction) -> AuthorizationDecision | None:
        if not project_id:
            return None
        if self._authorizer is None:
            raise GovernanceError("project authorization authority is unavailable")
        actor_id = identity.actor_id if identity else ""
        decision = self._authorizer.authorize(project_id, actor_id, action)
        if (decision.project_id, decision.actor_id, decision.action) != (project_id, actor_id, action):
            raise GovernanceError("authorization authority returned a mismatched decision")
        if not decision.allowed:
            raise GovernanceError(f"project {action} denied: {decision.reason}")
        return decision

    def check_access(self, project_id: str, identity: TrustedIdentity | None,
                     action: ProjectAction) -> AuthorizationDecision | None:
        return self._authorize(project_id, identity, action)

    def prepare(self, *, request_id: str, identity: TrustedIdentity | None,
                project_id: str, action: ProjectAction, session_id: str,
                generation: int | None, inputs: Any) -> PreparedRequest:
        if not request_id:
            raise GovernanceError("governed requests require a request_id")
        snapshot = snapshot_input(inputs)
        fingerprint = hashlib.sha256(snapshot.encode()).hexdigest()
        candidate = PreparedRequest(request_id, identity, project_id, action, session_id,
                                    generation, snapshot, fingerprint, None)
        key = self._key(candidate)
        prior = self._receipts.get(key)
        if prior is not None:
            old = prior.prepared
            if (old.fingerprint, old.project_id, old.action, old.generation) != (
                fingerprint, project_id, action, generation
            ):
                raise GovernanceError("request_id was already bound to different input or generation")
            # Even an in-progress preparation must not acquire resources twice.
            raise AlreadySubmitted(prior.outcome)
        decision = self._authorize(project_id, identity, action)
        if len(self._receipts) >= self._capacity:
            raise GovernanceError("request receipt capacity exhausted; create a new Runtime")
        prepared = PreparedRequest(request_id, identity, project_id, action, session_id,
                                   generation, snapshot, fingerprint, decision)
        self._receipts[key] = _Receipt(prepared)
        return prepared

    def _receipt(self, prepared: PreparedRequest) -> _Receipt:
        receipt = self._receipts.get(self._key(prepared))
        if receipt is None or receipt.prepared is not prepared:
            raise GovernanceError("preparation belongs to another admission authority")
        return receipt

    def revalidate(self, prepared: PreparedRequest, *, generation: int | None) -> None:
        self._receipt(prepared)
        if generation != prepared.generation:
            raise GovernanceError("session generation changed after preparation")
        # Keep the original revision/fingerprint intact; a newer allowed revision
        # is acceptable, but a revoked decision prevents submission.
        self._authorize(prepared.project_id, prepared.identity, prepared.action)

    def begin_submission(self, prepared: PreparedRequest, *, generation: int | None) -> None:
        receipt = self._receipt(prepared)
        if receipt.outcome != "prepared":
            raise AlreadySubmitted(receipt.outcome)
        try:
            self.revalidate(prepared, generation=generation)
        except BaseException:
            receipt.outcome = "rejected"
            raise
        # The call about to cross a side-effect boundary may have committed even
        # when the caller receives no acknowledgement. Never infer safe retry.
        receipt.outcome = "unknown"

    def accepted(self, prepared: PreparedRequest) -> None:
        receipt = self._receipt(prepared)
        if receipt.outcome != "unknown":
            raise GovernanceError("submission has not crossed its commit boundary")
        receipt.outcome = "accepted"

    def reject(self, prepared: PreparedRequest) -> None:
        receipt = self._receipt(prepared)
        if receipt.outcome == "prepared":
            receipt.outcome = "rejected"

    def outcome(self, prepared: PreparedRequest) -> str:
        return self._receipt(prepared).outcome

    def clear(self) -> None:
        """Only call after the owning Runtime has closed."""
        self._receipts.clear()


async def compensate_owned(
    releases: tuple[Callable[[], Awaitable[None]], ...],
) -> tuple[BaseException, ...]:
    """Release only explicitly transferred preparation leases, in reverse order.

    Cleanup errors are returned without hiding the original preparation failure.
    After submission becomes unknown, the caller must keep execution-owned leases
    with their existing lifecycle owner instead of rolling them back here.
    """
    failures: list[BaseException] = []
    for release in reversed(releases):
        try:
            await release()
        except BaseException as exc:
            failures.append(exc)
    return tuple(failures)
