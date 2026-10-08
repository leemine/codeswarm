"""Single transactional authority for personal evaluation business metadata."""

from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import time
import uuid

from jiuwenswarm.governance.contracts import TrustedIdentity
from ..models import DatasetDraft, ExperimentDraft, TaskDraft, canonical, decode, digest


class CatalogError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def owner(identity: TrustedIdentity) -> str:
    if not isinstance(identity, TrustedIdentity):
        raise PermissionError("trusted identity required")
    return identity.actor_id


class EvaluationStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = path
        self.db = sqlite3.connect(path, isolation_level=None, timeout=5)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version not in {0, 1}:
            self.db.close()
            raise CatalogError("UNSUPPORTED_STORE_SCHEMA")
        if version == 0:
            self.db.executescript("""
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS drafts (
                    owner TEXT NOT NULL, id TEXT NOT NULL, revision INTEGER NOT NULL,
                    body TEXT NOT NULL, PRIMARY KEY(owner,id));
                CREATE TABLE IF NOT EXISTS versions (
                    owner TEXT NOT NULL, kind TEXT NOT NULL, id TEXT NOT NULL,
                    revision INTEGER NOT NULL, digest TEXT NOT NULL, body TEXT NOT NULL,
                    created REAL NOT NULL, PRIMARY KEY(owner,kind,id,revision));
                CREATE TABLE IF NOT EXISTS experiments (
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL, idempotency_key TEXT NOT NULL,
                    fingerprint TEXT NOT NULL, body TEXT NOT NULL, created REAL NOT NULL,
                    UNIQUE(owner,idempotency_key));
                CREATE TABLE IF NOT EXISTS trials (
                    id TEXT PRIMARY KEY, experiment_id TEXT NOT NULL REFERENCES experiments(id),
                    task_id TEXT NOT NULL, task_revision INTEGER NOT NULL, repeat_index INTEGER NOT NULL,
                    UNIQUE(experiment_id,task_id,task_revision,repeat_index));
                CREATE TABLE IF NOT EXISTS attempts (
                    id TEXT PRIMARY KEY, trial_id TEXT NOT NULL REFERENCES trials(id),
                    attempt_index INTEGER NOT NULL, revision INTEGER NOT NULL,
                    phase TEXT NOT NULL, body TEXT NOT NULL, UNIQUE(trial_id,attempt_index));
                PRAGMA user_version=1;
                COMMIT;
            """)
        path.chmod(0o600)

    def close(self):
        self.db.close()

    @contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def save_draft(self, identity, value, expected_revision=0):
        actor = owner(identity)
        draft = decode(TaskDraft, value)
        with self.transaction():
            current = self.db.execute(
                "SELECT revision FROM drafts WHERE owner=? AND id=?",
                (actor, draft.task_id),
            ).fetchone()
            revision = current[0] if current else 0
            if revision != expected_revision:
                raise CatalogError("DRAFT_CONFLICT")
            body = canonical(draft.model_dump(mode="json"))
            self.db.execute(
                "INSERT INTO drafts VALUES(?,?,?,?) ON CONFLICT(owner,id) DO UPDATE SET revision=excluded.revision,body=excluded.body",
                (actor, draft.task_id, revision + 1, body),
            )
        return {"draft_revision": revision + 1, "task": json.loads(body)}

    def list_drafts(self, identity):
        return [
            {"draft_revision": row[0], "task": json.loads(row[1])}
            for row in self.db.execute(
                "SELECT revision,body FROM drafts WHERE owner=? ORDER BY id",
                (owner(identity),),
            )
        ]

    def _publish(self, actor, kind, identifier, value):
        body = canonical(value)
        fingerprint = digest(value)
        last = self.db.execute(
            "SELECT revision,digest FROM versions WHERE owner=? AND kind=? AND id=? ORDER BY revision DESC LIMIT 1",
            (actor, kind, identifier),
        ).fetchone()
        if last and last[1] == fingerprint:
            return self._version(actor, kind, identifier, last[0])
        revision = last[0] + 1 if last else 1
        self.db.execute(
            "INSERT INTO versions VALUES(?,?,?,?,?,?,?)",
            (actor, kind, identifier, revision, fingerprint, body, time.time()),
        )
        return self._version(actor, kind, identifier, revision)

    def publish_task(self, identity, task_id, draft_revision):
        actor = owner(identity)
        with self.transaction():
            draft = self.db.execute(
                "SELECT revision,body FROM drafts WHERE owner=? AND id=?",
                (actor, task_id),
            ).fetchone()
            if draft is None:
                raise CatalogError("NOT_FOUND")
            if draft[0] != draft_revision:
                raise CatalogError("DRAFT_CONFLICT")
            return self._publish(actor, "task", task_id, json.loads(draft[1]))

    def _version(self, actor, kind, identifier, revision):
        row = self.db.execute(
            "SELECT * FROM versions WHERE owner=? AND kind=? AND id=? AND revision=?",
            (actor, kind, identifier, revision),
        ).fetchone()
        if row is None:
            raise CatalogError("NOT_FOUND")
        return {
            "id": row["id"],
            "revision": row["revision"],
            "digest": row["digest"],
            "created": row["created"],
            "value": json.loads(row["body"]),
        }

    def version(self, identity, kind, identifier, revision):
        return self._version(owner(identity), kind, identifier, revision)

    def list_versions(self, identity, kind):
        actor = owner(identity)
        return [
            self._version(actor, kind, row[0], row[1])
            for row in self.db.execute(
                "SELECT id,revision FROM versions WHERE owner=? AND kind=? ORDER BY id,revision DESC",
                (actor, kind),
            )
        ]

    def publish_dataset(self, identity, value):
        actor = owner(identity)
        dataset = decode(DatasetDraft, value)
        with self.transaction():
            for ref in dataset.tasks:
                self._version(actor, "task", ref.task_id, ref.revision)
            return self._publish(
                actor, "dataset", dataset.dataset_id, dataset.model_dump(mode="json")
            )

    def create_experiment(self, identity, value, idempotency_key, *, versions):
        actor = owner(identity)
        definition = decode(ExperimentDraft, value)
        if not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 128:
            raise CatalogError("INVALID_IDEMPOTENCY_KEY")
        fingerprint = digest(definition.model_dump(mode="json"))
        with self.transaction():
            old = self.db.execute(
                "SELECT id,fingerprint FROM experiments WHERE owner=? AND idempotency_key=?",
                (actor, idempotency_key),
            ).fetchone()
            if old:
                if old[1] != fingerprint:
                    raise CatalogError("IDEMPOTENCY_CONFLICT")
                return self.experiment(identity, old[0])
            snapshots = [
                self._version(actor, "task", ref.task_id, ref.revision)
                for ref in definition.tasks
            ]
            if definition.dataset_id:
                dataset = self._version(
                    actor, "dataset", definition.dataset_id, definition.dataset_revision
                )
                allowed = {
                    (ref["task_id"], ref["revision"])
                    for ref in dataset["value"]["tasks"]
                }
                if any(
                    (ref.task_id, ref.revision) not in allowed
                    for ref in definition.tasks
                ):
                    raise CatalogError("TASK_NOT_IN_DATASET")
            identifier = uuid.uuid4().hex
            body = {
                "schema_version": 1,
                "definition": definition.model_dump(mode="json"),
                "tasks": snapshots,
                "versions": versions,
                "seed_support": "unsupported",
                "usage_coverage": "unknown",
            }
            self.db.execute(
                "INSERT INTO experiments VALUES(?,?,?,?,?,?)",
                (
                    identifier,
                    actor,
                    idempotency_key,
                    fingerprint,
                    canonical(body),
                    time.time(),
                ),
            )
            for ref in definition.tasks:
                for repeat in range(definition.repeats):
                    trial = uuid.uuid4().hex
                    self.db.execute(
                        "INSERT INTO trials VALUES(?,?,?,?,?)",
                        (trial, identifier, ref.task_id, ref.revision, repeat),
                    )
                    self.db.execute(
                        "INSERT INTO attempts VALUES(?,?,0,0,'pending',?)",
                        (uuid.uuid4().hex, trial, "{}"),
                    )
        return self.experiment(identity, identifier)

    def experiment(self, identity, identifier):
        row = self.db.execute(
            "SELECT * FROM experiments WHERE id=? AND owner=?",
            (identifier, owner(identity)),
        ).fetchone()
        if row is None:
            raise CatalogError("NOT_FOUND")
        result = {
            "id": identifier,
            "created": row["created"],
            **json.loads(row["body"]),
        }
        result["trials"] = []
        for trial in self.db.execute(
            "SELECT * FROM trials WHERE experiment_id=? ORDER BY rowid", (identifier,)
        ):
            record = dict(trial)
            record["attempts"] = [
                {**dict(attempt), "body": json.loads(attempt["body"])}
                for attempt in self.db.execute(
                    "SELECT * FROM attempts WHERE trial_id=? ORDER BY attempt_index",
                    (trial["id"],),
                )
            ]
            result["trials"].append(record)
        return result

    def list_experiments(self, identity):
        actor = owner(identity)
        return [
            self.experiment(identity, row[0])
            for row in self.db.execute(
                "SELECT id FROM experiments WHERE owner=? ORDER BY created DESC",
                (actor,),
            )
        ]

    def update_attempt(
        self, identity, experiment_id, attempt_id, *, revision, phase, body
    ):
        """CAS of business submission/verification intent, never a Turn state machine."""
        if phase not in {
            "pending",
            "submitting",
            "observing",
            "verifying",
            "settled",
            "unknown",
        }:
            raise CatalogError("INVALID_PHASE")
        self.experiment(identity, experiment_id)
        with self.transaction():
            changed = self.db.execute(
                "UPDATE attempts SET revision=revision+1,phase=?,body=? WHERE id=? AND revision=? AND trial_id IN (SELECT id FROM trials WHERE experiment_id=?)",
                (phase, canonical(body), attempt_id, revision, experiment_id),
            ).rowcount
            if changed != 1:
                raise CatalogError("ATTEMPT_CONFLICT")
        return revision + 1

    def retry_trial(self, identity, experiment_id, trial_id):
        self.experiment(identity, experiment_id)
        with self.transaction():
            row = self.db.execute(
                "SELECT a.attempt_index,a.phase FROM attempts a JOIN trials t ON t.id=a.trial_id WHERE t.experiment_id=? AND t.id=? ORDER BY a.attempt_index DESC LIMIT 1",
                (experiment_id, trial_id),
            ).fetchone()
            if row is None:
                raise CatalogError("NOT_FOUND")
            if row[1] != "settled":
                raise CatalogError("ATTEMPT_NOT_SETTLED")
            self.db.execute(
                "INSERT INTO attempts VALUES(?,?,?,0,'pending','{}')",
                (uuid.uuid4().hex, trial_id, row[0] + 1),
            )
        return self.experiment(identity, experiment_id)
