"""Trusted Runtime identity bridge for durable Session owner publication."""
from contextlib import contextmanager
from contextvars import ContextVar

from .contracts import TrustedIdentity
from .session_sharing import SessionSharingDenied


class SessionOwnerPublication:
    def __init__(self, host):
        self.host = host
        self._identity = ContextVar('session_owner_publication_identity', default=None)

    @contextmanager
    def scope(self, resolver):
        token = self._identity.set(resolver)
        try:
            yield
        finally:
            self._identity.reset(token)

    def before_publish(self, session_id, project_id, created):
        resolver = self._identity.get()
        identity = resolver() if callable(resolver) else None
        if not isinstance(identity, TrustedIdentity):
            raise SessionSharingDenied('trusted Runtime publication identity required')
        decision = self.host._storage.authorize(project_id, identity.actor_id, 'execute')
        if not decision.allowed or decision.revision < 1:
            raise SessionSharingDenied('managed project execution authority required')
        if not created:
            if not self.host.owner_current(session_id, identity):
                raise SessionSharingDenied('existing Session has no matching trusted owner')
            return None
        epoch = self.host.register_owner_and_source(session_id, identity, project_id)
        # register_owner_and_source used expected_owner_revision=0. A
        # successful CAS therefore already proves revision 1; do not perform
        # another fallible storage read before returning the owned receipt.
        owner_revision = 1

        def rollback():
            self.host.compensate_owner_registration(session_id, identity,
                expected_revision=owner_revision, expected_epoch=epoch)
        return rollback
