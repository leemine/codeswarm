"""Real owner sidecar publication, fixed source proof and bounded seed files."""
import asyncio
import copy
import json

import pytest

from jiuwenswarm.governance.continuation_publication import ContinuationPublication
from jiuwenswarm.governance.session_sharing import SessionSharingDenied, SessionSharingConflict
from jiuwenswarm.server.runtime.session import continuation_publication as publication
from jiuwenswarm.server.runtime.session.sharing_host import SharingHostService
from tests.unit_tests.server import test_continuation_source as source_tests

setup = source_tests.setup
ALICE, BOB = source_tests.ALICE, source_tests.BOB
FINGERPRINT = 'a' * 64
TARGET = {'model_binding_fingerprint': 'b' * 64, 'model_entry_fingerprint': 'c' * 64,
          'project_dir': '/synthetic/workspace', 'work_mode': 'work', 'mode': 'agent.work.normal'}


def make_scope(setup):
    return ContinuationPublication(setup.host, setup.compiler, setup.compiler.compile(setup.request), FINGERPRINT, target_snapshot=TARGET)


def register(setup, scope, sid='target-session'):
    assert setup.host.register_owner_and_source(sid, scope.seed.proof.identity, setup.target.project_id) == 1
    directory = setup.tmp_path / 'sessions' / sid
    directory.mkdir()
    metadata = {**{key: TARGET[key] for key in ('project_dir', 'work_mode', 'mode')}, 'project_id': setup.target.project_id, 'execution_profile_id': setup.request.execution_profile_id,
                'execution_config_fingerprint': FINGERPRINT, 'model': getattr(setup.request, 'model_name', '')}
    (directory / 'metadata.json').write_text(json.dumps(metadata))
    return directory


def new_host(setup):
    host = SharingHostService(lambda _, actor: {'alice': ALICE, 'bob': BOB}.get(actor),
                              known_actor=lambda identity: identity in {ALICE, BOB}, storage=setup.access)
    host.store._clock = lambda: setup.clock[0]
    return host


@pytest.mark.asyncio
async def test_pending_private_to_original_task_and_committed_survives_restart(setup):
    with make_scope(setup) as scope:
        directory = register(setup, scope)
        assert setup.host.owner_current('target-session', BOB)
        revision = setup.host.owner_revision('target-session', BOB)
        async def child():
            assert not setup.host.owner_current('target-session', BOB)
            with pytest.raises(SessionSharingDenied):
                scope.write_seed()
            with pytest.raises(SessionSharingDenied):
                setup.host.register_owner_and_source('unrelated', BOB, setup.target.project_id)
        await asyncio.create_task(child())
        assert not new_host(setup).owner_current('target-session', BOB)
        with pytest.raises(SessionSharingDenied):
            publication.read_seed(setup.host, 'target-session', BOB)
        scope.write_seed()
        raw = (directory / 'continuation.json').read_bytes()
        scope.write_seed()
        assert (directory / 'continuation.json').read_bytes() == raw
        scope.commit()
        scope.commit()
        assert setup.host.owner_revision('target-session', BOB) == revision
    host = new_host(setup)
    assert host.owner_current('target-session', BOB)
    assert publication.read_seed(host, 'target-session', BOB) == scope.seed
    assert not host.owner_current('target-session', ALICE)
    with pytest.raises(SessionSharingDenied):
        scope.commit()


@pytest.mark.asyncio
async def test_uncommitted_scope_exit_denies_all_ordinary_access(setup):
    with make_scope(setup) as scope:
        register(setup, scope)
        scope.write_seed()
    assert not setup.host.owner_current('target-session', BOB)
    assert not new_host(setup).owner_current('target-session', BOB)
    with pytest.raises(SessionSharingDenied):
        publication.read_seed(setup.host, 'target-session', BOB)


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['revoke', 'regrant', 'target', 'profile', 'fingerprint', 'model', 'expiry'])
async def test_committed_current_authority_never_uses_scope_privilege(setup, change):
    with make_scope(setup) as scope:
        directory = register(setup, scope)
        scope.write_seed()
        scope.commit()
        if change in ('revoke', 'regrant'):
            setup.host.store.revoke(setup.grant['share_id'], ALICE, expected_revision=1)
            if change == 'regrant':
                setup.host.store.grant('source-session', ALICE, BOB, actions={'view', 'execute'},
                                      history=setup.scope, expires_at=200)
        elif change == 'target':
            setup.access.replace_acl(setup.target.project_id, 'admin', acl={'bob': ['read']}, expected_revision=2)
        elif change == 'expiry':
            setup.clock[0] = 200
        else:
            metadata = json.loads((directory / 'metadata.json').read_text())
            field = {'profile': 'execution_profile_id', 'fingerprint': 'execution_config_fingerprint', 'model': 'model'}[change]
            metadata[field] = 'different'
            (directory / 'metadata.json').write_text(json.dumps(metadata))
        assert not setup.host.owner_current('target-session', BOB)
        with pytest.raises((SessionSharingDenied, ValueError)):
            publication.read_seed(setup.host, 'target-session', BOB)


@pytest.mark.asyncio
async def test_seed_failure_stays_pending_and_receipt_compensates(setup, monkeypatch):
    with make_scope(setup) as scope:
        register(setup, scope)
        monkeypatch.setattr(publication.lifecycle, 'atomic_json', lambda *a: (_ for _ in ()).throw(OSError('disk')))
        with pytest.raises(OSError):
            scope.write_seed()
        assert setup.host.compensate_owner_registration('target-session', BOB, expected_revision=1, expected_epoch=1) == 2
    assert not setup.host.owner_current('target-session', BOB)


@pytest.mark.asyncio
async def test_scope_cannot_commit_newer_owner_epoch(setup):
    with make_scope(setup) as scope:
        register(setup, scope)
        scope.write_seed()
        setup.host.invalidate_source('target-session', expected_epoch=1)
        before = copy.deepcopy(setup.access._load())
        with pytest.raises((SessionSharingDenied, SessionSharingConflict)):
            scope.commit()
        assert setup.access._load() == before
        with pytest.raises(SessionSharingDenied, match="invalidated"):
            setup.host.compensate_owner_registration('target-session', BOB, expected_revision=1, expected_epoch=1)


@pytest.mark.asyncio
@pytest.mark.parametrize('mutation', ['partial', 'hash', 'proof', 'extra', 'version', 'message', 'messages_limit', 'file_limit', 'symlink'])
async def test_persisted_seed_is_strict_bounded_and_hash_checked(setup, mutation, monkeypatch):
    with make_scope(setup) as scope:
        directory = register(setup, scope)
        scope.write_seed()
        scope.commit()
    path = directory / 'continuation.json'
    value = json.loads(path.read_text())
    if mutation == 'partial':
        path.write_text('{')
    elif mutation == 'symlink':
        saved = directory / 'saved.json'
        path.rename(saved)
        path.symlink_to(saved)
    elif mutation == 'file_limit':
        monkeypatch.setattr(publication, 'MAX_SEED_FILE_BYTES', 10)
    else:
        if mutation == 'hash':
            value['digest'] = 'b' * 64
        elif mutation == 'proof':
            value['proof']['identity']['authority'] = 'other'
        elif mutation == 'extra':
            value['credential'] = 'forbidden'
        elif mutation == 'version':
            value['version'] = True
        elif mutation == 'messages_limit':
            monkeypatch.setattr(publication, 'MAX_SEED_MESSAGES', 1)
        else:
            value['messages'][0]['tool_calls'] = []
        path.write_text(json.dumps(value))
    with pytest.raises((SessionSharingDenied, ValueError, OSError)):
        publication.read_seed(setup.host, 'target-session', BOB)


@pytest.mark.asyncio
async def test_no_seed_io_inside_sidecar_and_owner_guards_never_read_seed(setup, monkeypatch):
    with make_scope(setup) as scope:
        register(setup, scope)
        with setup.access._locked():
            with pytest.raises(SessionSharingDenied):
                scope.write_seed()
        scope.write_seed()
        with setup.access._locked():
            with pytest.raises(SessionSharingDenied):
                scope.commit()
        scope.commit()
    monkeypatch.setattr(publication, '_read_file', lambda *_: pytest.fail('owner checked seed file'))
    assert setup.host.owner_current('target-session', BOB)
    with setup.access._locked():
        with pytest.raises(SessionSharingDenied):
            publication.read_seed(setup.host, 'target-session', BOB)


@pytest.mark.asyncio
async def test_commit_revalidates_after_file_read_and_does_not_save_on_revoke(setup, monkeypatch):
    with make_scope(setup) as scope:
        register(setup, scope)
        scope.write_seed()
        original = publication._read_file
        def read(sid):
            seed = original(sid)
            setup.host.store.revoke(setup.grant['share_id'], ALICE, expected_revision=1)
            return seed
        monkeypatch.setattr(publication, '_read_file', read)
        with pytest.raises(SessionSharingDenied):
            scope.commit()
        assert setup.access._load()['session_sharing']['owners']['target-session']['continuation']['state'] == 'pending'


@pytest.mark.asyncio
async def test_one_scope_one_new_session_and_correct_owner(setup):
    with make_scope(setup) as scope:
        with pytest.raises(SessionSharingDenied):
            setup.host.register_owner_and_source('wrong', ALICE, setup.target.project_id)
        register(setup, scope)
        with pytest.raises(SessionSharingDenied):
            setup.host.register_owner_and_source('second', BOB, setup.target.project_id)
        with pytest.raises(SessionSharingDenied):
            with make_scope(setup):
                pass
    with pytest.raises(SessionSharingDenied):
        with scope:
            pass


def test_source_compiler_never_scans_unrelated_inventory(setup, monkeypatch):
    monkeypatch.setattr(setup.host.store, 'list_for_actor', lambda *_: pytest.fail('unrelated inventory read'))
    assert setup.compiler.compile(setup.request).messages


@pytest.mark.asyncio
async def test_malformed_persisted_proof_denies_without_seed_io(setup, monkeypatch):
    with make_scope(setup) as scope:
        register(setup, scope)
        scope.write_seed()
        scope.commit()
    data = setup.access._load()
    data['session_sharing']['owners']['target-session']['continuation']['proof']['target_revision'] = True
    setup.access._save(data)
    monkeypatch.setattr(publication, '_read_file', lambda *_: pytest.fail('malformed proof reached file'))
    assert not setup.host.owner_current('target-session', BOB)


@pytest.mark.asyncio
async def test_same_task_pending_does_not_authorize_worker_thread(setup):
    with make_scope(setup) as scope:
        register(setup, scope)
        assert not await asyncio.to_thread(setup.host.owner_current, 'target-session', BOB)
        with pytest.raises(SessionSharingDenied):
            await asyncio.to_thread(scope.write_seed)


@pytest.mark.asyncio
async def test_committed_target_rejects_old_prepare_abort_receipt(setup):
    with make_scope(setup) as scope:
        register(setup, scope)
        scope.write_seed()
        scope.commit()
    before = copy.deepcopy(setup.access._load())
    with pytest.raises(SessionSharingConflict, match='committed continuation'):
        setup.host.compensate_owner_registration('target-session', BOB, expected_revision=1, expected_epoch=1)
    assert setup.access._load() == before
    assert setup.host.owner_current('target-session', BOB)


@pytest.mark.asyncio
async def test_read_seed_rechecks_authority_after_actual_file_read(setup, monkeypatch):
    with make_scope(setup) as scope:
        register(setup, scope)
        scope.write_seed()
        scope.commit()
    original = publication._read_file
    def read(sid):
        seed = original(sid)
        setup.host.store.revoke(setup.grant['share_id'], ALICE, expected_revision=1)
        return seed
    monkeypatch.setattr(publication, '_read_file', read)
    with pytest.raises(SessionSharingDenied):
        publication.read_seed(setup.host, 'target-session', BOB)


@pytest.mark.asyncio
async def test_seed_different_digest_cannot_be_overwritten(setup):
    with make_scope(setup) as scope:
        directory = register(setup, scope)
        scope.write_seed()
        path = directory / 'continuation.json'
        raw = json.loads(path.read_text())
        raw['messages'][0]['content'] = 'changed'
        path.write_text(json.dumps(raw))
        before = path.read_bytes()
        with pytest.raises(SessionSharingDenied, match='digest'):
            scope.write_seed()
        assert path.read_bytes() == before
        with pytest.raises(SessionSharingDenied, match='digest'):
            scope.commit()


@pytest.mark.asyncio
async def test_commit_file_read_then_nonce_cas_rejects_changed_reservation(setup, monkeypatch):
    with make_scope(setup) as scope:
        register(setup, scope)
        scope.write_seed()
        original = publication._read_file
        def read(sid):
            seed = original(sid)
            data = setup.access._load()
            data['session_sharing']['owners']['target-session']['continuation']['publication_id'] = 'f' * 64
            setup.access._save(data)
            return seed
        monkeypatch.setattr(publication, '_read_file', read)
        with pytest.raises(SessionSharingDenied, match='incomplete'):
            scope.commit()
        assert setup.access._load()['session_sharing']['owners']['target-session']['continuation']['state'] == 'pending'


@pytest.mark.asyncio
async def test_source_chain_depth_is_bounded_without_seed_reads(setup, monkeypatch):
    with make_scope(setup) as scope:
        register(setup, scope)
        scope.write_seed()
        scope.commit()
    monkeypatch.setattr(publication, 'MAX_SOURCE_DEPTH', 0)
    assert not setup.host.owner_current('target-session', BOB)
    with pytest.raises(SessionSharingDenied, match='depth'):
        setup.host.owner_revision('target-session', BOB)


@pytest.mark.asyncio
async def test_committed_two_generation_chain_and_source_cycle_reject(setup):
    from dataclasses import replace
    from jiuwenswarm.server.runtime.session.continuation import ContinuationCompiler
    setup.access.replace_acl(setup.target.project_id, 'admin', acl={'bob': ['read', 'execute', 'admin']}, expected_revision=2)
    with make_scope(setup) as scope:
        directory = register(setup, scope)
        scope.write_seed()
        scope.commit()
    (directory / 'history.jsonl').write_text('{"role":"user","content":"derived-visible"}\n')
    source_range = setup.host.prepare_source('target-session', BOB)
    grant = setup.host.store.grant('target-session', BOB, ALICE, actions={'view', 'execute'},
                                 history=source_range, expires_at=190)
    compiler = ContinuationCompiler(setup.host, identity_resolver=lambda: ALICE, project_authorizer=setup.access)
    request = replace(setup.request, session_id='target-session', share_id=grant['share_id'],
                      target_project_id=setup.source.project_id, create_token='second')
    seed = compiler.compile(request)
    with ContinuationPublication(setup.host, compiler, seed, FINGERPRINT, target_snapshot=TARGET) as second:
        setup.host.register_owner_and_source('second-session', ALICE, setup.source.project_id)
        second_dir = setup.tmp_path / 'sessions/second-session'
        second_dir.mkdir()
        (second_dir / 'metadata.json').write_text(json.dumps({**{key: TARGET[key] for key in ('project_dir', 'work_mode', 'mode')}, 'project_id': setup.source.project_id,
            'execution_profile_id': request.execution_profile_id, 'execution_config_fingerprint': FINGERPRINT,
            'model': request.model_name}))
        second.write_seed()
        second.commit()
    assert new_host(setup).owner_current('second-session', ALICE)
    assert publication.read_seed(setup.host, 'second-session', ALICE) == seed
    # Persisted provenance is never itself authority; a forged self-cycle is
    # rejected before seed/history IO, rather than recursively trusting records.
    data = setup.access._load()
    item = data['session_sharing']['owners']['target-session']['continuation']['proof']
    item['request']['session_id'] = 'target-session'
    item['request']['share_id'] = grant['share_id']
    item['history'] = copy.deepcopy(data['session_sharing']['shares'][grant['share_id']]['history'])
    setup.access._save(data)
    assert not setup.host.owner_current('target-session', BOB)
    assert not setup.host.owner_current('second-session', ALICE)


@pytest.mark.asyncio
@pytest.mark.parametrize('field,value', [('identity', {'actor_id': 'bob', 'subject_id': '', 'authority': 'organization'}),
                                        ('target_revision', True), ('history', {'wrong': 'range'}),
                                        ('parent_share_id', [])])
async def test_malformed_proof_has_permission_denial_contract(setup, field, value):
    with make_scope(setup) as scope:
        register(setup, scope)
        scope.write_seed()
        scope.commit()
    data = setup.access._load()
    data['session_sharing']['owners']['target-session']['continuation']['proof'][field] = value
    setup.access._save(data)
    with pytest.raises(SessionSharingDenied):
        setup.host.owner_revision('target-session', BOB)


def test_exact_share_lookup_rejects_mismatched_persisted_id(setup):
    data = setup.access._load()
    data['session_sharing']['shares'][setup.grant['share_id']]['share_id'] = 'different'
    setup.access._save(data)
    with pytest.raises(SessionSharingDenied, match='share unavailable'):
        setup.compiler.compile(setup.request)


@pytest.mark.asyncio
@pytest.mark.parametrize('state', ['pending', 'committed', 'retired'])
@pytest.mark.parametrize('changed_input', [False, True])
async def test_same_actor_token_is_atomic_reservation_across_publication_scopes(setup, state, changed_input):
    from dataclasses import replace
    with make_scope(setup) as first:
        register(setup, first)
        if state == 'committed':
            first.write_seed()
            first.commit()
        elif state == 'retired':
            setup.host.compensate_owner_registration('target-session', BOB, expected_revision=1, expected_epoch=1)
    request = replace(setup.request, title='different') if changed_input else setup.request
    seed = setup.compiler.compile(request)
    before = copy.deepcopy(setup.access._load())
    with ContinuationPublication(setup.host, setup.compiler, seed, FINGERPRINT, target_snapshot=TARGET) as second:
        with pytest.raises(SessionSharingConflict, match='token already reserved'):
            setup.host.register_owner_and_source('another-target', BOB, setup.target.project_id)
        assert second.session_id is None
    assert setup.access._load() == before


@pytest.mark.asyncio
async def test_identical_token_different_full_identity_does_not_conflict(setup):
    from dataclasses import replace
    from jiuwenswarm.server.runtime.session.continuation import ContinuationCompiler
    carol = source_tests.CAROL
    setup.access.replace_acl(setup.target.project_id, 'admin', acl={'bob': ['read', 'execute'],
                             'carol': ['read', 'execute']}, expected_revision=2)
    grant = setup.host.store.grant('source-session', ALICE, carol, actions={'view', 'execute'},
                                  history=setup.scope, expires_at=200)
    with make_scope(setup) as first:
        register(setup, first)
    compiler = ContinuationCompiler(setup.host, identity_resolver=lambda: carol, project_authorizer=setup.access)
    request = replace(setup.request, share_id=grant['share_id'])
    seed = compiler.compile(request)
    with ContinuationPublication(setup.host, compiler, seed, FINGERPRINT, target_snapshot=TARGET) as second:
        register(setup, second, 'carol-target')
        second.write_seed()
        second.commit()
    assert setup.host.owner_current('carol-target', carol)
    assert not setup.host.owner_current('target-session', BOB)


@pytest.mark.asyncio
async def test_second_concurrent_task_cannot_win_same_token_registration(setup):
    ready, continue_second = asyncio.Event(), asyncio.Event()
    seed = setup.compiler.compile(setup.request)
    async def first():
        with ContinuationPublication(setup.host, setup.compiler, seed, FINGERPRINT, target_snapshot=TARGET) as scope:
            register(setup, scope)
            ready.set()
            await continue_second.wait()
    task = asyncio.create_task(first())
    await ready.wait()
    try:
        with make_scope(setup):
            with pytest.raises(SessionSharingConflict, match='token already reserved'):
                setup.host.register_owner_and_source('racing-target', BOB, setup.target.project_id)
    finally:
        continue_second.set()
        await task


@pytest.mark.asyncio
async def test_unsupported_safe_file_flags_deny_continuation_only(setup, monkeypatch):
    with make_scope(setup) as scope:
        monkeypatch.delattr(publication.os, 'O_NOFOLLOW')
        with pytest.raises(SessionSharingDenied, match='platform'):
            setup.host.register_owner_and_source('unsupported', BOB, setup.target.project_id)
        assert scope.session_id is None
    assert setup.host.owner_current('source-session', ALICE)


@pytest.mark.asyncio
async def test_retired_continuation_cannot_lose_token_tombstone_via_ordinary_recreation(setup):
    with make_scope(setup) as scope:
        register(setup, scope)
        setup.host.compensate_owner_registration('target-session', BOB, expected_revision=1, expected_epoch=1)
    before = copy.deepcopy(setup.access._load())
    with pytest.raises(SessionSharingConflict, match='IDs cannot be reused'):
        setup.host.register_owner_and_source('target-session', BOB, setup.target.project_id, expected_owner_revision=2)
    assert setup.access._load() == before
    # This only reserves B3 IDs; ordinary retired owner recreation still works.
    setup.host.register_owner_and_source('ordinary', BOB, setup.target.project_id)
    setup.host.compensate_owner_registration('ordinary', BOB, expected_revision=1, expected_epoch=1)
    assert setup.host.register_owner_and_source('ordinary', BOB, setup.target.project_id, expected_owner_revision=2) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize('field,value', [('mode', 'agent.code.normal'), ('mode', 'agent.plan'), ('mode', 'team.work.normal'),
                                        ('mode', None), ('mode', ''), ('mode', 'agent.unknown'), ('work_mode', 'code'),
                                        ('project_dir', '/another/workspace')])
async def test_original_target_workspace_and_mode_remain_bound(setup, field, value):
    with make_scope(setup) as scope:
        directory = register(setup, scope)
        scope.write_seed()
        scope.commit()
    assert publication.read_target_snapshot(setup.host, 'target-session', BOB) == TARGET
    metadata = json.loads((directory / 'metadata.json').read_text())
    metadata[field] = value
    (directory / 'metadata.json').write_text(json.dumps(metadata))
    assert not new_host(setup).owner_current('target-session', BOB)


@pytest.mark.asyncio
@pytest.mark.parametrize('field,value', [('model_binding_fingerprint', 'invalid'),
                                        ('model_entry_fingerprint', None), ('extra', 'unknown')])
async def test_corrupt_persisted_model_target_is_rejected(setup, field, value):
    with make_scope(setup) as scope:
        register(setup, scope)
        scope.write_seed()
        scope.commit()
    data = setup.access._load()
    data['session_sharing']['owners']['target-session']['continuation']['target_snapshot'][field] = value
    setup.access._save(data)
    assert not new_host(setup).owner_current('target-session', BOB)


@pytest.mark.asyncio
async def test_original_usage_history_keeps_committed_continuation_owner(setup, monkeypatch):
    from jiuwenswarm.server.runtime.session import session_metadata, session_history
    monkeypatch.setattr(session_metadata, 'get_agent_sessions_dir', lambda: setup.tmp_path / 'sessions')
    monkeypatch.setattr(session_metadata, '_METADATA_CACHE', {})
    monkeypatch.setattr(session_metadata, '_METADATA_CACHE_GENERATIONS', {})
    with make_scope(setup) as scope:
        directory = register(setup, scope)
        scope.write_seed()
        scope.commit()
    epoch = setup.host.owner_revision('target-session', BOB)
    session_history.append_history_record(session_id='target-session', role='assistant', content='',
        event_type='context.usage', channel_id='web', mode='agent', request_id='goal-usage',
        timestamp=1.0, extra={'context_window': {}, 'parts': {}})
    assert session_metadata.flush_pending_writes()
    assert json.loads((directory / 'metadata.json').read_text())['mode'] == 'agent'
    assert new_host(setup).owner_current('target-session', BOB)
    assert setup.host.owner_revision('target-session', BOB) == epoch
    assert publication.read_seed(setup.host, 'target-session', BOB) == scope.seed
