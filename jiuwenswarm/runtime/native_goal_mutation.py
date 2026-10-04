"""Local final-delivery proof; never serialized or used as execution authority."""
from dataclasses import dataclass
from typing import Callable

from jiuwenswarm.governance.contracts import TrustedIdentity


@dataclass(frozen=True, repr=False)
class NativeGoalMutationDelivery:
    session_id: str
    identity: TrustedIdentity
    final_check: Callable[[], None]
