"""Manual task use cases. Reference authority is supplied by the instance host."""

from .models import TaskboardError, validate_patch


class TaskboardService:
    def __init__(self, store):
        self.store = store

    def invoke(self, method, params, *, owner, projects, sessions):
        def project(task):
            pid = task.get("project_id")
            return (
                {
                    "id": pid,
                    "available": pid in projects,
                    "title": projects.get(pid, {}).get("name", ""),
                }
                if pid
                else None
            )

        def present(task):
            sid = task["linked_session_id"]
            meta = sessions.get(sid)
            return {
                **task,
                "project": project(task),
                "linked_session": (
                    {
                        "id": sid,
                        "available": meta is not None,
                        "title": (meta.get("display_title") or meta.get("title") or sid)
                        if meta
                        else "",
                    }
                    if sid
                    else None
                ),
            }

        def validate_refs(patch):
            for field, available in [
                ("project_id", projects),
                ("linked_session_id", sessions),
            ]:
                if patch.get(field) and patch[field] not in available:
                    raise TaskboardError(
                        "REFERENCE_UNAVAILABLE",
                        "reference unavailable or access denied",
                    )

        if method in {"taskboard.get", "taskboard.update"}:
            task_id = params.get("task_id")
            if not isinstance(task_id, str) or not 1 <= len(task_id) <= 200:
                raise TaskboardError("BAD_REQUEST", "invalid task_id")

        if method == "taskboard.create":
            allowed = {
                "title",
                "description",
                "priority",
                "project_id",
                "client_create_id",
            }
            if set(params) - allowed:
                raise TaskboardError("BAD_REQUEST", "invalid creation fields")
            values = validate_patch(
                {k: v for k, v in params.items() if k != "client_create_id"}
            )
            validate_refs(values)
            return {
                "task": present(
                    self.store.create(owner, params.get("client_create_id"), values)
                )
            }
        if method == "taskboard.list":
            if set(params) - {"status", "project_id", "query", "limit", "cursor"}:
                raise TaskboardError("BAD_REQUEST", "invalid list fields")
            result = self.store.list(owner, **params)
            return {**result, "tasks": [present(t) for t in result["tasks"]]}
        if method == "taskboard.get":
            if set(params) != {"task_id"}:
                raise TaskboardError("BAD_REQUEST", "task_id is required")
            return {"task": present(self.store.get(owner, params["task_id"]))}
        if method == "taskboard.update":
            if set(params) != {"task_id", "expected_version", "patch"}:
                raise TaskboardError("BAD_REQUEST", "invalid update fields")
            patch = validate_patch(params["patch"])
            validate_refs(patch)
            return {
                "task": present(
                    self.store.update(
                        owner, params["task_id"], params["expected_version"], patch
                    )
                )
            }
        raise TaskboardError("BAD_REQUEST", "unsupported taskboard method")
