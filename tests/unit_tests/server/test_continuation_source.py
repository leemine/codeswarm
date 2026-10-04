"""B3 source facts on real sidecar/JSONL, without Session allocation or models."""
import contextvars
import json
import threading
from dataclasses import asdict, FrozenInstanceError, replace
from types import SimpleNamespace

import pytest

from jiuwenswarm.governance.continuation import ContinuationInput, ContinuationMessage
from jiuwenswarm.governance.contracts import AuthorizationDecision, TrustedIdentity
from jiuwenswarm.governance.session_sharing import SessionSharingDenied
from jiuwenswarm.server.runtime.session import continuation, lifecycle, project_store, session_history
from jiuwenswarm.server.runtime.session.history_io import run_history_io
from jiuwenswarm.server.runtime.session.project_access import ProjectAccessStore
from jiuwenswarm.server.runtime.session.sharing_host import SharingHostService

ALICE = TrustedIdentity('alice', 'subject-alice', 'organization')
BOB = TrustedIdentity('bob', 'subject-bob', 'organization')
CAROL = TrustedIdentity('carol', 'subject-carol', 'organization')


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(project_store, 'get_agent_root_dir', lambda: tmp_path)
    monkeypatch.setattr(lifecycle, 'get_agent_sessions_dir', lambda: tmp_path / 'sessions')
    monkeypatch.setattr(session_history, 'get_agent_sessions_dir', lambda: tmp_path / 'sessions')
    project_store.invalidate_cache()
    source = project_store.create_project('source', str(tmp_path / 'source'))
    target = project_store.create_project('target', str(tmp_path / 'target'))
    access = ProjectAccessStore()
    for project, acl in ((source, {'alice': ['read', 'execute', 'admin']}),
                         (target, {'bob': ['read', 'execute']})):
        access.initialize(project.project_id, 'admin')
        access.replace_acl(project.project_id, 'admin', acl=acl, expected_revision=1)
    identities = [BOB]
    host = SharingHostService(lambda _, actor: {'alice': ALICE, 'bob': BOB, 'carol': CAROL}.get(actor),
                             known_actor=lambda actor: actor in {ALICE, BOB, CAROL}, storage=access)
    clock = [100.0]
    host.store._clock = lambda: clock[0]
    path = tmp_path / 'sessions/source-session/history.jsonl'
    path.parent.mkdir(parents=True)
    (path.parent / 'metadata.json').write_text(json.dumps({'project_id': source.project_id}))
    rows = [{'role': 'user' if i % 2 == 0 else 'assistant', 'content': f'text-{i}'} for i in range(205)]
    rows += [{'role': 'assistant', 'event_type': event, 'content': 'not-context'}
             for event in ('chat.tool_call', 'chat.tool_result', 'chat.reasoning', 'chat.file', 'unknown')]
    rows += [{'role': 'user', 'content': 'child-secret', 'subagent_id': 'child'},
             {'role': 'assistant', 'event_type': 'chat.final', 'content': 'final', 'credential': 'never-copy'}]
    path.write_bytes(b''.join(json.dumps(row).encode() + b'\n' for row in rows))
    host.register_owner_and_source('source-session', ALICE, source.project_id)
    scope = host.prepare_source('source-session', ALICE)
    grant = host.store.grant('source-session', ALICE, BOB, actions={'view', 'execute'},
                            history=scope, expires_at=200)
    request = ContinuationInput('source-session', grant['share_id'], 1, 'create-1', target.project_id, 'native')
    compiler = continuation.ContinuationCompiler(host, identity_resolver=lambda: identities[0], project_authorizer=access)
    yield SimpleNamespace(**locals())
    project_store.invalidate_cache()


def test_complete_pages_order_plain_projection_hash_and_immutable(setup):
    seed = setup.compiler.compile(setup.request)
    assert [m.content for m in seed.messages] == [f'text-{i}' for i in range(205)] + ['final']
    assert set(asdict(seed.messages[-1])) == {'role', 'content'}
    assert seed.digest == setup.compiler.compile(setup.request).digest
    assert seed.proof.identity == BOB and seed.proof.source_owner == ALICE
    assert 'text-0' not in repr(seed)
    with pytest.raises(FrozenInstanceError):
        seed.messages = ()
    with setup.path.open('ab') as stream:
        stream.write(b'{"role":"user","content":"after-share"}\n')
    assert setup.compiler.compile(setup.request).digest == seed.digest


@pytest.mark.parametrize('extra', ['identity', 'authority', 'subject_id', 'user_id', 'history', 'cursor',
                                  'project_dir', 'credentials', 'native_session_id', 'is_swarm'])
def test_wire_unknown_fields_rejected(setup, extra):
    with pytest.raises(ValueError):
        ContinuationInput.from_wire({**asdict(setup.request), extra: 'forged'})


@pytest.mark.parametrize('field,value', [('expected_revision', True), ('expected_revision', 0),
    ('title', 'x' * 101), ('create_token', ''), ('create_token', ' spaced '), ('target_project_id', {}),
    ('mode', 'team'), ('execution_profile_id', 'x\n')])
def test_strict_wire_types(setup, field, value):
    with pytest.raises(ValueError):
        ContinuationInput.from_wire({**asdict(setup.request), field: value})


def test_wire_roundtrip_and_missing_fields(setup):
    assert ContinuationInput.from_wire(asdict(setup.request)) == setup.request
    with pytest.raises(ValueError):
        ContinuationInput.from_wire({})


@pytest.mark.parametrize('actions', [{'view'}, {'execute'}])
def test_same_share_requires_both_independent_actions(setup, actions):
    record = setup.host.store.revise(setup.grant['share_id'], ALICE, actions=actions,
        history=setup.scope, expires_at=200, expected_revision=1)
    # An unrelated second share cannot fill in the missing action.
    setup.host.store.grant('source-session', ALICE, BOB, actions={'view', 'execute'},
                          history=setup.scope, expires_at=200)
    with pytest.raises(SessionSharingDenied):
        setup.compiler.compile(replace(setup.request, expected_revision=record['revision']))


@pytest.mark.parametrize('actor', [ALICE, None, replace(BOB, authority='other'), replace(BOB, subject_id='other')])
def test_full_current_identity_before_history(setup, monkeypatch, actor):
    setup.identities[0] = actor
    monkeypatch.setattr(continuation, 'read_shared_history_page', lambda *a, **k: pytest.fail('unauthorized IO'))
    with pytest.raises(SessionSharingDenied):
        setup.compiler.compile(setup.request)


@pytest.mark.parametrize('change', ['expiry', 'revoke', 'regrant', 'actor', 'source_acl', 'target_acl', 'share_revision', 'source_epoch'])
def test_barrier_mutation_after_first_page_never_returns_seed(setup, monkeypatch, change):
    original = continuation.read_shared_history_page
    def read(*args, **kwargs):
        page = original(*args, **kwargs)
        if change == 'expiry':
            setup.clock[0] = 200
        elif change in ('revoke', 'regrant'):
            setup.host.store.revoke(setup.grant['share_id'], ALICE, expected_revision=1)
            if change == 'regrant':
                setup.host.store.grant('source-session', ALICE, BOB, actions={'view', 'execute'},
                                      history=setup.scope, expires_at=200)
        elif change == 'actor':
            setup.identities[0] = replace(BOB, subject_id='different')
        elif change == 'source_acl':
            setup.access.replace_acl(setup.source.project_id, 'admin', acl={'alice': ['read', 'admin']}, expected_revision=2)
        elif change == 'target_acl':
            setup.access.replace_acl(setup.target.project_id, 'admin', acl={}, expected_revision=2)
        elif change == 'source_epoch':
            setup.host.invalidate_source('source-session', expected_epoch=1)
        else:
            setup.host.store.revise(setup.grant['share_id'], ALICE, actions={'view', 'execute'},
                                    history=setup.scope, expires_at=190, expected_revision=1)
        return page
    monkeypatch.setattr(continuation, 'read_shared_history_page', read)
    with pytest.raises(SessionSharingDenied):
        setup.compiler.compile(setup.request)


def test_revalidate_target_revision_even_if_regranted_allowed(setup):
    seed = setup.compiler.compile(setup.request)
    setup.access.replace_acl(setup.target.project_id, 'admin', acl={}, expected_revision=2)
    setup.access.replace_acl(setup.target.project_id, 'admin', acl={'bob': ['execute']}, expected_revision=3)
    with pytest.raises(SessionSharingDenied):
        setup.compiler.revalidate(seed.proof)


@pytest.mark.parametrize('limit,value', [('MAX_SEED_BYTES', 10), ('MAX_SEED_MESSAGES', 100),
                                       ('MAX_SOURCE_BYTES', 10), ('MAX_SOURCE_PAGES', 2)])
def test_limits_reject_whole_result_not_truncate(setup, monkeypatch, limit, value):
    monkeypatch.setattr(continuation, limit, value)
    with pytest.raises(continuation.SharedHistoryLimit):
        setup.compiler.compile(setup.request)


@pytest.mark.parametrize('mutation', ['inode', 'boundary'])
def test_actual_range_proof_not_just_value_object(setup, mutation):
    if mutation == 'inode':
        replacement = setup.path.with_name('replacement')
        replacement.write_bytes(setup.path.read_bytes())
        replacement.replace(setup.path)
    else:
        with setup.path.open('r+b') as stream:
            stream.seek(setup.scope.end - 1)
            stream.write(b'x')
    with pytest.raises((SessionSharingDenied, ValueError, session_history.HistorySnapshotChanged)):
        setup.compiler.compile(setup.request)


@pytest.mark.parametrize('rights,expected', [(['read', 'admin', 'execute'], True),
    (['read', 'admin'], False), (['read', 'execute'], False), (['admin', 'execute'], False)])
def test_source_execute_needs_all_three_rights(setup, rights, expected):
    setup.access.replace_acl(setup.source.project_id, 'admin', acl={'alice': rights}, expected_revision=2)
    source = setup.host.resolve_source('source-session')
    assert (source is not None and 'execute' in source.actions) is expected


@pytest.mark.parametrize('decision', [AuthorizationDecision(True, 'wrong', 'bob', 'execute', 2),
    AuthorizationDecision(True, 'target', 'bob', 'execute', 0), True])
def test_target_must_return_exact_current_managed_decision(setup, decision):
    setup.compiler.project_authorizer = SimpleNamespace(authorize=lambda *args: decision)
    with pytest.raises(SessionSharingDenied):
        setup.compiler.compile(setup.request)


@pytest.mark.asyncio
async def test_offload_preserves_original_identity_and_detects_revocation(setup, monkeypatch):
    current = contextvars.ContextVar('continuation-test-identity', default=None)
    credential_active = [True]
    current.set(BOB)
    setup.compiler.identity_resolver = lambda: current.get() if credential_active[0] else None
    entered, release = threading.Event(), threading.Event()
    original = continuation.read_shared_history_page
    def read(*args, **kwargs):
        assert current.get() == BOB
        page = original(*args, **kwargs)
        entered.set()
        assert release.wait(5)
        return page
    monkeypatch.setattr(continuation, 'read_shared_history_page', read)
    import asyncio
    task = asyncio.create_task(run_history_io(setup.compiler.compile, setup.request))
    assert await asyncio.to_thread(entered.wait, 5)
    current.set(ALICE)
    credential_active[0] = False
    release.set()
    with pytest.raises(SessionSharingDenied):
        await task


def test_message_rejects_nontext_or_tool_role():
    with pytest.raises(ValueError):
        ContinuationMessage('tool', 'secret')
    with pytest.raises(ValueError):
        ContinuationMessage('user', {'data': 'secret'})


def test_parent_revocation_and_restoration_does_not_revive_child(setup):
    parent = setup.host.store.grant('source-session', ALICE, CAROL, actions={'view', 'execute', 'manage'},
                                    history=setup.scope, expires_at=200)
    child = setup.host.store.grant('source-session', CAROL, BOB, actions={'view', 'execute'},
        history=setup.scope, expires_at=190, parent_share_id=parent['share_id'])
    request = replace(setup.request, share_id=child['share_id'])
    proof = setup.compiler.compile(request).proof
    setup.host.store.revise(parent['share_id'], ALICE, actions={'view', 'execute', 'manage'},
                            history=setup.scope, expires_at=200, expected_revision=1)
    with pytest.raises(SessionSharingDenied):
        setup.compiler.revalidate(proof)


def test_exact_limits_and_final_page_boundary_are_complete(setup, monkeypatch):
    seed = setup.compiler.compile(setup.request)
    monkeypatch.setattr(continuation, 'MAX_SEED_MESSAGES', len(seed.messages))
    monkeypatch.setattr(continuation, 'MAX_SEED_BYTES', sum(len(m.content.encode()) for m in seed.messages))
    monkeypatch.setattr(continuation, 'MAX_SOURCE_BYTES', setup.scope.end - setup.scope.start)
    monkeypatch.setattr(continuation, 'MAX_SOURCE_PAGES', 3)
    assert setup.compiler.compile(setup.request) == seed


def test_same_inode_outside_granted_range_not_seeded(setup):
    narrower = replace(setup.scope, end=setup.path.read_bytes().index(b'\n') + 1)
    record = setup.host.store.revise(setup.grant['share_id'], ALICE, actions={'view', 'execute'},
                                    history=narrower, expires_at=200, expected_revision=1)
    seed = setup.compiler.compile(replace(setup.request, expected_revision=record['revision']))
    assert [m.content for m in seed.messages] == ['text-0']
    assert seed.proof.history == narrower


def test_viewer_and_seed_share_tool_reasoning_exclusion(setup):
    from jiuwenswarm.server.runtime.gateway_adapter.shared_history_adapter import SharedHistoryAdapter
    adapter = SharedHistoryAdapter(setup.host.store, identity_resolver=lambda _: BOB)
    messages, cursor = [], None
    for _ in range(5):
        params = {'session_id': 'source-session', 'share_id': setup.grant['share_id'], 'limit': 100}
        if cursor:
            params['cursor'] = cursor
        response = adapter._read(SimpleNamespace(params=params, request_id='r', channel_id='web', metadata={}))
        messages.extend(response.payload['messages'])
        cursor = response.payload['next_cursor']
        if cursor is None:
            break
    assert cursor is None
    assert [m['content'] for m in reversed(messages)] == [m.content for m in setup.compiler.compile(setup.request).messages]
    assert 'not-context' not in json.dumps(messages) and 'child-secret' not in json.dumps(messages)


def test_compiler_releases_sharing_lock_before_history(setup, monkeypatch):
    from contextlib import contextmanager
    active = [0]
    locked = setup.access._locked
    @contextmanager
    def counted():
        with locked():
            active[0] += 1
            try:
                yield
            finally:
                active[0] -= 1
    monkeypatch.setattr(setup.access, '_locked', counted)
    original = continuation.read_shared_history_page
    def read(*args, **kwargs):
        assert active[0] == 0
        return original(*args, **kwargs)
    monkeypatch.setattr(continuation, 'read_shared_history_page', read)
    setup.compiler.compile(setup.request)
    assert active[0] == 0


def test_final_seed_construction_is_followed_by_authority_check(setup, monkeypatch):
    original = continuation.ContinuationSeed
    def build(*args):
        seed = original(*args)
        setup.clock[0] = 200
        return seed
    monkeypatch.setattr(continuation, 'ContinuationSeed', build)
    with pytest.raises(SessionSharingDenied):
        setup.compiler.compile(setup.request)


def test_target_authorizer_cannot_change_actor_during_final_check(setup):
    calls = []
    def authorize(*args):
        decision = setup.access.authorize(*args)
        calls.append(True)
        if len(calls) == 2:
            setup.identities[0] = ALICE
        return decision
    setup.compiler.project_authorizer = SimpleNamespace(authorize=authorize)
    with pytest.raises(SessionSharingDenied):
        setup.compiler.compile(setup.request)


def test_actual_legacy_writer_does_not_turn_tool_or_reasoning_into_seed(setup):
    records = [
        (None, {'tool_calls': [{'id': 't', 'name': 'read'}]}, 'tool-declaration'),
        ('', {'tool_call': {'id': 't'}}, 'tool-single'),
        (None, {'function_call': {'name': 'read'}}, 'legacy-function'),
        (None, {'tool_result': {'content': 'secret'}}, 'tool-result'),
        (None, {'reasoning_content': 'private-reasoning'}, 'ambiguous-reasoning'),
        ('', {'reasoning': 'private-reasoning'}, 'legacy-reasoning'),
        ('chat.final', {'reasoning_content': 'private-reasoning'}, 'visible-answer'),
    ]
    for index, (event, extra, content) in enumerate(records):
        receipt = session_history.append_history_record_durable(
            session_id='source-session', request_id=f'legacy-{index}', channel_id='web',
            role='assistant', content=content, timestamp=1.0, event_type=event, extra=extra)
        assert receipt.result(timeout=5) is True
    raw = setup.path.read_text()
    assert 'tool-declaration' in raw and 'ambiguous-reasoning' in raw
    scope = setup.host.prepare_source('source-session', ALICE)
    record = setup.host.store.revise(setup.grant['share_id'], ALICE, actions={'view', 'execute'},
                                    history=scope, expires_at=200, expected_revision=1)
    seed = setup.compiler.compile(replace(setup.request, expected_revision=record['revision']))
    assert seed.messages[-1].content == 'visible-answer'
    rendered = json.dumps([asdict(message) for message in seed.messages])
    for _, _, content in records[:-1]:
        assert content not in rendered
    assert 'private-reasoning' not in rendered


@pytest.mark.parametrize('field', ['tool_call', 'tool_calls', 'function_call', 'tool_result',
                                   'reasoning', 'reasoning_content'])
@pytest.mark.parametrize('value', [None, '', [], {}])
def test_ambiguous_legacy_payloads_fail_closed_even_when_empty(field, value):
    from jiuwenswarm.server.runtime.gateway_adapter.shared_history_adapter import SharedHistoryAdapter
    assert not SharedHistoryAdapter._visible({'role': 'assistant', 'content': 'legacy', field: value})


def test_explicit_target_model_is_bound_as_data_without_consuming_credentials(setup):
    first = setup.compiler.compile(replace(setup.request, model_name='bob-model'))
    second = setup.compiler.compile(replace(setup.request, model_name='other-model'))
    assert first.proof.request.model_name == 'bob-model' and first.digest != second.digest
    assert first.messages == second.messages
    for invalid in (None, [], ' spaced ', 'bad\nmodel', 'x' * 201):
        with pytest.raises(ValueError):
            replace(setup.request, model_name=invalid)
