"""One observer over the existing Runtime; all execution/control authority stays there."""

from __future__ import annotations

from contextlib import aclosing
from dataclasses import replace
from pathlib import Path

from jiuwenswarm.common.config import get_config
from jiuwenswarm.common.runtime_workspace import resolve_runtime_workspace_paths
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.common.utils import get_agent_workspace_dir
from jiuwenswarm.runtime.harness.config_source import load_execution_catalog
from jiuwenswarm.runtime.session_catalog import SessionGetInput
from jiuwenswarm.runtime.session_provisioner import SessionCreateInput

from .store import CatalogError


class RuntimeExecution:
    def __init__(self, runtime, *, send_push=None):
        self.runtime = runtime
        self.send_push = send_push

    async def options(self):
        await self.runtime.start()
        models = self.runtime.list_model_capabilities().to_dict()["models"]
        catalog = load_execution_catalog(get_config())
        profiles = []
        if catalog is None:
            profiles.append({"id": "legacy-native", "revision": "legacy"})
        else:
            for name in catalog.profile_ids:
                spec = catalog.source(explicit_profile_id=name).resolve()
                if spec.provider_id == "native":
                    profiles.append({"id": name, "revision": spec.config_revision})
        return {"models": models, "profiles": profiles}

    async def configuration(self, definition):
        options = await self.options()
        model = next(
            (
                item
                for item in options["models"]
                if item["selection_key"] == definition.model
            ),
            None,
        )
        profile = next(
            (
                item
                for item in options["profiles"]
                if item["id"] == definition.execution_profile_id
            ),
            None,
        )
        if model is None or profile is None:
            raise CatalogError("CONFIGURATION_UNAVAILABLE")
        # Snapshot public identity facts and a digest of resolved config, never credentials.
        from openjiuwen.harness.engine.config import config_fingerprint
        from ..models import digest
        from jiuwenswarm.common.config import get_default_models

        catalog = load_execution_catalog(get_config())
        execution_digest = (
            config_fingerprint(
                catalog.source(explicit_profile_id=profile["id"]).resolve()
            )
            if catalog
            else "legacy-native"
        )
        return {
            "model": model,
            "profile": profile,
            "execution_fingerprint": execution_digest,
            "model_configuration_digest": digest(get_default_models()),
        }

    async def prepare(self, definition, *, title: str, request_id: str):
        await self.runtime.start()
        return await self.runtime.prepare_session_create(
            SessionCreateInput(
                channel_id="web",
                create_token="evaluation-" + request_id,
                mode="agent.code.normal",
                work_mode="code",
                work_mode_explicit=True,
                persist_session=True,
                persist_session_supplied=True,
                title=title,
                model_name=definition.model,
                execution_profile_id=None
                if definition.execution_profile_id == "legacy-native"
                else definition.execution_profile_id,
            )
        )

    def workspace(self, session_id: str) -> Path:
        # Reuse the original projectless Session binding, never invent a Project.
        return resolve_runtime_workspace_paths(
            internal_workspace_dir=get_agent_workspace_dir(),
            project_dir=None,
            workspace_dir=None,
            cwd=None,
            session_id=session_id,
            task_name=None,
            bind_request=True,
        ).cwd

    async def commit(self, prepared):
        return await self.runtime.commit_session_provision(
            prepared, timing=prepared.commit_timing
        )

    async def observe(
        self, *, session_id, request_id, definition, task, workspace, on_event
    ):
        request = AgentRequest(
            request_id=request_id,
            channel_id="web",
            session_id=session_id,
            req_method=ReqMethod.CHAT_SEND,
            is_stream=True,
            params={
                "query": task.instruction,
                "mode": "agent.code.normal",
                "work_mode": "code",
                "model_name": definition.model,
                "cwd": str(workspace),
                "trusted_dirs": [str(workspace)],
                "supports_user_interaction": True,
                **(
                    {"execution_profile_id": definition.execution_profile_id}
                    if definition.execution_profile_id != "legacy-native"
                    else {}
                ),
            },
        )
        async def project(event):
            await on_event(event)
            if self.send_push is not None:
                await self.send_push({
                    "request_id": event.request_id,
                    "channel_id": event.channel_id or "web",
                    "session_id": event.session_id or session_id,
                    "payload": event.payload,
                    "agent_ref": event.agent_ref,
                    "is_complete": event.is_complete,
                    "metadata": event.metadata,
                })

        # The single observer projects onto the existing Web channel, which owns
        # live tools, questions, approvals and reconnect. It never answers controls.
        async with aclosing(self.runtime.stream(request, on_control_event=project)) as events:
            async for event in events:
                await project(event)

    def _executions(self, session_id, request_id):
        return self.runtime.get_session_request_executions(
            SessionGetInput(channel_id="web", session_id=session_id),
            request_id=request_id,
        )

    def snapshot(self, session_id, request_id):
        from jiuwenswarm.runtime.session.model import SessionExecutionState as State

        executions = self._executions(session_id, request_id)
        if not executions:
            return None
        root = executions[0]
        live = [item for item in executions if not item.state.terminal]
        waiting = tuple(sorted({control for item in live
                                for control in (*item.waiting_control_ids, item.waiting_control_id)
                                if control}))
        if live:
            state = (State.WAITING_FOR_CONTROL
                     if any(item.state is State.WAITING_FOR_CONTROL for item in live)
                     else State.RUNNING)
        elif any(item.state is State.FAILED or item.error for item in executions):
            state = State.FAILED
        elif any(item.state is State.CANCELLED for item in executions):
            state = State.CANCELLED
        else:
            state = State.SUCCEEDED
        # A read projection over existing receipts, never a second execution owner.
        return replace(root, state=state, waiting_control_ids=waiting,
                       waiting_control_id=waiting[0] if waiting else None,
                       finished_at=None if live else max(item.finished_at or 0 for item in executions))

    async def cancel(self, session_id, request_id):
        executions = self._executions(session_id, request_id)
        # A terminal root no longer matches Runtime's active cancellation selector.
        # Target each live descendant through the original cancellation channel.
        targets = [item.request_id for item in executions if not item.state.terminal]
        for target in dict.fromkeys(targets or [request_id]):
            if executions and not any(
                item.request_id == target and not item.state.terminal
                for item in self._executions(session_id, request_id)
            ):
                continue
            response = await self.runtime.cancel_request(
                AgentRequest(
                    request_id="cancel-" + request_id,
                    channel_id="web",
                    session_id=session_id,
                    req_method=ReqMethod.CHAT_CANCEL,
                    params={
                        "target_request_id": target,
                        "intent": "cancel",
                        "mode": "agent.code.normal",
                        "work_mode": "code",
                    },
                )
            )
            if not response.ok or (
                isinstance(response.payload, dict)
                and response.payload.get("success") is False
            ):
                raise CatalogError("EXIT_NOT_CONFIRMED")

        if self.send_push is not None:
            receipt = self.snapshot(session_id, request_id)
            if receipt is not None and receipt.state.terminal:
                # Notify the original Web channel only after Runtime confirms exit.
                # Its existing metadata reconciliation clears resolved controls.
                await self.send_push({
                    "request_id": request_id,
                    "channel_id": "web",
                    "session_id": session_id,
                    "payload": {
                        "event_type": "chat.processing_status",
                        "session_id": session_id,
                        "is_processing": False,
                    },
                    "is_complete": True,
                })
