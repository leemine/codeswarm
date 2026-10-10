"""Per-instance SQLite authority. Connections never escape an operation."""

from __future__ import annotations

import base64
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from .models import TaskboardError, validate_patch


class TaskboardStore:
    def __init__(self, root: Path):
        self.path = Path(root) / "taskboard.sqlite3"

    @contextmanager
    def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("PRAGMA synchronous=FULL")
            conn.execute("PRAGMA journal_mode=WAL")
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise TaskboardError(
                    "STORE_VERSION_UNSUPPORTED", "unsupported task database version"
                )
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS tasks (
                    number INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL UNIQUE, owner TEXT NOT NULL,
                    client_create_id TEXT NOT NULL, create_body TEXT NOT NULL,
                    title TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL, priority TEXT NOT NULL,
                    project_id TEXT, linked_session_id TEXT,
                    result_note TEXT NOT NULL DEFAULT '', version INTEGER NOT NULL,
                    created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
                    UNIQUE(owner, client_create_id));
                CREATE INDEX IF NOT EXISTS tasks_board ON tasks(owner, status, updated_at DESC, task_id DESC);
                PRAGMA user_version=1;
            """)
            with conn:
                yield conn
        finally:
            conn.close()

    @staticmethod
    def public(row):
        return {
            k: row[k]
            for k in row.keys()
            if k not in {"owner", "client_create_id", "create_body"}
        }

    def get(self, owner: str, task_id: str) -> dict:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM tasks WHERE owner=? AND task_id=?", (owner, task_id)
            ).fetchone()
            if row is None:
                raise TaskboardError("NOT_FOUND", "task unavailable")
            return self.public(row)

    def create(self, owner: str, key: str, values: dict) -> dict:
        values = validate_patch(values)
        if (
            "title" not in values
            or not isinstance(key, str)
            or not 1 <= len(key) <= 128
        ):
            raise TaskboardError(
                "BAD_REQUEST", "title and client_create_id are required"
            )
        body = json.dumps(values, sort_keys=True, ensure_ascii=False)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute(
                "SELECT * FROM tasks WHERE owner=? AND client_create_id=?", (owner, key)
            ).fetchone()
            if old:
                if old["create_body"] != body:
                    raise TaskboardError(
                        "VERSION_CONFLICT", "creation key reused with different data"
                    )
                return self.public(old)
            now = time.time_ns() // 1000000
            task_id = str(uuid.uuid4())
            db.execute(
                """INSERT INTO tasks(task_id,owner,client_create_id,create_body,title,description,status,priority,
                project_id,linked_session_id,result_note,version,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    task_id,
                    owner,
                    key,
                    body,
                    values["title"],
                    values.get("description", ""),
                    values.get("status", "todo"),
                    values.get("priority", "normal"),
                    values.get("project_id"),
                    values.get("linked_session_id"),
                    values.get("result_note", ""),
                    1,
                    now,
                    now,
                ),
            )
            return self.public(
                db.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            )

    def update(self, owner: str, task_id: str, version: int, patch: dict) -> dict:
        patch = validate_patch(patch)
        if type(version) is not int or version < 1:
            raise TaskboardError(
                "BAD_REQUEST", "expected_version must be a positive integer"
            )
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM tasks WHERE owner=? AND task_id=?", (owner, task_id)
            ).fetchone()
            if not row:
                raise TaskboardError("NOT_FOUND", "task unavailable")
            if row["version"] != version:
                raise TaskboardError(
                    "VERSION_CONFLICT", "task changed; reload before editing"
                )
            now = max(time.time_ns() // 1000000, row["updated_at"] + 1)
            keys = list(patch)
            db.execute(
                f"UPDATE tasks SET {','.join(k + '=?' for k in keys)},version=version+1,updated_at=? WHERE owner=? AND task_id=? AND version=?",
                (*[patch[k] for k in keys], now, owner, task_id, version),
            )
            return self.public(
                db.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            )

    def list(
        self,
        owner: str,
        *,
        status: str,
        project_id=None,
        query="",
        limit=30,
        cursor=None,
    ) -> dict:
        from .models import STATUSES

        if status not in STATUSES or type(limit) is not int or not 1 <= limit <= 100:
            raise TaskboardError("BAD_REQUEST", "invalid status or limit")
        if not isinstance(query, str) or len(query) > 200:
            raise TaskboardError("BAD_REQUEST", "invalid search")
        clauses, args = ["owner=?", "status=?"], [owner, status]
        if project_id is not None:
            if not isinstance(project_id, str) or len(project_id) > 200:
                raise TaskboardError("BAD_REQUEST", "invalid project filter")
            clauses.append("project_id=?")
            args.append(project_id)
        if query.strip():
            # Literal search, not a SQL LIKE pattern supplied by the caller.
            clauses.append(
                "(instr(lower(title),lower(?))>0 OR instr('TB-' || printf('%03d',number),upper(?))>0)"
            )
            args.extend([query.strip(), query.strip()])
        if cursor:
            try:
                if not isinstance(cursor, str) or len(cursor) > 512:
                    raise ValueError()
                stamp, tid = json.loads(base64.urlsafe_b64decode(cursor))
                if type(stamp) is not int or not isinstance(tid, str):
                    raise ValueError()
            except (ValueError, TypeError, UnicodeError) as exc:
                raise TaskboardError("BAD_REQUEST", "invalid cursor") from exc
            clauses.append("(updated_at<? OR (updated_at=? AND task_id<?))")
            args.extend([stamp, stamp, tid])
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM tasks WHERE "
                + " AND ".join(clauses)
                + " ORDER BY updated_at DESC,task_id DESC LIMIT ?",
                (*args, limit + 1),
            ).fetchall()
            more = len(rows) > limit
            rows = rows[:limit]
            next_cursor = (
                base64.urlsafe_b64encode(
                    json.dumps([rows[-1]["updated_at"], rows[-1]["task_id"]]).encode()
                ).decode()
                if more
                else None
            )
            return {"tasks": [self.public(r) for r in rows], "next_cursor": next_cursor}
