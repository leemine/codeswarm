from concurrent.futures import ThreadPoolExecutor

import pytest

from jiuwenswarm.extensions.taskboard.backend.store import TaskboardStore
from jiuwenswarm.extensions.taskboard.backend.models import TaskboardError
from jiuwenswarm.extensions.taskboard.backend.service import TaskboardService


def test_retry_reopen_and_version_conflict(tmp_path):
    store = TaskboardStore(tmp_path)
    task = store.create("alice", "request1", {"title": "First"})
    assert store.create("alice", "request1", {"title": "First"}) == task
    with pytest.raises(TaskboardError, match="different data"):
        store.create("alice", "request1", {"title": "Changed"})
    done = store.update(
        "alice", task["task_id"], 1, {"status": "done", "result_note": "artifact"}
    )
    with pytest.raises(TaskboardError) as conflict:
        store.update("alice", task["task_id"], 1, {"title": "stale"})
    assert conflict.value.code == "VERSION_CONFLICT"
    assert (
        TaskboardStore(tmp_path).get("alice", task["task_id"])["result_note"]
        == "artifact"
    )
    assert (
        store.update("alice", task["task_id"], done["version"], {"status": "todo"})[
            "status"
        ]
        == "todo"
    )


def test_owner_and_instance_isolation(tmp_path):
    a = TaskboardStore(tmp_path / "a")
    task = a.create("alice", "same-key", {"title": "private"})
    assert a.list("bob", status="todo")["tasks"] == []
    with pytest.raises(TaskboardError) as err:
        a.get("bob", task["task_id"])
    assert err.value.code == "NOT_FOUND"
    assert TaskboardStore(tmp_path / "b").list("alice", status="todo")["tasks"] == []


def test_concurrent_create_and_compare_update(tmp_path):
    store = TaskboardStore(tmp_path)
    store.create("owner", "warmup", {"title": "warmup"})
    with ThreadPoolExecutor(max_workers=6) as pool:
        tasks = list(
            pool.map(
                lambda i: store.create("owner", str(i), {"title": str(i)}), range(20)
            )
        )
    assert len({t["number"] for t in tasks}) == 20
    target = tasks[0]

    def change(i):
        try:
            store.update("owner", target["task_id"], 1, {"title": str(i)})
            return "success"
        except TaskboardError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(change, range(10)))
    assert results.count("success") == 1
    assert results.count("VERSION_CONFLICT") == 9


def test_literal_search_project_filter_and_cursor(tmp_path):
    store = TaskboardStore(tmp_path)
    for i in range(8):
        store.create("owner", str(i), {"title": f"task {i}", "project_id": "p"})
    first = store.list("owner", status="todo", limit=3)
    second = store.list("owner", status="todo", limit=3, cursor=first["next_cursor"])
    third = store.list("owner", status="todo", limit=3, cursor=second["next_cursor"])
    assert len({x["task_id"] for p in [first, second, third] for x in p["tasks"]}) == 8
    assert third["next_cursor"] is None
    assert len(store.list("owner", status="todo", query="TB-001")["tasks"]) == 1
    assert store.list("owner", status="todo", query="%")["tasks"] == []
    assert store.list("owner", status="todo", project_id="other")["tasks"] == []
    with pytest.raises(TaskboardError):
        store.list("owner", status="todo", cursor="bad!")


@pytest.mark.parametrize(
    "patch",
    [
        {"title": " "},
        {"status": "running"},
        {"priority": "urgent"},
        {"owner": "bob"},
        {"title": "x" * 121},
        {"project_id": []},
        {},
    ],
)
def test_input_validation(tmp_path, patch):
    with pytest.raises(TaskboardError):
        TaskboardStore(tmp_path).create("owner", "req", patch)


def test_reference_unavailable_retained_without_title_leak(tmp_path):
    service = TaskboardService(TaskboardStore(tmp_path))
    params = {"title": "task", "client_create_id": "req", "project_id": "p"}
    task = service.invoke(
        "taskboard.create",
        params,
        owner="o",
        projects={"p": {"name": "private project"}},
        sessions={},
    )["task"]
    with pytest.raises(TaskboardError) as error:
        service.invoke(
            "taskboard.update",
            {
                "task_id": task["task_id"],
                "expected_version": 1,
                "patch": {"linked_session_id": "foreign"},
            },
            owner="o",
            projects={},
            sessions={},
        )
    assert error.value.code == "REFERENCE_UNAVAILABLE"
    read = service.invoke(
        "taskboard.get",
        {"task_id": task["task_id"]},
        owner="o",
        projects={},
        sessions={},
    )["task"]
    assert read["project"] == {"id": "p", "available": False, "title": ""}
    assert read["version"] == 1
