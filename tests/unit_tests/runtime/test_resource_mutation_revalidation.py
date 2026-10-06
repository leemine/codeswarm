"""An accepted resource mutation must visit every original Session owner.

Uses real Coordinator registration, authority watchers and close/retry paths.
Only the host Provider release is synthetic; no model or Provider is started.
"""

import asyncio

import pytest

from jiuwenswarm.runtime.session.coordinator import RuntimeSessionCoordinator
from jiuwenswarm.runtime.session.model import RuntimeSessionState


class OriginalAuthority:
    def __init__(self, coordinator, record, *, fail_exit):
        self.coordinator = coordinator
        self.record = record
        self.scope = object()
        self.revoked = False
        self.fail_exit = fail_exit
        self.release_calls = 0

    def check_owner(self):
        assert self.coordinator._sessions[self.record.session_id] is self.record

    def check_authority(self):
        self.check_owner()
        if self.revoked:
            raise PermissionError('synthetic original resource revoked')

    async def release(self):
        self.check_owner()
        self.release_calls += 1
        if self.fail_exit:
            raise RuntimeError('synthetic original Provider exit unconfirmed')


@pytest.mark.asyncio
@pytest.mark.parametrize('second_fails', [False, True])
async def test_resource_revalidation_fences_later_owner_despite_first_exit_failure(second_fails):
    coordinator = RuntimeSessionCoordinator(cancel_timeout=0.2)
    authorities = []
    for sid, fail_exit in [('original-first', True), ('original-second', second_fails)]:
        snapshot = await coordinator.register_session(sid, 'web')
        record = coordinator._sessions[sid]
        authority = OriginalAuthority(coordinator, record, fail_exit=fail_exit)
        coordinator.watch_session_authority(sid, generation=snapshot.generation,
                                            authority=authority, interval=3600)
        authorities.append(authority)
    # Let the existing monitors do their initial valid read and park. The
    # explicit mutation callback below must do the work, not a timer retry.
    await asyncio.sleep(0)
    for authority in authorities:
        authority.revoked = True
    try:
        with pytest.raises((RuntimeError, ExceptionGroup)) as caught:
            await coordinator.revalidate_session_authorities()
        first, second = authorities
        assert first.release_calls == 1
        assert second.release_calls == 1
        assert first.record.authority_close_started
        assert second.record.authority_close_started
        assert first.record.state is RuntimeSessionState.QUIESCING
        assert second.record.state is (RuntimeSessionState.QUIESCING if second_fails
                                       else RuntimeSessionState.CLOSED)
        if second_fails:
            assert isinstance(caught.value, ExceptionGroup)
            assert len(caught.value.exceptions) == 2
        else:
            assert type(caught.value) is RuntimeError
        # Retry retains the same records and only retries unconfirmed exits.
        for authority in authorities:
            authority.fail_exit = False
        await coordinator.revalidate_session_authorities()
        assert first.release_calls == 2
        assert second.release_calls == (2 if second_fails else 1)
        assert all(a.record.state is RuntimeSessionState.CLOSED for a in authorities)
    finally:
        for authority in authorities:
            authority.record.authority_task.cancel()
        await asyncio.gather(*(a.record.authority_task for a in authorities), return_exceptions=True)
