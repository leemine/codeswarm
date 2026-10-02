# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Swarm domain identity and project authorization; never wire credentials."""
from dataclasses import dataclass
from typing import Literal, Protocol

ProjectAction = Literal["read", "write", "execute", "admin"]


@dataclass(frozen=True, slots=True)
class TrustedIdentity:
    """Identity supplied by an authenticated host boundary, never request data."""
    actor_id: str
    subject_id: str
    authority: str

    def __post_init__(self) -> None:
        for name in ("actor_id", "subject_id", "authority"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip() or value != value.strip():
                raise ValueError(f"{name} must be a nonempty normalized string")


@dataclass(frozen=True, slots=True)
class AuthorizationDecision:
    allowed: bool
    project_id: str
    actor_id: str
    action: ProjectAction
    revision: int
    reason: str = ""


class ProjectAuthorizer(Protocol):
    def authorize(
        self, project_id: str, actor_id: str, action: ProjectAction,
    ) -> AuthorizationDecision: ...
