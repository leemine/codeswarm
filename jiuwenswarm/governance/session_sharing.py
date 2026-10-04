"""Pure sharing contracts; history ranges are host-compiled admission facts.

Constructing a range does not establish that its byte boundaries are real JSONL
record boundaries. The host compiler/reader must validate those against the
persisted history, and must never turn an arbitrary client cursor into a grant.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Callable, Literal

from .contracts import TrustedIdentity
from .resources import normalized, valid_expiry

SharingAction = Literal['view', 'discuss', 'execute', 'approve', 'download', 'manage']
SHARING_ACTIONS = frozenset({'view', 'discuss', 'execute', 'approve', 'download', 'manage'})


class SessionSharingDenied(PermissionError):
    pass


class SessionSharingConflict(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class SessionHistoryRange:
    session_id: str
    stream: str
    dev: int
    ino: int
    snapshot_end: int
    start: int
    end: int

    def __post_init__(self):
        normalized(self.session_id, 'session_id')
        # This version deliberately supports only the product Session stream.
        expected = hashlib.sha256(f'{self.session_id}\0'.encode()).hexdigest()
        if self.stream != expected:
            raise ValueError('history range must bind the product Session stream')
        for field in ('dev', 'ino', 'snapshot_end', 'start', 'end'):
            if type(getattr(self, field)) is not int or getattr(self, field) < 0:
                raise ValueError('history positions must be nonnegative integers')
        if not self.start <= self.end <= self.snapshot_end:
            raise ValueError('history range lies outside snapshot')

    def contains(self, other: SessionHistoryRange) -> bool:
        return isinstance(other, SessionHistoryRange) and (
            self.session_id, self.stream, self.dev, self.ino
        ) == (other.session_id, other.stream, other.dev, other.ino) and (
            other.snapshot_end <= self.snapshot_end and self.start <= other.start <= other.end <= self.end
        )

    def intersection(self, other: SessionHistoryRange) -> SessionHistoryRange | None:
        if not isinstance(other, SessionHistoryRange) or (
            self.session_id, self.stream, self.dev, self.ino
        ) != (other.session_id, other.stream, other.dev, other.ino):
            return None
        start, end = max(self.start, other.start), min(self.end, other.end)
        if start > end:
            return None
        return SessionHistoryRange(self.session_id, self.stream, self.dev, self.ino,
                                   min(self.snapshot_end, other.snapshot_end), start, end)


@dataclass(frozen=True, slots=True)
class SessionSharingAuthority:
    """Current host authority, independently resolved on every operation.

    ``revision`` is a monotonic authorization epoch, advanced on revocation and
    restoration. It is not the append-only history size or routing metadata.
    A host must verify the current Session incarnation and trusted owner before
    producing this value. No implicit/default-project authorization is valid.
    """
    session_id: str
    owner: TrustedIdentity
    revision: int
    actions: frozenset[str]
    history: SessionHistoryRange
    expires_at: float | None = None

    def __post_init__(self):
        normalized(self.session_id, 'session_id')
        if not isinstance(self.owner, TrustedIdentity):
            raise TypeError('trusted owner required')
        if type(self.revision) is not int or self.revision < 1:
            raise ValueError('authority revision must be positive')
        object.__setattr__(self, 'actions', frozenset(self.actions))
        if not self.actions <= SHARING_ACTIONS:
            raise ValueError('unknown sharing action')
        if not isinstance(self.history, SessionHistoryRange) or self.history.session_id != self.session_id:
            raise ValueError('authority history belongs to a different Session')
        if not valid_expiry(self.expires_at):
            raise ValueError('invalid authority expiry')


SessionAuthorityResolver = Callable[[str], SessionSharingAuthority | None]


@dataclass(frozen=True, slots=True)
class SessionSharingDecision:
    allowed: bool
    session_id: str
    action: str
    share_id: str | None = None
    revision: int = 0
    reason: str = ''
