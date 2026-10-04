"""Actual Provisioner phases recheck the caller's original delete authority."""
from unittest.mock import AsyncMock

import pytest

from tests.unit_tests.runtime.test_runtime_session_provisioner import env  # noqa: F401


async def guarded_delete(env, guard):
    return await env.runtime._session_provisioner.delete_session(
        channel_id='web', session_id='owned',
        quiesce_session=env.runtime._quiesce_agent_session_for_delete,
        dispose_session=env.runtime._dispose_agent_session_after_resource_release,
        _cleanup_guard=guard, _cleanup_descriptor=dict(env.metadata),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize('phase', ['before', 'release', 'lifecycle', 'runner', 'commit'])
async def test_changed_authority_after_await_blocks_next_destructive_phase(env, monkeypatch, phase):
    path = env.root / 'owned'
    path.mkdir()
    valid = True

    def guard():
        if not valid:
            raise PermissionError('original authority changed')

    async def invalidate(*_, **__):
        nonlocal valid
        env.events.append('invalidate.' + phase)
        valid = False

    if phase == 'before':
        env.participant.before_delete = invalidate
    elif phase == 'release':
        env.participant.release_resources = invalidate
    elif phase == 'lifecycle':
        env.runtime._session_provisioner._delete_lifecycle.begin_session_delete = invalidate
    elif phase == 'runner':
        monkeypatch.setattr('openjiuwen.core.runner.Runner.release', invalidate)
    else:
        env.runtime._session_provisioner._delete_lifecycle.commit_session_delete = invalidate
    if phase == 'before':
        with pytest.raises(PermissionError, match='authority changed'):
            await guarded_delete(env, guard)
    else:
        result = await guarded_delete(env, guard)
        assert not result.ok
        if phase == 'commit':
            assert result.deleted and result.recovery_required
            assert result.error_code == 'DELETE_COMMIT_PENDING'
    assert path.exists() is (phase != 'commit')
    if phase in {'before', 'release', 'lifecycle'}:
        assert 'runner.release' not in env.events
        assert 'dispose.agent' not in env.events
    assert 'participant.committed' not in env.events


@pytest.mark.asyncio
async def test_guarded_participant_release_failure_cannot_be_a_success(env):
    path = env.root / 'owned'
    path.mkdir()
    env.participant.release_resources = AsyncMock(side_effect=RuntimeError('resource still owned'))
    result = await guarded_delete(env, lambda: None)
    assert not result.ok and path.exists()
    assert 'dispose.agent' not in env.events and 'runner.release' not in env.events


@pytest.mark.asyncio
async def test_guard_rejection_after_first_participant_cannot_reach_second(env):
    from jiuwenswarm.runtime.session_lifecycle import RuntimeParticipantRegistry
    from tests.unit_tests.runtime.test_runtime_session_provisioner import Participant
    path = env.root / 'owned'
    path.mkdir()
    other = Participant([])
    other.before_delete = AsyncMock()
    registry = RuntimeParticipantRegistry()
    registry.replace_session_participants(activity=(), delete=(env.participant, other))
    env.runtime._session_provisioner._participant_registry = registry
    allowed = True
    async def first(_):
        nonlocal allowed
        allowed = False
    env.participant.before_delete = first
    def guard():
        if not allowed:
            raise PermissionError('revoked')
    with pytest.raises(PermissionError):
        await guarded_delete(env, guard)
    other.before_delete.assert_not_awaited()
    assert path.exists()
