"""Authorized fixed ranges over the existing Session JSONL, without new storage.

These synchronous functions belong in the host's IO offload. Callbacks are
trusted, synchronous current-authorization checks; never hold a sharing-store
lock while calling this module. Private handles must stay server-side. The
module does not assemble messages, resolve attachments, restore a runtime or
read subagent streams.
"""
from __future__ import annotations

import json
import os
import stat
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable

from jiuwenswarm.governance.session_sharing import SessionHistoryRange, SessionSharingDenied
from jiuwenswarm.server.runtime.session import session_history as history

MAX_PAGE_RECORDS = 200
MAX_SCANNED_RECORDS = 512
MAX_RECORD_BYTES = 2 * 1024 * 1024
MAX_PAGE_SCAN_BYTES = 4 * 1024 * 1024
MAX_SNAPSHOT_BYTES = 64 * 1024 * 1024


def visible_conversation_text(record: dict[str, Any]) -> bool:
    """The shared viewer/continuation text projection, excluding tool events."""
    role, event = record.get('role'), record.get('event_type')
    # Legacy/SDK writers may omit event_type while retaining structured tool
    # declarations or reasoning. Missing event identity is not final-text proof.
    if any(key in record for key in ('tool_call', 'tool_calls', 'function_call', 'tool_result')):
        return False
    if role == 'assistant' and event in (None, '') and any(
        key in record for key in ('reasoning_content', 'reasoning')
    ):
        return False
    # Explicit chat.final commonly also stores reasoning_content; only its
    # final content is projected, never that separate reasoning field.
    return ((role == 'user' and event in (None, ''))
            or (role == 'assistant' and event in (None, '', 'chat.final'))) and (
        isinstance(record.get('content'), str) and bool(record['content'].strip())
    )


class SharedHistoryLimit(ValueError):
    """The history exceeds the bounded compiler/page reader's supported size."""


def _authorize(callback: Callable[..., bool], *args: Any) -> None:
    try:
        allowed = callback(*args)
    except Exception as exc:
        raise SessionSharingDenied('current history authorization unavailable') from exc
    if allowed is not True:
        # A forgotten await must not act as a truthy grant (nor leak a coroutine).
        if hasattr(allowed, 'close') and hasattr(allowed, '__await__'):
            allowed.close()
        raise SessionSharingDenied('current history authorization denied')


def _session_id(session_id: str) -> None:
    if not isinstance(session_id, str) or not history.is_valid_session_id(session_id):
        raise history.InvalidHistoryCursor('invalid session_id')


@contextmanager
def _open(session_id: str):
    _session_id(session_id)
    path = history.get_read_history_path(session_id)
    if path.suffix != '.jsonl':
        raise history.InvalidHistoryCursor('shared history requires existing JSONL storage')
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        raise history.HistorySnapshotChanged('shared history is missing or replaced') from exc
    with os.fdopen(fd, 'rb') as stream:
        current = os.fstat(stream.fileno())
        if not stat.S_ISREG(current.st_mode):
            raise history.HistorySnapshotChanged('shared history is not a regular file')
        yield stream, current
        # A rename during the read must not deliver data from an obsolete inode.
        try:
            after = path.lstat()
        except OSError as exc:
            raise history.HistorySnapshotChanged('shared history disappeared') from exc
        if not stat.S_ISREG(after.st_mode) or (after.st_dev, after.st_ino) != (current.st_dev, current.st_ino):
            raise history.HistorySnapshotChanged('shared history was replaced')


def _boundary(stream, position: int) -> None:
    if position:
        stream.seek(position - 1)
        if stream.read(1) != b'\n':
            raise history.InvalidHistoryCursor('history position is not a complete record boundary')


def _validate(stream, current, scope: SessionHistoryRange) -> None:
    if (current.st_dev, current.st_ino) != (scope.dev, scope.ino) or current.st_size < scope.snapshot_end:
        raise history.HistorySnapshotChanged('shared history snapshot changed')
    for position in (scope.start, scope.end, scope.snapshot_end):
        _boundary(stream, position)


def _revalidate(scope: SessionHistoryRange, authorize: Callable[[SessionHistoryRange], bool]) -> None:
    _authorize(authorize, scope)
    with history._FILE_LOCK:
        with _open(scope.session_id) as (stream, current):
            _validate(stream, current, scope)


def compile_shared_history_range(
    session_id: str,
    *,
    authorize: Callable[[], bool],
    start_record: int = 0,
    end_record: int | None = None,
) -> SessionHistoryRange:
    """Compile a host-selected record interval, never a client cursor/path.

    Indices select nonblank JSON object records in durable file order, end
    exclusive. The host authorizes selection; a caller-provided index is not an
    authority. Existing empty files have their real inode; absent files fail.
    Only the root product Session stream is supported. JSON legacy migration
    remains the existing host flow, not a side effect of a shared read.
    """
    _authorize(authorize)
    _session_id(session_id)
    if type(start_record) is not int or start_record < 0 or (
        end_record is not None and (type(end_record) is not int or end_record < start_record)
    ):
        raise ValueError('invalid record selection')
    # Same ordering as initial history cursor capture: stop new enqueues, drain
    # prior writes before taking the existing file lock (never drain under it).
    with history._QUEUE_ENQUEUE_LOCK:
        history._WRITE_QUEUE.join()
        with history._FILE_LOCK:
            with _open(session_id) as (stream, current):
                size = current.st_size
                if size > MAX_SNAPSHOT_BYTES:
                    raise SharedHistoryLimit('shared history snapshot is too large')
                index = 0
                start = end = None
                while stream.tell() < size:
                    position = stream.tell()
                    raw = stream.readline(MAX_RECORD_BYTES + 1)
                    if len(raw) > MAX_RECORD_BYTES:
                        raise SharedHistoryLimit('shared history record is too large')
                    if not raw.endswith(b'\n'):
                        raise history.InvalidHistoryCursor('history has an incomplete final record')
                    if not raw.strip():
                        continue
                    try:
                        record = json.loads(raw)
                    except (UnicodeError, ValueError) as exc:
                        raise history.InvalidHistoryCursor('history has an invalid JSONL record') from exc
                    if not isinstance(record, dict):
                        raise history.InvalidHistoryCursor('history record must be an object')
                    if index == start_record:
                        start = position
                    if index == end_record:
                        end = position
                    index += 1
                if start_record == index:
                    start = size
                if end_record is None or end_record == index:
                    end = size
                if start is None or end is None:
                    raise ValueError('record selection is outside history')
                scope = SessionHistoryRange(session_id, history._cursor_stream_key(session_id, None),
                                            current.st_dev, current.st_ino, size, start, end)
                _validate(stream, os.fstat(stream.fileno()), scope)
    _revalidate(scope, lambda _: authorize())
    return scope


@dataclass(frozen=True, slots=True)
class SharedHistoryHandle:
    """Host-private wrapper over the existing cursor, not a wire-deserializable grant."""
    _scope: SessionHistoryRange = field(repr=False)
    _cursor: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class SharedHistoryPage:
    messages: tuple[dict[str, Any], ...]
    next_handle: SharedHistoryHandle | None
    snapshot_id: str
    scanned_bytes: int
    _scope: SessionHistoryRange = field(repr=False)
    _authorize: Callable[[SessionHistoryRange], bool] = field(repr=False, compare=False)

    def revalidate(self) -> None:
        """Host MUST call immediately before sending each buffered chunk."""
        _revalidate(self._scope, self._authorize)


class _RangeReader:
    """Present only the authorized interval to the original reverse JSONL iterator."""
    def __init__(self, stream, start: int, end: int):
        self.stream, self.start, self.end, self.position = stream, start, end, 0
        self.scanned = 0

    def seek(self, offset: int):
        if not 0 <= offset <= self.end - self.start:
            raise history.InvalidHistoryCursor('read position is outside shared range')
        self.position = offset
        return self.stream.seek(self.start + offset)

    def read(self, size: int):
        if type(size) is not int or size < 0 or self.position + size > self.end - self.start:
            raise history.InvalidHistoryCursor('read exceeds shared range')
        if self.scanned + size > MAX_PAGE_SCAN_BYTES:
            raise SharedHistoryLimit('shared history page scan budget exceeded')
        result = self.stream.read(size)
        if len(result) != size:
            raise history.HistorySnapshotChanged('shared history was truncated during read')
        self.position += size
        self.scanned += size
        return result


def read_shared_history_page(
    scope: SessionHistoryRange,
    *,
    authorize: Callable[[SessionHistoryRange], bool],
    is_visible: Callable[[dict[str, Any]], bool],
    handle: SharedHistoryHandle | None = None,
    limit: int = 50,
) -> SharedHistoryPage:
    """Return a bounded newest-first page wholly inside a current fixed grant.

    ``is_visible`` is the host's history display predicate, not an execution or
    restore permission. No secondary bucket, attachment or message assembler is
    read. All serialization/chunking remains the host's responsibility, with a
    fresh ``page.revalidate()`` immediately before every delivery.
    """
    if not isinstance(scope, SessionHistoryRange):
        raise TypeError('a server-compiled SessionHistoryRange is required')
    _authorize(authorize, scope)
    if type(limit) is not int or not 1 <= limit <= MAX_PAGE_RECORDS:
        raise ValueError('invalid shared history page limit')
    position = scope.end
    if handle is not None:
        if not isinstance(handle, SharedHistoryHandle) or handle._scope != scope:
            raise history.InvalidHistoryCursor('private history handle belongs to a different grant range')
        payload = history._decode_history_cursor(handle._cursor)
        expected = {'v': history._HISTORY_CURSOR_VERSION, 'stream': scope.stream,
                    'dev': scope.dev, 'ino': scope.ino, 'end': scope.snapshot_end}
        if any(payload.get(key) != value or type(payload.get(key)) is not type(value)
               for key, value in expected.items()):
            raise history.InvalidHistoryCursor('history cursor does not match the fixed grant')
        position = payload.get('pos')
        if type(position) is not int or not scope.start <= position <= scope.end:
            raise history.InvalidHistoryCursor('history cursor is outside the fixed grant')
    records = []
    next_position = position
    scanned_bytes = [0]
    with history._FILE_LOCK:
        with _open(scope.session_id) as (stream, current):
            _validate(stream, current, scope)
            _boundary(stream, position)
            view = _RangeReader(stream, scope.start, position)
            lines = history._iter_reverse_jsonl_lines(
                view, start_position=position - scope.start,
                read_block_bytes=history._HISTORY_CURSOR_READ_BLOCK_BYTES, bytes_read=scanned_bytes,
            )
            scanned_records = 0
            for raw, line_start, _ in lines:
                next_position = scope.start + line_start
                if not raw.strip():
                    continue
                scanned_records += 1
                if len(raw) > MAX_RECORD_BYTES:
                    raise SharedHistoryLimit('shared history record is too large')
                try:
                    item = json.loads(raw)
                except (UnicodeError, ValueError) as exc:
                    raise history.InvalidHistoryCursor('shared history JSONL record changed') from exc
                if not isinstance(item, dict):
                    raise history.InvalidHistoryCursor('shared history record must be an object')
                # Legacy root-stream subagent records confer no implicit child access.
                if not item.get('subagent_id') and is_visible(item) is True:
                    content = item.get('content')
                    if item.get('role') in {'user', 'human'} and isinstance(content, str):
                        item['content'] = history.collapse_file_content_blocks(content)
                    records.append(item)
                if len(records) >= limit or scanned_records >= MAX_SCANNED_RECORDS:
                    break
            else:
                next_position = scope.start
            _validate(stream, os.fstat(stream.fileno()), scope)
    next_handle = None
    if next_position > scope.start:
        token = history._encode_history_cursor({'v': history._HISTORY_CURSOR_VERSION, 'stream': scope.stream,
                'dev': scope.dev, 'ino': scope.ino, 'end': scope.snapshot_end, 'pos': next_position})
        next_handle = SharedHistoryHandle(scope, token)
    page = SharedHistoryPage(tuple(records), next_handle,
                history._history_snapshot_id(stream_key=scope.stream, device=scope.dev, inode=scope.ino,
                                             snapshot_end=scope.snapshot_end),
                scanned_bytes[0], scope, authorize)
    page.revalidate()
    return page
