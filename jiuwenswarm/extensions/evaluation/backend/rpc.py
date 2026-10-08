"""Fixed personal-scope RPC methods, consumed only inside AgentServer."""

from pathlib import Path

from .adapters.store import CatalogError, EvaluationStore, owner
from .task_catalog import TaskCatalog
from .trials import Trials

METHODS = frozenset(
    {
        "evaluation.catalog",
        "evaluation.task.save",
        "evaluation.task.publish",
        "evaluation.import.preview",
        "evaluation.import.commit",
        "evaluation.dataset.publish",
        "evaluation.options",
        "evaluation.experiment.create",
        "evaluation.experiment.list",
        "evaluation.experiment.get",
        "evaluation.experiment.start",
        "evaluation.experiment.cancel",
        "evaluation.evidence",
        "evaluation.examples",
    }
)


class EvaluationService:
    def __init__(self, *, runtime, data_root: Path, send_push=None):
        self.runtime = runtime
        self.root = data_root / "evaluation"
        self.store = EvaluationStore(self.root / "metadata.sqlite3")
        self.catalog = TaskCatalog(self.store)
        self.trials = Trials(self.store, self.root, runtime, send_push=send_push)
        self.closed = False

    async def close(self):
        if not self.closed:
            await self.trials.close()
            self.closed = True
            self.store.close()

    async def call(self, method, params, identity):
        owner(identity)
        if self.closed or method not in METHODS:
            raise CatalogError("EVALUATION_UNAVAILABLE")
        if not isinstance(params, dict):
            raise CatalogError("INVALID_PARAMS")
        allowed = {
            "evaluation.catalog": set(),
            "evaluation.task.save": {"task", "expected_revision"},
            "evaluation.task.publish": {"task_id", "draft_revision"},
            "evaluation.import.preview": {"text"},
            "evaluation.import.commit": {"text"},
            "evaluation.dataset.publish": {"dataset"},
            "evaluation.examples": set(),
            "evaluation.options": set(),
            "evaluation.experiment.create": {"experiment", "idempotency_key"},
            "evaluation.experiment.list": set(),
            "evaluation.experiment.get": {"experiment_id"},
            "evaluation.experiment.start": {"experiment_id"},
            "evaluation.experiment.cancel": {"experiment_id"},
            "evaluation.evidence": {"experiment_id"},
        }
        if method in allowed:
            if set(params) - allowed[method]:
                raise CatalogError("INVALID_PARAMS")
            if method == "evaluation.options":
                value = await self.trials.execution.options()
                value["execution_available"] = (
                    identity.authority == "local-single-user-installation"
                )
                return value
            if method == "evaluation.experiment.create":
                return await self.trials.create(
                    identity, params.get("experiment"), params.get("idempotency_key")
                )
            if method == "evaluation.experiment.list":
                return {
                    "experiments": [
                        self.trials.get(identity, item["id"])
                        for item in self.store.list_experiments(identity)
                    ]
                }
            if method in {"evaluation.experiment.get", "evaluation.evidence"}:
                return self.trials.get(identity, params.get("experiment_id"))
            if method == "evaluation.experiment.start":
                return await self.trials.start(identity, params.get("experiment_id"))
            if method == "evaluation.experiment.cancel":
                return await self.trials.cancel(identity, params.get("experiment_id"))
            if method == "evaluation.catalog":
                return {
                    "schema_version": 1,
                    "drafts": self.store.list_drafts(identity),
                    "tasks": self.store.list_versions(identity, "task"),
                    "datasets": self.store.list_versions(identity, "dataset"),
                }
            if method == "evaluation.task.save":
                revision = params.get("expected_revision", 0)
                if type(revision) is not int or revision < 0:
                    raise CatalogError("INVALID_REVISION")
                return self.store.save_draft(identity, params.get("task"), revision)
            if method == "evaluation.task.publish":
                if type(params.get("draft_revision")) is not int:
                    raise CatalogError("INVALID_REVISION")
                return self.store.publish_task(
                    identity, params.get("task_id"), params["draft_revision"]
                )
            if method == "evaluation.import.preview":
                return self.catalog.preview_jsonl(identity, params.get("text"))
            if method == "evaluation.import.commit":
                return self.catalog.import_jsonl(identity, params.get("text"))
            if method == "evaluation.dataset.publish":
                return self.store.publish_dataset(identity, params.get("dataset"))
            if method == "evaluation.examples":
                return self.examples(identity)
        raise CatalogError("EVALUATION_UNAVAILABLE")

    def examples(self, identity):
        tasks = [
            {
                "task_id": "m1-add",
                "name": "Addition / 加法",
                "instruction": "Implement add(a, b) in solution.py to return a + b.",
                "files": [
                    {"path": "solution.py", "content": "def add(a, b):\n    return 0\n"}
                ],
                "deliverables": ["solution.py"],
                "acceptance": {
                    "kind": "python",
                    "script": "from solution import add\nassert add(2,3)==5\nassert add(-2,2)==0\n",
                },
            },
            {
                "task_id": "m1-unicode",
                "name": "Unicode / 中文文件",
                "instruction": "Write result.txt containing exactly 你好，评测！ followed by a newline.",
                "deliverables": ["result.txt"],
                "acceptance": {
                    "kind": "python",
                    "script": "from pathlib import Path\nassert Path('result.txt').read_text() == '你好，评测！\\n'\n",
                },
            },
            {
                "task_id": "m1-repair",
                "name": "Repair / 修复",
                "instruction": "Fix add(a, b) in solution.py so it adds both numbers.",
                "files": [
                    {
                        "path": "solution.py",
                        "content": "def add(a, b):\n    return a - b\n",
                    }
                ],
                "deliverables": ["solution.py"],
                "acceptance": {
                    "kind": "python",
                    "script": "from solution import add\nassert add(3,2)==5\n",
                },
            },
        ]
        # One transaction also protects a concurrent first click by another connection.
        from ..backend.models import TaskDraft, decode

        actor = owner(identity)
        with self.store.transaction():
            refs = []
            for value in tasks:
                task = decode(TaskDraft, value)
                try:
                    version = self.store._version(actor, "task", task.task_id, 1)
                    if version["value"] != task.model_dump(mode="json"):
                        raise CatalogError("EXAMPLE_ID_CONFLICT")
                except CatalogError as exc:
                    if exc.code != "NOT_FOUND":
                        raise
                    version = self.store._publish(
                        actor, "task", task.task_id, task.model_dump(mode="json")
                    )
                refs.append({"task_id": task.task_id, "revision": version["revision"]})
            self.store._publish(
                actor,
                "dataset",
                "m1-micro",
                {
                    "schema_version": 1,
                    "dataset_id": "m1-micro",
                    "name": "M1 micro tasks / 微型任务",
                    "tasks": refs,
                    "source": "bundled-m1-v1",
                    "license": "Apache-2.0",
                },
            )
        return {"created": True}
