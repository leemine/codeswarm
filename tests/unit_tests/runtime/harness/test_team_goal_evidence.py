"""Read-only root evidence on the original Team Round and Runtime registry."""
from dataclasses import replace

import pytest
from openjiuwen.harness_protocol import (
    HarnessEvent, TurnEventKind, TurnLifecycleEvent, TurnUsage, UsageUpdatedEvent,
    UsageUpdateMode, TurnResult, TurnStatus, TurnError, TurnTermination, TurnTerminationKind,
)
from jiuwenswarm.runtime.harness.goal_evidence import GoalAttemptIdentity
from jiuwenswarm.runtime.harness.team_goal_evidence import (
    TeamGoalSource, TeamGoalAttemptEvidence, TeamGoalEventObserver,
)
from tests.unit_tests.runtime.harness.test_goal_evidence import output


def identity(index=1, generation=7):
    return GoalAttemptIdentity('goal', 2, index, 'execution', generation)


def collector(**kwargs):
    return TeamGoalAttemptEvidence(identity(), session_id='root', request_id='request',
                                   is_current=lambda: True, **kwargs)


def source(member='leader', product=False):
    return TeamGoalSource('root', member, f'{member}-child' if product else member,
                         'agent', member, product)


def event(src, seq, payload, turn='same-turn', provider='provider'):
    return HarnessEvent(seq, 1.0, payload, src.host_session_id, src.agent_id,
                        turn_id=turn, provider_session_id=provider)


def finished(usage=None, kind=TurnEventKind.FINISHED):
    status = {TurnEventKind.FINISHED: TurnStatus.COMPLETED,
              TurnEventKind.FAILED: TurnStatus.FAILED, TurnEventKind.ABORTED: TurnStatus.INTERRUPTED}[kind]
    return TurnLifecycleEvent(kind, TurnResult(
        status, usage=usage,
        error=TurnError('provider failed') if status is TurnStatus.FAILED else None,
        termination=TurnTermination(TurnTerminationKind.USER_ABORT) if status is TurnStatus.INTERRUPTED else None,
    ))


@pytest.mark.asyncio
async def test_all_members_and_product_child_sum_once_only_after_explicit_round_seal():
    target = collector()
    for src in (source(), source('worker'), source('worker', True)):
        observer = TeamGoalEventObserver(src, lambda: target)
        events = [event(src, 1, TurnLifecycleEvent(TurnEventKind.STARTED)),
                  event(src, 2, UsageUpdatedEvent(TurnUsage(input_tokens=3, output_tokens=1), UsageUpdateMode.DELTA)),
                  event(src, 3, UsageUpdatedEvent(TurnUsage(input_tokens=7, output_tokens=2), UsageUpdateMode.DELTA)),
                  event(src, 4, output(src.host_session_id)),
                  event(src, 5, finished(TurnUsage(input_tokens=10, output_tokens=3, total_tokens=13)))]
        for item in events + events:  # exact replay never bills again
            await observer(item)
    assert target.turn_count == 3 and target.all_terminal and target.usage_complete
    assert not target.ready_for_assessment and target.take_usage_delta() is None
    target.seal('completed')
    assert target.ready_for_assessment
    usage = target.take_usage_delta()
    assert (usage.input_tokens, usage.output_tokens, usage.total_tokens) == (30, 9, 39)
    assert target.take_usage_delta() is None
    assert '/product/' in target.transcript and '/member/' in target.transcript


@pytest.mark.asyncio
@pytest.mark.parametrize('reason', ['missing', 'total_only', 'decreased', 'unfinished', 'failed', 'aborted', 'eof'])
async def test_incomplete_or_unsuccessful_round_cannot_be_assessed_as_success(reason):
    target = collector(); src = source(); observer = TeamGoalEventObserver(src, lambda: target)
    await observer(event(src, 1, TurnLifecycleEvent(TurnEventKind.STARTED)))
    if reason == 'decreased':
        await observer(event(src, 2, UsageUpdatedEvent(TurnUsage(input_tokens=20, output_tokens=3))))
    if reason == 'eof':
        observer.close()
    elif reason != 'unfinished':
        usage = (None if reason == 'missing' else TurnUsage(total_tokens=13) if reason == 'total_only'
                 else TurnUsage(input_tokens=10, output_tokens=3))
        kind = {'failed': TurnEventKind.FAILED, 'aborted': TurnEventKind.ABORTED}.get(reason, TurnEventKind.FINISHED)
        await observer(event(src, 3, finished(usage, kind)))
    target.seal('completed')
    assert not target.ready_for_assessment
    if reason not in {'failed', 'aborted'}:
        assert target.take_usage_delta() is None
    else:
        assert target.take_usage_delta().total_tokens == 13  # failed work still costs tokens


@pytest.mark.asyncio
async def test_late_old_turn_is_never_reassigned_to_replacement_attempt():
    old = collector(); current = old; src = source()
    observer = TeamGoalEventObserver(src, lambda: current)
    await observer(event(src, 1, TurnLifecycleEvent(TurnEventKind.STARTED), turn='old'))
    old.release()
    current = TeamGoalAttemptEvidence(identity(2), session_id='root', request_id='request-2', is_current=lambda: True)
    await observer(event(src, 2, TurnLifecycleEvent(TurnEventKind.STARTED), turn='new'))
    await observer(event(src, 3, finished(TurnUsage(input_tokens=1000, output_tokens=1000)), turn='old'))
    await observer(event(src, 4, finished(TurnUsage(input_tokens=2, output_tokens=1)), turn='new'))
    assert current.turn_count == 1
    current.seal('completed')
    assert current.take_usage_delta().total_tokens == 3
    assert old.take_usage_delta() is None
    assert 'released' in old.error


@pytest.mark.asyncio
async def test_cancellation_retains_old_turn_usage_but_cannot_start_or_assess_new_work():
    live = True
    target = TeamGoalAttemptEvidence(identity(), session_id='root', request_id='request', is_current=lambda: live)
    src = source(); observer = TeamGoalEventObserver(src, lambda: target)
    await observer(event(src, 1, TurnLifecycleEvent(TurnEventKind.STARTED)))
    live = False
    await observer(event(src, 2, TurnLifecycleEvent(TurnEventKind.STARTED), turn='late-start'))
    await observer(event(src, 3, finished(TurnUsage(input_tokens=4, output_tokens=2), TurnEventKind.ABORTED)))
    assert target.turn_count == 1 and target.all_terminal
    with pytest.raises(RuntimeError):
        target.seal('completed')
    target.seal('cancelled')
    assert not target.ready_for_assessment
    assert target.take_usage_delta().total_tokens == 6


@pytest.mark.asyncio
@pytest.mark.parametrize('bad', ['host', 'agent', 'provider', 'turn_start', 'source_root'])
async def test_missing_or_mismatched_attribution_cannot_yield_complete_usage(bad):
    target = collector(); src = source()
    if bad == 'source_root':
        src = replace(src, root_session_id='victim')
    observer = TeamGoalEventObserver(src, lambda: target)
    item = event(src, 1, TurnLifecycleEvent(TurnEventKind.STARTED))
    if bad == 'host': item = replace(item, host_session_id='victim')
    if bad == 'agent': item = replace(item, agent_id='victim')
    if bad == 'provider': item = replace(item, provider_session_id=None)
    if bad == 'turn_start': item = event(src, 1, finished(TurnUsage(input_tokens=2, output_tokens=1)))
    await observer(item)
    target.seal('completed')
    assert target.error and not target.ready_for_assessment
    assert target.take_usage_delta() is None


@pytest.mark.asyncio
@pytest.mark.parametrize('budget', ['turns', 'transcript', 'observer'])
async def test_evidence_budgets_fail_closed_without_unbounded_growth(budget):
    target = collector(max_turns=1 if budget == 'turns' else 3, max_transcript_chars=10 if budget == 'transcript' else 64000)
    src = source(); observer = TeamGoalEventObserver(src, lambda: target, max_tracked_turns=1 if budget == 'observer' else 5)
    await observer(event(src, 1, TurnLifecycleEvent(TurnEventKind.STARTED)))
    if budget == 'transcript':
        await observer(event(src, 2, output('x' * 11)))
    else:
        await observer(event(src, 2, TurnLifecycleEvent(TurnEventKind.STARTED), turn='next'))
    target.seal('completed')
    assert target.error and not target.ready_for_assessment
    assert target.turn_count <= (1 if budget == 'turns' else 3)
    assert target.error == {
        'turns': 'team_goal_turn_budget_exceeded',
        'transcript': 'team_goal_transcript_budget_exceeded',
        'observer': 'team_goal_source_turn_budget_exceeded',
    }[budget]
    assert len(target.transcript) <= (10 if budget == 'transcript' else 64000)


@pytest.mark.asyncio
async def test_new_provider_cycle_can_reuse_turn_id_without_reusing_old_evidence():
    target = collector()
    for src in (source(), source()):
        observer = TeamGoalEventObserver(src, lambda: target)
        await observer(event(src, 1, TurnLifecycleEvent(TurnEventKind.STARTED)))
        await observer(event(src, 2, finished(TurnUsage(input_tokens=1, output_tokens=1))))
        observer.close()
    target.seal('completed')
    assert target.turn_count == 2 and target.take_usage_delta().total_tokens == 4


@pytest.mark.asyncio
@pytest.mark.parametrize('mismatch', ['generation', 'execution', 'round', 'lease', 'kind', 'attempt', 'release', 'lease_after'])
async def test_original_runtime_and_round_are_the_only_binding_authority(mismatch):
    from jiuwenswarm.agents.harness.team.team_manager import TeamManager
    from jiuwenswarm.runtime.session import RuntimeSessionCoordinator, SessionWorkKind
    coordinator = RuntimeSessionCoordinator(); manager = TeamManager()
    await coordinator.register_session('root', 'web')
    async def operation():
        owner = coordinator.external_execution_owner('root', 'request')
        await coordinator.acquire_external_execution(owner, goal=True)
        manager.begin_round('root', 'request', defer_terminal_release=mismatch != 'round')
        valid = GoalAttemptIdentity('goal', 0, 1, owner.execution_id, owner.generation)
        proposed = replace(valid, generation=99) if mismatch == 'generation' else replace(valid, execution_id='wrong') if mismatch == 'execution' else valid
        if mismatch == 'lease': coordinator.release_external_execution(owner)
        if mismatch in {'attempt', 'release', 'lease_after'}:
            evidence = manager.bind_goal_attempt_evidence('root', 'request', identity=valid, runtime=coordinator)
            assert manager.bind_goal_attempt_evidence('root', 'request', identity=valid, runtime=coordinator) is evidence
            if mismatch == 'attempt': proposed = replace(valid, attempt_index=2)
            elif mismatch == 'lease_after':
                coordinator.release_external_execution(owner)
                assert not evidence.accepting and not evidence.ready_for_assessment
            else:
                await manager.release_round('root', 'request')
                assert not evidence.accepting and not evidence.ready_for_assessment
        with pytest.raises((ValueError, RuntimeError)):
            manager.bind_goal_attempt_evidence('root', 'request', identity=proposed, runtime=coordinator)
        await manager.release_current_round('root')
        coordinator.release_external_execution(owner)
    try:
        await coordinator.run_unary('root', 'request',
            SessionWorkKind.CHAT_UNARY if mismatch == 'kind' else SessionWorkKind.GOAL_STREAM, operation)
    finally:
        await coordinator.close()


@pytest.mark.asyncio
async def test_new_sequence_cannot_reuse_an_ended_turn_identity_and_hide_its_usage():
    target = collector(); src = source(); observer = TeamGoalEventObserver(src, lambda: target)
    await observer(event(src, 1, TurnLifecycleEvent(TurnEventKind.STARTED)))
    await observer(event(src, 2, finished(TurnUsage(input_tokens=2, output_tokens=1))))
    await observer(event(src, 3, TurnLifecycleEvent(TurnEventKind.STARTED)))
    target.seal('completed')
    assert target.error == 'team_goal_reused_turn_identity'
    assert target.take_usage_delta() is None and not target.ready_for_assessment


@pytest.mark.asyncio
async def test_turn_started_before_goal_binding_is_not_adopted_by_new_round():
    current = None
    src = source()
    observer = TeamGoalEventObserver(src, lambda: current)
    await observer(event(src, 1, TurnLifecycleEvent(TurnEventKind.STARTED), turn='ordinary'))
    current = collector()
    await observer(event(src, 2, finished(TurnUsage(input_tokens=100, output_tokens=100)), turn='ordinary'))
    await observer(event(src, 3, TurnLifecycleEvent(TurnEventKind.STARTED), turn='goal'))
    await observer(event(src, 4, finished(TurnUsage(input_tokens=2, output_tokens=1)), turn='goal'))
    current.seal('completed')
    assert current.ready_for_assessment and current.turn_count == 1
    assert current.take_usage_delta().total_tokens == 3
