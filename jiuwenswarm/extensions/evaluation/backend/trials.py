"""Business Trial submission and reconciliation; the Runtime owns each execution."""

from __future__ import annotations

import asyncio
import importlib.metadata
import json
import time

from .adapters.runtime_execution import RuntimeExecution
from .adapters.store import CatalogError
from .adapters.verification import SharedHostVerifier, workspace_file
from .adapters.independent import IndependentVerifier, environment_snapshot, POLICY
from .models import ExperimentDraft, TaskDraft, decode
from .results import (
    delivery_evidence,
    statistics,
    safe_diagnostic,
    implementation_source,
)


class Trials:
    def __init__(self, store, root, runtime, *, send_push=None):
        self.store, self.root = store, root
        self.execution = RuntimeExecution(runtime, send_push=send_push)
        self.verifier = SharedHostVerifier()
        self.independent = IndependentVerifier(root / "verification-staging")
        self.workers = {}
        self.observations = {}
        self.stopping = set()
        self.closed = False

    @staticmethod
    def require_execution(identity):
        # shared-host-v1 is the existing trusted local personal installation.
        # Remote/organization actors need an independently admitted environment.
        if identity.authority != "local-single-user-installation":
            raise CatalogError("SHARED_HOST_LOCAL_ONLY")

    @staticmethod
    def _sources():
        distribution = importlib.metadata.distribution("openjiuwen")
        return {
            "implementation": implementation_source(),
            "core": distribution.version,
            "swarm": importlib.metadata.version("workswarm"),
            "core_source": json.loads(distribution.read_text("direct_url.json") or "null"),
        }

    def _require_sources(self, frozen):
        current = self._sources()
        # Paths, Git history and dirty flags describe provenance, not code identity.
        # A clean installation or commit reorganization can have identical bytes.
        if current["implementation"]["sha256"] != frozen.get("implementation", {}).get("sha256"):
            raise CatalogError("IMPLEMENTATION_CHANGED")
        if any(current[key] != frozen.get(key) for key in ("core", "swarm", "core_source")):
            raise CatalogError("DEPENDENCY_CHANGED")
        return current

    async def create(self, identity, value, key):
        self.require_execution(identity)
        definition = decode(ExperimentDraft, value)
        configuration = await self.execution.configuration(definition)
        versions = {
            "plugin": "1.0.0",
            **self._sources(),
            "configuration": configuration,
        }
        if definition.acceptance_policy == POLICY:
            versions["verification_environment"] = await environment_snapshot()
        return self.store.create_experiment(identity, value, key, versions=versions)

    def _attempt(self, identity, experiment_id, attempt_id):
        experiment = self.store.experiment(identity, experiment_id)
        for trial in experiment["trials"]:
            for attempt in trial["attempts"]:
                if attempt["id"] == attempt_id:
                    return attempt
        raise CatalogError("NOT_FOUND")

    def _patch(self, identity, experiment_id, attempt_id, phase=None, **changes):
        attempt = self._attempt(identity, experiment_id, attempt_id)
        self.store.update_attempt(
            identity,
            experiment_id,
            attempt_id,
            revision=attempt["revision"],
            phase=phase or attempt["phase"],
            body={**attempt["body"], **changes},
        )

    async def start(self, identity, experiment_id):
        self.require_execution(identity)
        experiment = self.store.experiment(identity, experiment_id)
        if self.closed:
            raise CatalogError("EVALUATION_CLOSED")
        if experiment_id in self.workers:
            return self.get(identity, experiment_id)
        if any(
            attempt["phase"] not in {"pending", "settled"}
            for trial in experiment["trials"]
            for attempt in trial["attempts"]
        ):
            raise CatalogError("UNRESOLVED_ATTEMPT")
        definition = decode(ExperimentDraft, experiment["definition"])
        if (
            await self.execution.configuration(definition)
            != experiment["versions"]["configuration"]
        ):
            raise CatalogError("CONFIGURATION_CHANGED")
        self._require_sources(experiment["versions"])
        # Configuration lookup yields; another click may already own the submission.
        if self.closed:
            raise CatalogError("EVALUATION_CLOSED")
        if experiment_id in self.workers:
            return self.get(identity, experiment_id)
        # The task merely owns pending business intents and observes Runtime work.
        # Database CAS below is the submission claim across service instances.
        self.stopping.discard(experiment_id)
        worker = asyncio.create_task(self._run(identity, experiment_id))
        self.workers[experiment_id] = worker

        def released(done):
            if self.workers.get(experiment_id) is done:
                self.workers.pop(experiment_id, None)

        worker.add_done_callback(released)
        return self.get(identity, experiment_id)

    def get(self, identity, experiment_id):
        experiment = self.store.experiment(identity, experiment_id)
        for trial in experiment["trials"]:
            for attempt in trial["attempts"]:
                body = attempt["body"]
                if attempt["phase"] not in {"pending", "settled"}:
                    if body.get("session_id"):
                        snapshot = self.execution.snapshot(
                            body["session_id"], attempt["id"]
                        )
                        if snapshot is not None:
                            body["runtime"] = {
                                "state": snapshot.state.value,
                                "generation": snapshot.generation,
                                "execution_id": snapshot.execution_id,
                                "waiting_control_ids": list(
                                    snapshot.waiting_control_ids
                                ),
                            }
                    if experiment_id not in self.workers:
                        body["status"] = "recovery_required"
        experiment["statistics"] = statistics(experiment)
        experiment["active"] = experiment_id in self.workers
        return experiment

    async def _run(self, identity, experiment_id):
        experiment = self.store.experiment(identity, experiment_id)
        definition = decode(ExperimentDraft, experiment["definition"])
        for trial in experiment["trials"]:
            attempt = trial["attempts"][-1]
            if attempt["phase"] != "pending":
                continue
            if experiment_id in self.stopping:
                self._patch(
                    identity,
                    experiment_id,
                    attempt["id"],
                    "settled",
                    outcome="cancelled",
                    status="settled",
                    exit_confirmed=True,
                    submitted=False,
                )
                continue
            try:
                # No await between read and CAS: the winner is durable before any side effect.
                self.store.update_attempt(
                    identity,
                    experiment_id,
                    attempt["id"],
                    revision=attempt["revision"],
                    phase="submitting",
                    body={"started_at": time.time(), "status": "submitting"},
                )
            except CatalogError:
                continue
            try:
                task = decode(
                    TaskDraft,
                    next(
                        item["value"]
                        for item in experiment["tasks"]
                        if item["id"] == trial["task_id"]
                        and item["revision"] == trial["task_revision"]
                    ),
                )
                await self._run_attempt(
                    identity, experiment_id, definition, task, attempt["id"]
                )
            except Exception as exc:
                current = self._attempt(identity, experiment_id, attempt["id"])
                # Retain unknown ownership and the original Session; never auto-resubmit.
                self._patch(
                    identity,
                    experiment_id,
                    attempt["id"],
                    "unknown"
                    if current["body"].get("session_id")
                    and (
                        not current["body"].get("execution_finished_at")
                        or (
                            isinstance(exc, CatalogError)
                            and exc.code == "EXIT_NOT_CONFIRMED"
                        )
                    )
                    else "settled",
                    outcome="environment_error",
                    status="settled"
                    if not current["body"].get("session_id")
                    or current["body"].get("execution_finished_at")
                    else "recovery_required",
                    error_code=exc.code
                    if isinstance(exc, CatalogError)
                    else type(exc).__name__,
                    exit_confirmed=(
                        bool(current["body"].get("execution_finished_at"))
                        or not bool(current["body"].get("session_id"))
                    )
                    and not (
                        isinstance(exc, CatalogError)
                        and exc.code == "EXIT_NOT_CONFIRMED"
                    ),
                )
                if (
                    self._attempt(identity, experiment_id, attempt["id"])["phase"]
                    == "unknown"
                ):
                    return

    async def _run_attempt(self, identity, experiment_id, definition, task, attempt_id):
        versions = self.store.experiment(identity, experiment_id)["versions"]
        expected = versions["configuration"]
        if await self.execution.configuration(definition) != expected:
            raise CatalogError("CONFIGURATION_CHANGED")
        sources = self._require_sources(versions)
        self._patch(identity, experiment_id, attempt_id, execution_sources=sources)
        prepared = await self.execution.prepare(
            definition, title=task.name, request_id=attempt_id
        )
        session_id = prepared.result.session_id
        self._patch(identity, experiment_id, attempt_id, session_id=session_id)
        try:
            workspace = self.execution.workspace(session_id)
            self._patch(identity, experiment_id, attempt_id, workspace=str(workspace))
            for item in task.files:
                path = workspace_file(workspace, item.path)
                path.parent.mkdir(parents=True, exist_ok=True)
                # Do not overwrite an existing Session's files after a lost receipt.
                with path.open("x", encoding="utf-8") as target:
                    target.write(item.content)
                path.chmod(0o755 if item.executable else 0o644)
            if await self.execution.configuration(definition) != expected:
                raise CatalogError("CONFIGURATION_CHANGED")
            self._require_sources(versions)
            await self.execution.commit(prepared)
        except BaseException:
            await self.execution.runtime.abort_session_provision(prepared)
            raise
        self._patch(
            identity, experiment_id, attempt_id, "observing", status="submitted"
        )
        errors = []

        async def observed(event):
            from jiuwenswarm.runtime.events import TERMINAL_ERROR_EVENT_TYPES

            if not event.ok or event.event_type in TERMINAL_ERROR_EVENT_TYPES:
                errors.append(event.event_type or "runtime.error")
                if isinstance(event.payload, dict) and event.payload.get("error"):
                    self._patch(
                        identity,
                        experiment_id,
                        attempt_id,
                        runtime_error=safe_diagnostic(event.payload["error"]),
                    )
            self._patch(
                identity, experiment_id, attempt_id, last_event_type=event.event_type
            )

        observation = asyncio.create_task(
            self.execution.observe(
                session_id=session_id,
                request_id=attempt_id,
                definition=definition,
                task=task,
                workspace=workspace,
                on_event=observed,
            )
        )
        self.observations[attempt_id] = (
            identity,
            experiment_id,
            session_id,
            observation,
        )
        deadline = time.monotonic() + definition.timeout_seconds
        stop_deadline = None
        requested_stop = False
        timed_out = False
        try:
            while True:
                snapshot = self.execution.snapshot(session_id, attempt_id)
                if (
                    snapshot is not None
                    and snapshot.state.terminal
                    and observation.done()
                ):
                    # Runtime cancellation commonly closes its sole consumer with
                    # CancelledError. Only its confirmed terminal receipt settles it.
                    if not observation.cancelled():
                        await observation
                    break
                if not requested_stop and time.monotonic() >= deadline:
                    timed_out = True
                if (experiment_id in self.stopping or timed_out) and not requested_stop:
                    self._patch(identity, experiment_id, attempt_id, status="stopping")
                    await self.execution.cancel(session_id, attempt_id)
                    requested_stop = True
                    stop_deadline = time.monotonic() + 15
                elif requested_stop and time.monotonic() >= stop_deadline:
                    raise CatalogError("EXIT_NOT_CONFIRMED")
                if observation.done():
                    if not observation.cancelled():
                        await observation
                    if snapshot is None:
                        raise CatalogError("EXECUTION_OUTCOME_UNKNOWN")
                await asyncio.sleep(0.1)
        finally:
            # Do not cancel the original Runtime producer merely because observation
            # ends. If cleanup failed retain its consumer and ownership for retry.
            if not observation.done():
                self._patch(
                    identity, experiment_id, attempt_id, status="exit_unconfirmed"
                )
            else:
                self.observations.pop(attempt_id, None)
        outcome = (
            "execution_timeout"
            if timed_out
            else "cancelled"
            if requested_stop or snapshot.state.value == "cancelled"
            else "execution_failed"
            if errors or snapshot.state.value != "succeeded"
            else None
        )
        execution_finished = time.time()
        self._patch(
            identity,
            experiment_id,
            attempt_id,
            execution_finished_at=execution_finished,
            runtime_terminal=snapshot.state.value,
            execution_seconds=execution_finished
            - self._attempt(identity, experiment_id, attempt_id)["body"]["started_at"],
        )
        if outcome:
            self._patch(
                identity,
                experiment_id,
                attempt_id,
                "settled",
                outcome=outcome,
                status="settled",
                exit_confirmed=True,
                finished_at=time.time(),
            )
            return
        self._patch(
            identity,
            experiment_id,
            attempt_id,
            "verifying",
            status="verifying",
            execution_outcome="succeeded",
        )
        if definition.acceptance_policy == POLICY:
            snapshot = self.store.experiment(identity, experiment_id)["versions"][
                "verification_environment"
            ]
            verification = await self.independent.verify(
                attempt_id,
                workspace,
                task,
                snapshot,
                remember=lambda ownership: self._patch(
                    identity, experiment_id, attempt_id, verifier_ownership=ownership
                ),
            )
            verification["files"] = verification.get("delivery_manifest", {}).get(
                "files", []
            )
        else:
            verification = await self.verifier.verify(attempt_id, workspace, task)
            verification["files"] = delivery_evidence(workspace, task)
        if experiment_id in self.stopping:
            verification["outcome"] = "cancelled"
        self._patch(
            identity,
            experiment_id,
            attempt_id,
            "settled",
            **verification,
            finished_at=time.time(),
            status="settled",
        )

    async def cancel(self, identity, experiment_id):
        self.require_execution(identity)
        experiment = self.store.experiment(identity, experiment_id)
        self.stopping.add(experiment_id)
        for trial in experiment["trials"]:
            attempt = trial["attempts"][-1]
            if attempt["phase"] == "pending":
                self._patch(
                    identity,
                    experiment_id,
                    attempt["id"],
                    "settled",
                    outcome="cancelled",
                    status="settled",
                    exit_confirmed=True,
                    submitted=False,
                )
            elif (
                experiment_id not in self.workers
                and attempt["phase"] != "settled"
                and attempt["body"].get("execution_finished_at")
                and attempt["body"].get("verifier_ownership")
            ):
                await self.independent.cancel(
                    attempt["id"], attempt["body"]["verifier_ownership"]
                )
                self._patch(
                    identity,
                    experiment_id,
                    attempt["id"],
                    "settled",
                    outcome="cancelled",
                    status="settled",
                    exit_confirmed=True,
                    verifier_removed=True,
                )
            elif attempt["phase"] == "verifying":
                await self.verifier.cancel(attempt["id"])
                await self.independent.cancel(
                    attempt["id"], attempt["body"].get("verifier_ownership")
                )
            elif (
                (attempt["phase"] == "unknown" or experiment_id not in self.workers)
                and attempt["body"].get("session_id")
                and attempt["phase"] != "settled"
            ):
                await self.verifier.cancel(attempt["id"])
                await self.independent.cancel(
                    attempt["id"], attempt["body"].get("verifier_ownership")
                )
                snapshot = self.execution.snapshot(
                    attempt["body"]["session_id"], attempt["id"]
                )
                if snapshot is None or not snapshot.state.terminal:
                    await self.execution.cancel(
                        attempt["body"]["session_id"], attempt["id"]
                    )
                    snapshot = self.execution.snapshot(
                        attempt["body"]["session_id"], attempt["id"]
                    )
                record = self.observations.get(attempt["id"])
                if record is not None:
                    await asyncio.wait({record[3]}, timeout=5)
                if (
                    snapshot is None
                    or not snapshot.state.terminal
                    or (record is not None and not record[3].done())
                ):
                    raise CatalogError("EXIT_NOT_CONFIRMED")
                self.observations.pop(attempt["id"], None)
                self._patch(
                    identity,
                    experiment_id,
                    attempt["id"],
                    "settled",
                    outcome="cancelled",
                    status="settled",
                    exit_confirmed=True,
                )
        return self.get(identity, experiment_id)

    async def close(self):
        self.closed = True
        self.stopping.update(self.workers)
        for attempt_id in tuple(self.independent.environments):
            await self.independent.cancel(attempt_id)
        for attempt_id in tuple(self.verifier.processes):
            await self.verifier.cancel(attempt_id)
        if self.workers:
            done, pending = await asyncio.wait(tuple(self.workers.values()), timeout=25)
            if pending:
                raise CatalogError("EXIT_NOT_CONFIRMED")
            for task in done:
                task.result()

        # Unknown exit retains the sole consumer and Runtime ownership for retry.
        for identity, experiment_id, _session_id, _observation in tuple(
            self.observations.values()
        ):
            await self.cancel(identity, experiment_id)
        if (
            self.observations
            or self.verifier.processes
            or self.independent.environments
        ):
            raise CatalogError("EXIT_NOT_CONFIRMED")
