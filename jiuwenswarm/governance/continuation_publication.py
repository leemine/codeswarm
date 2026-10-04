"""Lexical, Task-owned privilege for publishing one new continuation Session."""
from __future__ import annotations

import asyncio
import secrets
from contextvars import ContextVar

from .continuation import ContinuationSeed
from .session_sharing import SessionSharingDenied

_CURRENT = ContextVar('continuation_publication', default=None)


def current_scope():
    """Do not drop an inherited inactive scope into the ordinary creation path."""
    return _CURRENT.get()


class ContinuationPublication:
    def __init__(self, host, compiler, seed: ContinuationSeed, config_fingerprint: str):
        if not isinstance(seed, ContinuationSeed) or compiler.host is not host:
            raise TypeError('matching host compiler and immutable seed required')
        if (not isinstance(config_fingerprint, str) or len(config_fingerprint) != 64
                or any(c not in '0123456789abcdef' for c in config_fingerprint)):
            raise ValueError('exact configuration fingerprint required')
        self.host, self.compiler, self.seed = host, compiler, seed
        self.config_fingerprint = config_fingerprint
        self.publication_id = secrets.token_hex(32)
        self.session_id = None
        self._active = False
        self._task = None
        self._token = None
        self._used = False

    def _require(self):
        try:
            task = asyncio.current_task()
        except RuntimeError:
            task = None
        if (not self._active or task is None or self._task is not task or task.cancelling()
                or current_scope() is not self):
            raise SessionSharingDenied('current publication Task required')
        self.compiler.revalidate(self.seed.proof)

    def __enter__(self):
        if self._used or current_scope() is not None:
            raise SessionSharingDenied('publication scope cannot be reused or nested')
        try:
            task = asyncio.current_task()
        except RuntimeError:
            task = None
        if task is None:
            raise SessionSharingDenied('publication requires its owning async Task')
        self._task, self._active, self._used = task, True, True
        self._token = _CURRENT.set(self)
        try:
            self._require()
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, *_):
        self._active, self._task = False, None
        if self._token is not None:
            _CURRENT.reset(self._token)
            self._token = None

    def write_seed(self):
        from jiuwenswarm.server.runtime.session.continuation_publication import write_seed
        return write_seed(self)

    def commit(self):
        from jiuwenswarm.server.runtime.session.continuation_publication import commit
        return commit(self)
