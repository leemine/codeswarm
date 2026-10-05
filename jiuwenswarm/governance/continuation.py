"""Private continuation facts, never credentials, Provider state or a wire grant.

A constructed proof is not authority: the host compiler must revalidate it at
publication and every subsequent execution/resource boundary. Runtime owns the
transaction, token scope, Binding and lifecycle; this module owns none of them.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field

from .contracts import TrustedIdentity
from .session_sharing import SessionHistoryRange


def _text(value, name, *, maximum=200, empty=False):
    if (not isinstance(value, str) or len(value) > maximum
            or value != value.strip() or (not empty and not value)
            or any(ord(char) < 32 for char in value)):
        raise ValueError(f'invalid continuation {name}')
    return value


@dataclass(frozen=True, slots=True)
class ContinuationInput:
    session_id: str
    share_id: str
    expected_revision: int
    create_token: str
    target_project_id: str
    execution_profile_id: str
    mode: str = 'agent'
    title: str = ''
    model_name: str = ''

    def __post_init__(self):
        for name in ('session_id', 'share_id', 'create_token', 'target_project_id', 'execution_profile_id'):
            _text(getattr(self, name), name)
        _text(self.title, 'title', maximum=100, empty=True)
        _text(self.model_name, 'model_name', empty=True)
        if type(self.expected_revision) is not int or not 1 <= self.expected_revision < 2 ** 63:
            raise ValueError('invalid continuation expected_revision')
        # Further modes require their own real Provider/Team acceptance.
        if self.mode not in ('agent', 'code', 'agent.work.normal', 'agent.code.normal', 'code.normal'):
            raise ValueError('unsupported continuation mode')

    @classmethod
    def from_wire(cls, params):
        if not isinstance(params, dict) or set(params) - set(cls.__dataclass_fields__):
            raise ValueError('invalid continuation request fields')
        try:
            return cls(**params)
        except TypeError as exc:
            raise ValueError('missing or invalid continuation fields') from exc


@dataclass(frozen=True, slots=True)
class ContinuationProof:
    request: ContinuationInput
    identity: TrustedIdentity
    source_owner: TrustedIdentity
    owner_revision: int
    source_revision: int
    share_revision: int
    history: SessionHistoryRange
    expires_at: float | None
    source_expires_at: float | None
    grantor: TrustedIdentity
    parent_share_id: str | None
    parent_revision: int | None
    target_revision: int


@dataclass(frozen=True, slots=True)
class ContinuationMessage:
    role: str
    content: str = field(repr=False)

    def __post_init__(self):
        if self.role not in ('user', 'assistant') or not isinstance(self.content, str):
            raise ValueError('continuation accepts only user/assistant text')


@dataclass(frozen=True, slots=True)
class ContinuationSeed:
    proof: ContinuationProof
    messages: tuple[ContinuationMessage, ...] = field(repr=False)
    digest: str = field(init=False)
    format_version: int = field(default=1, init=False)

    def __post_init__(self):
        if not isinstance(self.proof, ContinuationProof) or type(self.messages) is not tuple or any(
            not isinstance(message, ContinuationMessage) for message in self.messages
        ):
            raise TypeError('frozen host continuation facts required')
        material = json.dumps({'version': self.format_version, 'proof': asdict(self.proof),
                               'messages': [asdict(message) for message in self.messages]},
                              sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)
        object.__setattr__(self, 'digest', hashlib.sha256(material.encode()).hexdigest())
