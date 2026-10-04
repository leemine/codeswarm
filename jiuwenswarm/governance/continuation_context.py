"""Live host continuation input, never a wire grant or a session checkpoint.

Runtime creates one handle per admitted request. Its synchronous checker must
capture the original full trusted identity and revalidate source, target owner
and admission lifecycle. The seed's recipient identity alone is not authority.
Copying a request preserves this opaque handle; serializing it is unsupported.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Callable

from .continuation import ContinuationSeed
from .contracts import TrustedIdentity

_SEAL = object()


class ContinuationContextDenied(PermissionError):
    """A missing, changed or no longer current host continuation input."""


@dataclass(frozen=True, slots=True, init=False, repr=False)
class ContinuationContext:
    session_id: str
    request_id: str
    identity: TrustedIdentity
    seed: ContinuationSeed
    check: Callable[[], None]
    _seal: object

    def __init__(self, *args, **kwargs):
        raise TypeError("use the live host continuation context factory")

    def __copy__(self):
        return self

    def __deepcopy__(self, memo):
        memo[id(self)] = self
        return self

    def __reduce_ex__(self, protocol):
        raise TypeError("live continuation context cannot be serialized")

    def validate(self, session_id: str, request_id: str | None = None) -> None:
        if (
            getattr(self, "_seal", None) is not _SEAL
            or type(self.identity) is not TrustedIdentity
            or type(self.seed) is not ContinuationSeed
            or self.seed.proof.identity != self.identity
            or self.session_id == self.seed.proof.request.session_id
            or self.session_id != session_id
            or (request_id is not None and self.request_id != request_id)
        ):
            raise ContinuationContextDenied("continuation context is not current")
        try:
            result = self.check()
            if inspect.iscoroutine(result):
                result.close()
            if result is not None:
                raise ValueError("synchronous checker required")
        except Exception:
            # Never expose a credential, history excerpt or authority exception.
            raise ContinuationContextDenied(
                "continuation context is not current"
            ) from None


def create_continuation_context(
    *,
    session_id: str,
    request_id: str,
    identity: TrustedIdentity,
    seed: ContinuationSeed,
    check: Callable[[], None],
) -> ContinuationContext:
    """Host-only factory; ``check`` must revalidate the captured original admission."""
    if any(
        not isinstance(value, str) or not value or value != value.strip()
        for value in (session_id, request_id)
    ) or not callable(check):
        raise ContinuationContextDenied("invalid continuation context")
    value = object.__new__(ContinuationContext)
    for name, item in (
        ("session_id", session_id),
        ("request_id", request_id),
        ("identity", identity),
        ("seed", seed),
        ("check", check),
        ("_seal", _SEAL),
    ):
        object.__setattr__(value, name, item)
    value.validate(session_id, request_id)
    return value


def validate_continuation_context(
    value: object,
    session_id: str,
    request_id: str | None = None,
) -> ContinuationContext:
    """Reject mappings/duck types even when carried in a private request attribute."""
    if type(value) is not ContinuationContext:
        raise ContinuationContextDenied("live continuation context required")
    value.validate(session_id, request_id)
    return value
