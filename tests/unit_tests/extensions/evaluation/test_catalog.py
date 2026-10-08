"""Durability, immutable snapshots, ownership and hostile import boundaries."""

import json
import sqlite3

import pytest
from pydantic import ValidationError

from jiuwenswarm.extensions.evaluation.backend.adapters.store import (
    CatalogError,
    EvaluationStore,
)
from jiuwenswarm.extensions.evaluation.backend.models import TaskDraft, decode
from jiuwenswarm.extensions.evaluation.backend.task_catalog import TaskCatalog
from jiuwenswarm.governance.contracts import TrustedIdentity

ALICE = TrustedIdentity("alice", "alice", "test")
BOB = TrustedIdentity("bob", "bob", "test")


@pytest.fixture
def store(tmp_path):
    value = EvaluationStore(tmp_path / "evaluation.sqlite3")
    yield value
    value.close()


def task(identifier="sum", **changes):
    return {
        "schema_version": 1,
        "task_id": identifier,
        "name": "Add",
        "instruction": "Add two numbers",
        **changes,
    }


def publish(store, identity=ALICE):
    store.save_draft(identity, task())
    return store.publish_task(identity, "sum", 1)


def definition(**changes):
    return {
        "name": "experiment",
        "tasks": [{"task_id": "sum", "revision": 1}],
        "model": "test-model",
        "execution_profile_id": "native",
        "shared_environment_acknowledged": True,
        **changes,
    }


def experiment(store, key="key"):
    return store.create_experiment(
        ALICE, definition(), key, versions={"plugin": "1.0.0"}
    )


def test_frozen_task_experiment_and_reopen(store):
    original = publish(store)
    first = experiment(store)
    store.save_draft(ALICE, task(instruction="Changed"), expected_revision=1)
    second = store.publish_task(ALICE, "sum", 2)
    assert second["revision"] == 2 and second["digest"] != original["digest"]
    other = EvaluationStore(store.path)
    try:
        assert other.experiment(ALICE, first["id"])["tasks"][0] == original
        assert other.version(ALICE, "task", "sum", 1) == original
    finally:
        other.close()


def test_idempotency_and_independent_names(store):
    publish(store)
    first = experiment(store)
    assert experiment(store)["id"] == first["id"]
    second = experiment(store, "other")
    assert (
        second["trials"][0]["attempts"][0]["id"]
        != first["trials"][0]["attempts"][0]["id"]
    )
    with pytest.raises(CatalogError, match="IDEMPOTENCY_CONFLICT"):
        store.create_experiment(ALICE, definition(name="other"), "key", versions={})


def test_owner_checked_on_every_read_and_write(store):
    publish(store)
    exp = experiment(store)
    with pytest.raises(CatalogError, match="NOT_FOUND"):
        store.experiment(BOB, exp["id"])
    with pytest.raises(CatalogError, match="NOT_FOUND"):
        store.version(BOB, "task", "sum", 1)
    with pytest.raises(CatalogError, match="NOT_FOUND"):
        store.create_experiment(BOB, definition(), "key", versions={})
    with pytest.raises(PermissionError):
        store.list_drafts({"actor_id": "alice"})
    assert store.list_experiments(BOB) == []


def test_attempt_cas_and_retry_not_new_sample(store):
    publish(store)
    exp = experiment(store)
    trial = exp["trials"][0]
    attempt = trial["attempts"][0]
    store.update_attempt(
        ALICE, exp["id"], attempt["id"], revision=0, phase="unknown", body={}
    )
    with pytest.raises(CatalogError, match="ATTEMPT_NOT_SETTLED"):
        store.retry_trial(ALICE, exp["id"], trial["id"])
    with pytest.raises(CatalogError, match="ATTEMPT_CONFLICT"):
        store.update_attempt(
            ALICE, exp["id"], attempt["id"], revision=0, phase="settled", body={}
        )
    store.update_attempt(
        ALICE,
        exp["id"],
        attempt["id"],
        revision=1,
        phase="settled",
        body={"outcome": "failed"},
    )
    retried = store.retry_trial(ALICE, exp["id"], trial["id"])
    assert len(retried["trials"]) == 1
    assert [a["attempt_index"] for a in retried["trials"][0]["attempts"]] == [0, 1]


@pytest.mark.parametrize(
    "path",
    [
        "../secret",
        "/etc/passwd",
        "a/../b",
        "a//b",
        "a\\b",
        ".git/config",
        "a/.evaluation/test",
        "a\x00b",
        "C:foo",
    ],
)
def test_unsafe_paths_rejected(path):
    with pytest.raises(ValidationError):
        decode(TaskDraft, task(files=[{"path": path, "content": "x"}]))


@pytest.mark.parametrize(
    "value",
    [
        task(schema_version=2),
        task(owner="alice"),
        task(api_key="secret"),
        task(files=[{"path": "a", "content": ""}, {"path": "a/b", "content": ""}]),
        task(acceptance={"kind": "python"}),
    ],
)
def test_schema_and_material_errors(value):
    with pytest.raises(ValidationError):
        decode(TaskDraft, value)


def test_import_line_errors_and_atomicity(store):
    catalog = TaskCatalog(store)
    text = json.dumps(task()) + '\n\n{"schema_version":2}\n'
    preview = catalog.import_jsonl(ALICE, text)
    assert preview["rows"][1]["line"] == 3 and not preview["can_import"]
    assert store.list_drafts(ALICE) == []
    assert catalog.import_jsonl(ALICE, json.dumps(task()))["imported"] == 1
    assert not catalog.preview_jsonl(ALICE, json.dumps(task()))["can_import"]
    assert not catalog.preview_jsonl(ALICE, '{"task_id":"a","task_id":"b"}')[
        "can_import"
    ]


def test_dataset_subset_and_stale_draft(store):
    publish(store)
    with pytest.raises(CatalogError, match="DRAFT_CONFLICT"):
        store.save_draft(ALICE, task(), 0)
    dataset = store.publish_dataset(
        ALICE,
        {
            "dataset_id": "tiny",
            "name": "Tiny",
            "tasks": [{"task_id": "sum", "revision": 1}],
        },
    )
    assert dataset["revision"] == 1
    created = store.create_experiment(
        ALICE, definition(dataset_id="tiny", dataset_revision=1), "dataset", versions={}
    )
    assert created["definition"]["dataset_revision"] == 1


def test_unknown_database_schema_rejected(tmp_path):
    path = tmp_path / "future.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA user_version=99")
    with pytest.raises(CatalogError, match="UNSUPPORTED_STORE_SCHEMA"):
        EvaluationStore(path)
