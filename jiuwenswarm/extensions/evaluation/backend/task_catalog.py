"""Manual and JSONL inputs share one strict versioned catalog."""

import json

from pydantic import ValidationError

from .adapters.store import CatalogError, owner
from .models import TaskDraft, canonical, decode

MAX_IMPORT_BYTES = 2 * 1024 * 1024


def _unique_keys(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate key")
        value[key] = item
    return value


class TaskCatalog:
    def __init__(self, store):
        self.store = store

    def preview_jsonl(self, identity, text):
        owner(identity)
        if not isinstance(text, str) or len(text.encode()) > MAX_IMPORT_BYTES:
            raise CatalogError("IMPORT_TOO_LARGE")
        rows, seen = [], set()
        existing = {
            item["task"]["task_id"] for item in self.store.list_drafts(identity)
        }
        existing.update(
            item["id"] for item in self.store.list_versions(identity, "task")
        )
        for line, raw in enumerate(text.splitlines(), 1):
            if not raw.strip():
                continue
            if len(rows) >= 100:
                raise CatalogError("TOO_MANY_IMPORT_TASKS")
            try:
                value = json.loads(raw, object_pairs_hook=_unique_keys)
                task = decode(TaskDraft, value)
                if task.task_id in seen or task.task_id in existing:
                    raise CatalogError("TASK_ID_CONFLICT")
                seen.add(task.task_id)
                rows.append(
                    {"line": line, "task": task.model_dump(mode="json"), "errors": []}
                )
            except ValidationError as exc:
                # Do not echo submitted values or arbitrary extra-field names.
                rows.append(
                    {
                        "line": line,
                        "errors": [
                            {
                                "code": "INVALID_TASK",
                                "field": str(error["loc"][0])
                                if error["loc"]
                                and error["loc"][0] in TaskDraft.model_fields
                                else "task",
                            }
                            for error in exc.errors(
                                include_input=False, include_url=False
                            )
                        ],
                    }
                )
            except (ValueError, TypeError, RecursionError) as exc:
                rows.append(
                    {
                        "line": line,
                        "errors": [
                            {
                                "code": exc.code
                                if isinstance(exc, CatalogError)
                                else "INVALID_JSON",
                                "field": "task",
                            }
                        ],
                    }
                )
        return {
            "rows": rows,
            "can_import": bool(rows) and all(not row["errors"] for row in rows),
        }

    def import_jsonl(self, identity, text):
        actor = owner(identity)
        # Revalidate inside the database write lock; stale previews grant nothing.
        with self.store.transaction():
            preview = self.preview_jsonl(identity, text)
            if not preview["can_import"]:
                return preview
            for row in preview["rows"]:
                task = row["task"]
                self.store.db.execute(
                    "INSERT INTO drafts VALUES(?,?,1,?)",
                    (actor, task["task_id"], canonical(task)),
                )
        return {**preview, "imported": len(preview["rows"])}
