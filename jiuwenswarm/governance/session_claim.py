"""Private Runtime provenance for the existing Session creation token cache."""
import asyncio
import hashlib
import json
import os
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass

from .contracts import TrustedIdentity
from .session_sharing import SessionSharingDenied


@dataclass
class _Claim:
    identity: TrustedIdentity
    fingerprint: str
    resolver: object
    task: object
    active: bool = True

    def check(self):
        if (not self.active or self.task is not asyncio.current_task()
                or self.task.done() or self.task.cancelling()
                or self.resolver() != self.identity):
            raise SessionSharingDenied('Session creation identity changed')
        return self.identity, self.fingerprint


_CLAIM = ContextVar('runtime_session_create_claim', default=None)


@contextmanager
def session_create_claim_scope(resolver, provision_input, *, continuation_input=None):
    """Bind the complete normalized input to one trusted live Runtime task."""
    identity = resolver()
    if not isinstance(identity, TrustedIdentity):
        raise SessionSharingDenied('trusted Session creation identity required')
    inputs = asdict(provision_input)
    for key in ('cwd', 'project_dir'):
        if isinstance(inputs.get(key), os.PathLike):
            inputs[key] = os.fsdecode(inputs[key])
    if continuation_input is not None:
        inputs = {'create': inputs, 'continuation': asdict(continuation_input)}
    serialized = json.dumps(inputs, sort_keys=True, separators=(',', ':'), allow_nan=False)
    claim = _Claim(identity, hashlib.sha256(serialized.encode()).hexdigest(), resolver, asyncio.current_task())
    if claim.task is None:
        raise SessionSharingDenied('Session creation task required')
    token = _CLAIM.set(claim)
    try:
        claim.check()
        yield
    finally:
        claim.active = False
        _CLAIM.reset(token)


def current_session_create_claim():
    claim = _CLAIM.get()
    return claim.check() if claim is not None else None
