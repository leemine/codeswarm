"""Actual JSONL range admission and current authorization, without a second store."""
import json
from dataclasses import replace

import pytest

from jiuwenswarm.governance.session_sharing import SessionSharingDenied
from jiuwenswarm.server.runtime.session import lifecycle, session_history as history
from jiuwenswarm.server.runtime.session import shared_history as shared


@pytest.fixture
def source(tmp_path, monkeypatch):
    monkeypatch.setattr(history, 'get_agent_sessions_dir', lambda: tmp_path / 'sessions')
    monkeypatch.setattr(lifecycle, 'get_agent_sessions_dir', lambda: tmp_path / 'sessions')
    path = tmp_path / 'sessions/session/history.jsonl'
    path.parent.mkdir(parents=True)
    records = [dict(id=str(i), role='user', content=f'记录-{i}') for i in range(8)]
    path.write_bytes(b''.join(json.dumps(row, ensure_ascii=False).encode() + b'\n' for row in records))
    return path


def compile_scope(**kwargs):
    return shared.compile_shared_history_range('session', authorize=lambda: True, **kwargs)


def read(scope, **kwargs):
    options = dict(authorize=lambda _: True, is_visible=lambda _: True)
    options.update(kwargs)
    return shared.read_shared_history_page(scope, **options)


def test_actual_record_range_pages_do_not_expand_after_append(source):
    scope = compile_scope(start_record=2, end_record=6)
    assert scope.dev == source.stat().st_dev and scope.ino == source.stat().st_ino
    with source.open('ab') as stream:
        stream.write(b'{"id":"new"}\n')
    page = read(scope, limit=2)
    assert [r['id'] for r in page.messages] == ['5', '4']
    retry = read(scope, limit=2)
    assert retry.messages == page.messages and retry.next_handle == page.next_handle
    older = read(scope, handle=page.next_handle, limit=2)
    assert [r['id'] for r in older.messages] == ['3', '2']
    assert older.next_handle is None
    assert page.snapshot_id == older.snapshot_id


@pytest.mark.parametrize('result', [False, None, 1, 'true', 'error'])
def test_denied_compilation_does_not_resolve_or_touch_paths(source, monkeypatch, result):
    def authorize():
        if result == 'error':
            raise RuntimeError('authority unavailable')
        return result
    def touched(*args, **kwargs):
        pytest.fail('unauthorized filesystem lookup')
    monkeypatch.setattr(history, 'get_read_history_path', touched)
    with pytest.raises(SessionSharingDenied):
        shared.compile_shared_history_range('session', authorize=authorize)


@pytest.mark.parametrize('stage', ['read', 'return', 'deliver'])
def test_current_authority_rechecked_before_read_return_and_delivery(source, monkeypatch, stage):
    scope = compile_scope()
    allowed = [stage != 'read']
    def visible(record):
        if stage == 'return':
            allowed[0] = False
        return True
    if stage == 'read':
        monkeypatch.setattr(history, 'get_read_history_path', lambda *args: pytest.fail('read before authorization'))
    if stage == 'deliver':
        page = read(scope, authorize=lambda _: allowed[0], is_visible=visible)
        allowed[0] = False
        with pytest.raises(SessionSharingDenied):
            page.revalidate()
    else:
        with pytest.raises(SessionSharingDenied):
            read(scope, authorize=lambda _: allowed[0], is_visible=visible)


def test_compiler_rechecks_authority_after_snapshot(source):
    calls = []
    def authorize():
        calls.append(True)
        return len(calls) == 1
    with pytest.raises(SessionSharingDenied):
        shared.compile_shared_history_range('session', authorize=authorize)
    assert len(calls) == 2


@pytest.mark.parametrize('mutation', ['replace', 'truncate', 'remove', 'symlink'])
def test_file_identity_and_snapshot_size_validated_each_page_and_delivery(source, mutation):
    scope = compile_scope()
    page = read(scope, limit=1)
    if mutation == 'replace':
        target = source.with_name('replacement')
        target.write_bytes(source.read_bytes())
        target.replace(source)
    elif mutation == 'truncate':
        with source.open('r+b') as stream:
            stream.truncate(0)
    elif mutation == 'remove':
        source.unlink()
    else:
        target = source.with_name('other')
        source.rename(target)
        source.symlink_to(target)
    with pytest.raises(history.HistorySnapshotChanged):
        read(scope, handle=page.next_handle)
    with pytest.raises(history.HistorySnapshotChanged):
        page.revalidate()


def test_replacement_inside_filter_never_delivers_old_records(source):
    scope = compile_scope()
    def visible(record):
        replacement = source.with_name('replacement')
        replacement.write_bytes(source.read_bytes())
        replacement.replace(source)
        return True
    with pytest.raises(history.HistorySnapshotChanged):
        read(scope, is_visible=visible)


@pytest.mark.parametrize('field', ['start', 'end', 'snapshot_end'])
def test_grant_byte_boundaries_cannot_split_jsonl_records(source, field):
    scope = compile_scope(start_record=1, end_record=5)
    broken = replace(scope, **{field: getattr(scope, field) - 1})
    with pytest.raises(history.InvalidHistoryCursor):
        read(broken)


def test_private_handle_rejects_wire_json_other_scope_and_forged_position(source):
    scope = compile_scope(start_record=2, end_record=6)
    page = read(scope, limit=1)
    for handle in ('client-cursor', {'pos': 0}, read(compile_scope(), limit=1).next_handle):
        with pytest.raises(history.InvalidHistoryCursor):
            read(scope, handle=handle)
    payload = history._decode_history_cursor(page.next_handle._cursor)
    for change in ({'pos': 0}, {'pos': scope.end + 1}, {'pos': scope.end - 1},
                   {'pos': True}, {'ino': scope.ino + 1}, {'end': scope.snapshot_end + 1},
                   {'stream': history._cursor_stream_key('session', 'child')}):
        forged = shared.SharedHistoryHandle(scope, history._encode_history_cursor({**payload, **change}))
        with pytest.raises(history.InvalidHistoryCursor):
            read(scope, handle=forged)


def test_filter_is_strict_and_never_reads_child_or_attachments(source, monkeypatch):
    with source.open('ab') as stream:
        stream.write(b'{"id":"child-root","subagent_id":"child"}\n')
        stream.write(b'{"id":"file","files":[{"path":"secret-attachment"}]}\n')
    child = source.parent / 'subagents/child/history.jsonl'
    child.parent.mkdir(parents=True)
    child.write_bytes(b'{"id":"secret-child"}\n')
    monkeypatch.setattr(history, 'resolve_subagent_history_path', lambda *args, **kwargs: pytest.fail('child lookup'))
    scope = compile_scope()
    page = read(scope, is_visible=lambda row: True if row.get('id') in {'7', 'child-root'} else 1)
    assert [r['id'] for r in page.messages] == ['7']


def test_read_budget_pages_filtered_rows_and_large_record_rejection(source, monkeypatch):
    monkeypatch.setattr(shared, 'MAX_SCANNED_RECORDS', 2)
    scope = compile_scope()
    page = read(scope, is_visible=lambda _: False)
    assert not page.messages and page.next_handle is not None
    assert read(scope, handle=page.next_handle, limit=1).messages[0]['id'] == '5'
    monkeypatch.setattr(shared, 'MAX_PAGE_SCAN_BYTES', 1)
    with pytest.raises(shared.SharedHistoryLimit):
        read(scope)


def test_empty_existing_file_has_real_identity_and_missing_file_fails(source):
    source.write_bytes(b'')
    scope = compile_scope()
    assert scope.start == scope.end == scope.snapshot_end == 0
    assert scope.ino == source.stat().st_ino
    assert read(scope).messages == ()
    source.unlink()
    with pytest.raises(history.HistorySnapshotChanged):
        compile_scope()


@pytest.mark.parametrize('body', [b'{"id":1}', b'not json\n', b'[]\n'])
def test_incomplete_or_corrupt_history_cannot_create_a_range(source, body):
    source.write_bytes(body)
    with pytest.raises(history.InvalidHistoryCursor):
        compile_scope()


def test_no_legacy_migration_and_no_arbitrary_path(source):
    source.unlink()
    source.with_suffix('.json').write_text('[]')
    with pytest.raises(history.InvalidHistoryCursor):
        compile_scope()
    assert not source.exists()
    with pytest.raises(history.InvalidHistoryCursor):
        shared.compile_shared_history_range('../session', authorize=lambda: True)


def test_pending_existing_writer_queue_is_drained_before_capture(source):
    history._enqueue_history_item('session', {'id': 'queued', 'role': 'user', 'content': 'durable'})
    scope = compile_scope()
    assert read(scope, limit=1).messages[0]['id'] == 'queued'
    assert scope.snapshot_end == source.stat().st_size


@pytest.mark.parametrize('limit', [0, -1, 201, True])
def test_page_limits_are_bounded(source, limit):
    with pytest.raises(ValueError):
        read(compile_scope(), limit=limit)


def test_shared_user_content_reuses_existing_collapse(source):
    source.write_bytes(json.dumps({'role': 'user', 'content': '<file-content path="owned">SECRET</file-content>'}).encode()+b'\n')
    assert read(compile_scope()).messages[0]['content'] == '@owned'


def test_utf8_record_larger_than_read_block_keeps_exact_bounds(source):
    rows = [{'id': 'before', 'content': 'outside'}, {'id': 'large', 'content': '汉🙂' * 30000},
            {'id': 'after', 'content': 'outside'}]
    source.write_bytes(b''.join(json.dumps(row, ensure_ascii=False).encode()+b'\n' for row in rows))
    scope = compile_scope(start_record=1, end_record=2)
    page = read(scope, limit=1)
    assert page.messages == (rows[1],)
    assert page.next_handle is None
    assert page.scanned_bytes == scope.end - scope.start


def test_compiler_size_record_and_selection_limits(source, monkeypatch):
    for kwargs in ({'start_record': True}, {'start_record': -1}, {'end_record': 9},
                   {'start_record': 4, 'end_record': 3}):
        with pytest.raises(ValueError):
            compile_scope(**kwargs)
    monkeypatch.setattr(shared, 'MAX_SNAPSHOT_BYTES', 1)
    with pytest.raises(shared.SharedHistoryLimit):
        compile_scope()
    monkeypatch.setattr(shared, 'MAX_SNAPSHOT_BYTES', 100000)
    monkeypatch.setattr(shared, 'MAX_RECORD_BYTES', 1)
    with pytest.raises(shared.SharedHistoryLimit):
        compile_scope()


@pytest.mark.parametrize('value', [False, None, 1, 'true', 'error'])
def test_page_authority_fails_closed_before_filesystem(source, monkeypatch, value):
    scope = compile_scope()
    monkeypatch.setattr(history, 'get_read_history_path', lambda *args: pytest.fail('unauthorized lookup'))
    def authorize(_):
        if value == 'error':
            raise RuntimeError('test-only authority error')
        return value
    with pytest.raises(SessionSharingDenied):
        read(scope, authorize=authorize)
