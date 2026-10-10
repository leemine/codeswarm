"""Business contracts over a controlled execution port plus real verifier processes."""

import asyncio
import importlib.metadata
import json
from types import SimpleNamespace

import pytest

from jiuwenswarm.extensions.evaluation.backend.adapters.store import (
    CatalogError,
    EvaluationStore,
)
from jiuwenswarm.extensions.evaluation.backend.adapters.verification import (
    SharedHostVerifier,
)
from jiuwenswarm.extensions.evaluation.backend.models import TaskDraft, decode
from jiuwenswarm.extensions.evaluation.backend.trials import Trials
from jiuwenswarm.extensions.evaluation.backend.results import implementation_source
from jiuwenswarm.governance.contracts import TrustedIdentity
from jiuwenswarm.runtime.session.model import SessionExecutionState

ACTOR = TrustedIdentity("local", "local", "local-single-user-installation")
OTHER = TrustedIdentity("other", "other", "test")


class Port:
    def __init__(
        self, state=SessionExecutionState.SUCCEEDED, wait=False, missing=False
    ):
        self.state, self.wait, self.missing = state, wait, missing
        self.calls = 0
        self.cancelled = asyncio.Event()
        self.entered = asyncio.Event()
        self.finished = False
        self.config = {"model": "fixture"}
        self.root = None

    async def configuration(self, definition):
        await asyncio.sleep(0)
        return self.config

    async def prepare(self, *args, **kwargs):
        return SimpleNamespace(result=SimpleNamespace(session_id="session-fixture"))

    def workspace(self, session_id):
        path = self.root / session_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    async def commit(self, prepared):
        pass

    async def observe(self, **kwargs):
        self.calls += 1
        self.entered.set()
        if self.wait:
            await self.cancelled.wait()
            self.state = SessionExecutionState.CANCELLED
            self.finished = True
            raise asyncio.CancelledError()
        self.finished = True

    def snapshot(self, *args):
        if self.missing:
            return None
        return SimpleNamespace(
            state=self.state if self.finished else SessionExecutionState.RUNNING,
            generation=1,
            execution_id="exec",
            waiting_control_ids=(),
        )

    async def cancel(self, *args):
        self.cancelled.set()


def setup(tmp_path, monkeypatch, script="assert True", port=None):
    monkeypatch.setattr(
        "jiuwenswarm.common.projectless_workspace.get_projectless_tasks_dir",
        lambda: tmp_path / "tasks",
    )
    store = EvaluationStore(tmp_path / "metadata.sqlite3")
    value = {
        "task_id": "task",
        "name": "Task",
        "instruction": "Do work",
        "files": [{"path": "solution.py", "content": "x = 1\n"}],
        "deliverables": ["solution.py"],
        "acceptance": {"kind": "python", "script": script}
        if script
        else {"kind": "manual"},
    }
    draft = store.save_draft(ACTOR, value, 0)
    store.publish_task(ACTOR, "task", draft["draft_revision"])
    trials = Trials(store, tmp_path, None)
    trials.execution = port or Port()
    trials.execution.root = tmp_path / "tasks"
    definition = {
        "name": "Experiment",
        "tasks": [{"task_id": "task", "revision": 1}],
        "model": "fixture",
        "execution_profile_id": "native",
        "shared_environment_acknowledged": True,
    }
    exp = store.create_experiment(
        ACTOR, definition, "key", versions={
            "configuration": {"model": "fixture"},
            "implementation": implementation_source(),
            "core": importlib.metadata.version("openjiuwen"),
            "swarm": importlib.metadata.version("workswarm"),
            "core_source": json.loads(importlib.metadata.distribution("openjiuwen").read_text("direct_url.json") or "null"),
        }
    )
    return trials, exp


async def finish(trials, exp):
    await asyncio.wait_for(trials.workers[exp["id"]], 5)
    return trials.get(ACTOR, exp["id"])["trials"][0]["attempts"][0]


@pytest.mark.asyncio
@pytest.mark.parametrize("failed", [False, True])
async def test_long_reasoning_burst_does_not_rewrite_status_per_delta(
    tmp_path, monkeypatch, failed
):
    class BurstPort(Port):
        async def observe(self, **kwargs):
            self.calls += 1
            for _ in range(10_000):
                await kwargs["on_event"](SimpleNamespace(
                    ok=True, event_type="chat.reasoning", payload={"content": "x"}
                ))
            await kwargs["on_event"](SimpleNamespace(
                ok=True, event_type="chat.ask_user_question", payload={}
            ))
            if failed:
                # Same event type must not suppress later error diagnostics.
                for error in ("first failure", "second failure"):
                    await kwargs["on_event"](SimpleNamespace(
                        ok=False, event_type="chat.error", payload={"error": error}
                    ))
            self.finished = True

    trials, exp = setup(tmp_path, monkeypatch, port=BurstPort())
    writes = []
    update = trials.store.update_attempt

    def counted(*args, **kwargs):
        writes.append(kwargs)
        return update(*args, **kwargs)

    monkeypatch.setattr(trials.store, "update_attempt", counted)
    try:
        await trials.start(ACTOR, exp["id"])
        result = await finish(trials, exp)
        assert len(writes) < 25
        assert result["body"]["exit_confirmed"]
        if failed:
            assert result["body"]["outcome"] == "execution_failed"
            assert result["body"]["runtime_error"] == "second failure"
            assert result["body"]["last_event_type"] == "chat.error"
        else:
            assert result["body"]["outcome"] == "passed"
            assert result["body"]["last_event_type"] == "chat.ask_user_question"
    finally:
        await trials.close()
        trials.store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("script", "outcome"),
    [
        ("assert True", "passed"),
        ("assert False", "test_failed"),
        ("import nonexistent_evaluation_module", "environment_error"),
        ("", "awaiting_manual_review"),
    ],
)
async def test_result_classification_and_repeated_start(
    tmp_path, monkeypatch, script, outcome
):
    trials, exp = setup(tmp_path, monkeypatch, script)
    try:
        await asyncio.gather(
            trials.start(ACTOR, exp["id"]), trials.start(ACTOR, exp["id"])
        )
        result = await finish(trials, exp)
        assert result["phase"] == "settled"
        assert result["body"]["outcome"] == outcome
        assert result["body"]["exit_confirmed"]
        assert result["body"]["files"][0]["status"] == "unchanged"
        await trials.start(ACTOR, exp["id"])
        await finish(trials, exp)
        assert trials.execution.calls == 1
        assert trials.get(ACTOR, exp["id"])["statistics"]["cost"] is None
        with pytest.raises(CatalogError, match="NOT_FOUND"):
            trials.get(OTHER, exp["id"])
    finally:
        await trials.close()
        trials.store.close()


@pytest.mark.asyncio
async def test_stream_close_missing_receipt_never_passes_or_resubmits(
    tmp_path, monkeypatch
):
    trials, exp = setup(tmp_path, monkeypatch, port=Port(missing=True))
    try:
        await trials.start(ACTOR, exp["id"])
        result = await finish(trials, exp)
        assert result["phase"] == "unknown"
        assert not result["body"]["exit_confirmed"]
        assert result["body"]["error_code"] == "EXECUTION_OUTCOME_UNKNOWN"
        for _ in range(3):
            assert not trials.get(ACTOR, exp["id"])["active"]
        with pytest.raises(CatalogError, match="UNRESOLVED_ATTEMPT"):
            await trials.start(ACTOR, exp["id"])
        assert trials.execution.calls == 1
    finally:
        await trials.close()
        trials.store.close()


@pytest.mark.asyncio
async def test_cancel_waits_for_original_execution_and_refresh_only_reads(
    tmp_path, monkeypatch
):
    trials, exp = setup(tmp_path, monkeypatch, port=Port(wait=True))
    try:
        await trials.start(ACTOR, exp["id"])
        await trials.execution.entered.wait()
        for _ in range(3):
            assert (
                trials.get(ACTOR, exp["id"])["trials"][0]["attempts"][0]["phase"]
                == "observing"
            )
        await trials.cancel(ACTOR, exp["id"])
        result = await finish(trials, exp)
        assert result["body"]["outcome"] == "cancelled"
        assert result["body"]["exit_confirmed"]
        assert trials.execution.calls == 1
    finally:
        await trials.close()
        trials.store.close()


@pytest.mark.asyncio
async def test_configuration_drift_and_foreign_execution_denied(tmp_path, monkeypatch):
    trials, exp = setup(tmp_path, monkeypatch)
    try:
        trials.execution.config = {"model": "changed"}
        with pytest.raises(CatalogError, match="CONFIGURATION_CHANGED"):
            await trials.start(ACTOR, exp["id"])
        with pytest.raises(CatalogError, match="SHARED_HOST_LOCAL_ONLY"):
            await trials.start(OTHER, exp["id"])
        assert trials.execution.calls == 0
    finally:
        await trials.close()
        trials.store.close()


@pytest.mark.asyncio
async def test_real_verifier_timeout_and_secret_isolation(tmp_path, monkeypatch):
    monkeypatch.setenv("MODEL_API_KEY", "must-not-inherit")
    verifier = SharedHostVerifier()
    task = decode(
        TaskDraft,
        {
            "task_id": "test",
            "name": "Test",
            "instruction": "Test",
            "acceptance": {
                "kind": "python",
                "timeout_seconds": 1,
                "script": 'import os,time\nassert "MODEL_API_KEY" not in os.environ\ntime.sleep(10)',
            },
        },
    )
    result = await verifier.verify("attempt", tmp_path, task)
    assert result["outcome"] == "verification_timeout"
    assert result["returncode"] < 0
    assert not verifier.processes
    assert result["exit_confirmed"]


@pytest.mark.asyncio
async def test_runtime_control_adapter_uses_original_cancel_contract():
    from unittest.mock import AsyncMock
    from jiuwenswarm.extensions.evaluation.backend.adapters.runtime_execution import (
        RuntimeExecution,
    )
    from jiuwenswarm.common.schema.agent import AgentResponse
    from jiuwenswarm.common.schema.message import ReqMethod

    runtime = SimpleNamespace(
        get_session_request_executions=lambda *args, **kwargs: (),
        cancel_request=AsyncMock(
            return_value=AgentResponse(
                request_id="control",
                channel_id="web",
                ok=True,
                payload={"success": True},
            )
        )
    )
    await RuntimeExecution(runtime).cancel("session", "attempt")
    sent = runtime.cancel_request.call_args.args[0]
    assert sent.req_method is ReqMethod.CHAT_CANCEL
    assert sent.session_id == "session"
    assert sent.params["target_request_id"] == "attempt"
    assert sent.params["intent"] == "cancel"
    runtime.cancel_request.return_value = AgentResponse(
        request_id="control", channel_id="web", ok=False
    )
    with pytest.raises(CatalogError, match="EXIT_NOT_CONFIRMED"):
        await RuntimeExecution(runtime).cancel("session", "attempt")


@pytest.mark.asyncio
async def test_failed_runtime_never_runs_passing_acceptance(tmp_path, monkeypatch):
    trials, exp = setup(tmp_path, monkeypatch, port=Port(state=SessionExecutionState.FAILED))
    try:
        await trials.start(ACTOR, exp["id"])
        result = await finish(trials, exp)
        assert result["body"]["outcome"] == "execution_failed"
        assert result["body"]["runtime_terminal"] == "failed"
        assert not (trials.execution.root / "session-fixture" / ".evaluation").exists()
        assert trials.get(ACTOR, exp["id"])["statistics"]["passed"] == 0
    finally:
        await trials.close()
        trials.store.close()


@pytest.mark.asyncio
async def test_timeout_requires_original_cancel_exit_before_settlement(tmp_path, monkeypatch):
    import time
    from jiuwenswarm.extensions.evaluation.backend import trials as module
    trials, exp = setup(tmp_path, monkeypatch, port=Port(wait=True))
    now = [0.0]
    monkeypatch.setattr(module, "time", SimpleNamespace(time=time.time, monotonic=lambda: now[0]))
    try:
        await trials.start(ACTOR, exp["id"])
        await trials.execution.entered.wait()
        now[0] = 301.0
        result = await finish(trials, exp)
        assert trials.execution.cancelled.is_set()
        assert trials.execution.finished
        assert result["body"]["outcome"] == "execution_timeout"
        assert result["body"]["exit_confirmed"] is True
    finally:
        await trials.close()
        trials.store.close()


@pytest.mark.asyncio
async def test_cancel_targets_live_control_after_original_receipt_completed():
    from unittest.mock import AsyncMock
    from jiuwenswarm.common.schema.agent import AgentResponse
    from jiuwenswarm.extensions.evaluation.backend.adapters.runtime_execution import RuntimeExecution

    runtime = SimpleNamespace(
        get_session_request_executions=lambda *args, **kwargs: (
            SimpleNamespace(request_id="attempt", state=SessionExecutionState.SUCCEEDED),
            SimpleNamespace(request_id="question-1", state=SessionExecutionState.WAITING_FOR_CONTROL),
        ),
        cancel_request=AsyncMock(return_value=AgentResponse(
            request_id="cancel", channel_id="web", ok=True, payload={"success": True})),
    )
    await RuntimeExecution(runtime).cancel("session", "attempt")
    assert runtime.cancel_request.await_count == 1
    assert runtime.cancel_request.call_args.args[0].params["target_request_id"] == "question-1"


@pytest.mark.asyncio
async def test_changed_implementation_cannot_start_frozen_experiment(tmp_path, monkeypatch):
    trials, exp = setup(tmp_path, monkeypatch)
    frozen = exp["versions"]["implementation"]
    monkeypatch.setattr(
        "jiuwenswarm.extensions.evaluation.backend.trials.implementation_source",
        lambda: {**frozen, "sha256": "changed-after-freeze"},
    )
    try:
        with pytest.raises(CatalogError, match="IMPLEMENTATION_CHANGED"):
            await trials.start(ACTOR, exp["id"])
        assert trials.execution.calls == 0
        assert trials.get(ACTOR, exp["id"])["trials"][0]["attempts"][0]["phase"] == "pending"
    finally:
        await trials.close()
        trials.store.close()


@pytest.mark.asyncio
async def test_installed_location_does_not_change_source_identity(tmp_path, monkeypatch):
    trials, exp = setup(tmp_path, monkeypatch)
    frozen = exp["versions"]["implementation"]
    monkeypatch.setattr(
        "jiuwenswarm.extensions.evaluation.backend.trials.implementation_source",
        lambda: {**frozen, "package_path": "/another/clean/install", "git_head": "rewritten-history"},
    )
    try:
        await trials.start(ACTOR, exp["id"])
        result = await finish(trials, exp)
        assert result["body"]["outcome"] == "passed"
    finally:
        await trials.close()
        trials.store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["core", "swarm", "core_source"])
async def test_dependency_drift_rejected_before_submission(tmp_path, monkeypatch, field):
    trials, exp = setup(tmp_path, monkeypatch)
    current = trials._sources()
    monkeypatch.setattr(trials, "_sources", lambda: {**current, field: "changed"})
    try:
        with pytest.raises(CatalogError, match="DEPENDENCY_CHANGED"):
            await trials.start(ACTOR, exp["id"])
        assert not trials.workers
        assert trials.execution.calls == 0
        # Read and cancellation remain available even for obsolete snapshots.
        await trials.cancel(ACTOR, exp["id"])
        assert trials.get(ACTOR, exp["id"])["trials"][0]["attempts"][0]["body"]["outcome"] == "cancelled"
    finally:
        await trials.close()
        trials.store.close()


@pytest.mark.asyncio
async def test_source_drift_after_start_is_rechecked_before_session_creation(tmp_path, monkeypatch):
    trials, exp = setup(tmp_path, monkeypatch)
    configuration = trials.execution.configuration
    calls = 0

    async def changed(definition):
        nonlocal calls
        calls += 1
        if calls == 2:
            monkeypatch.setattr(
                "jiuwenswarm.extensions.evaluation.backend.trials.implementation_source",
                lambda: {"sha256": "changed-between-trials"},
            )
        return await configuration(definition)

    monkeypatch.setattr(trials.execution, "configuration", changed)
    try:
        await trials.start(ACTOR, exp["id"])
        result = await finish(trials, exp)
        assert result["body"]["error_code"] == "IMPLEMENTATION_CHANGED"
        assert result["body"]["exit_confirmed"]
        assert "session_id" not in result["body"]
        assert trials.execution.calls == 0
    finally:
        await trials.close()
        trials.store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("stage,aborts", [("configuration", 0), ("prepare", 1), ("recheck", 1), ("commit", 0)])
async def test_cancel_during_submission_never_starts_model(tmp_path, monkeypatch, stage, aborts):
    from unittest.mock import AsyncMock

    trials, exp = setup(tmp_path, monkeypatch)
    trials.execution.runtime = SimpleNamespace(abort_session_provision=AsyncMock())
    entered, released = asyncio.Event(), asyncio.Event()
    method = "configuration" if stage in {"configuration", "recheck"} else stage
    original = getattr(trials.execution, method)
    calls = 0

    async def blocked(*args, **kwargs):
        nonlocal calls
        calls += 1
        target = 2 if stage == "configuration" else 3 if stage == "recheck" else 1
        if calls == target:
            entered.set()
            await released.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(trials.execution, method, blocked)
    try:
        await trials.start(ACTOR, exp["id"])
        await asyncio.wait_for(entered.wait(), 5)
        await trials.cancel(ACTOR, exp["id"])
        released.set()
        result = await finish(trials, exp)
        assert result["phase"] == "settled"
        assert result["body"]["outcome"] == "cancelled"
        assert result["body"]["exit_confirmed"]
        assert result["body"]["submitted"] is False
        assert trials.execution.calls == 0
        assert trials.execution.runtime.abort_session_provision.await_count == aborts
        assert not trials.observations
    finally:
        released.set()
        await trials.close()
        trials.store.close()
