# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Real Provider CLIs through the Team member factory; loopback models only."""

import asyncio
import json
import os
import socket
import threading
from contextlib import AsyncExitStack
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from openjiuwen.agent_teams import TeamAgentSpec
from openjiuwen.agent_teams.context import reset_session_id, set_session_id
from openjiuwen.agent_teams.schema.blueprint import DeepAgentSpec, StorageSpec
from openjiuwen.agent_teams.schema.task import TaskGraphSpec
from openjiuwen.agent_teams.schema.team import TeamMemberSpec
from openjiuwen.core.runner import Runner
from openjiuwen.core.session.checkpointer import CheckpointerFactory
from openjiuwen.core.session.checkpointer.checkpointer import InMemoryCheckpointer
from openjiuwen.harness.engine import ExecutionBinding
from openjiuwen.harness.goal import GoalStatus
from openjiuwen.harness_protocol import HarnessState

from jiuwenswarm.agents.harness.team.team_manager import TeamManager
from jiuwenswarm.common.runtime_workspace import RuntimeWorkspacePaths
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.runtime.context import reset_runtime_context, set_runtime_context
from jiuwenswarm.runtime.harness import team_execution as module
from jiuwenswarm.runtime.harness.binding_store import (
    BoundExecution,
    ExecutionBindingStore,
)
from jiuwenswarm.runtime.harness.config_source import load_execution_catalog
from jiuwenswarm.runtime.harness.recovery_store import SessionExecutionRecovery
from jiuwenswarm.runtime.harness.request_binding import AdmittedExecutionRoute
from jiuwenswarm.runtime.harness.surface import (
    EffectiveSurfaceSnapshot,
    build_surface_identity,
)
from jiuwenswarm.runtime.session import RuntimeSessionCoordinator, SessionWorkKind
from jiuwenswarm.server.runtime.agent_adapter import team_helpers
from jiuwenswarm.server.runtime.agent_adapter.team_engine_adapter import (
    ExternalTeamAgentAdapter,
)
from jiuwenswarm.server.runtime.session.session_history import load_history_records
from tests.system_tests.test_external_codex_product_route_local import _ResponsesFixture
from tests.system_tests.test_external_opencode_product_route_local import _ModelFixture
from tests.unit_tests.runtime.harness.test_execution_recovery import (
    recovery_env as recovery_env,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.system,
    pytest.mark.skipif(
        os.environ.get("RUN_TEAM_PROVIDER_LOCAL") != "1",
        reason="real Team Provider CLI root Goal validation is opt-in",
    ),
]


class _ReviewScenario:
    """Deterministic local model decisions; real CLIs execute every tool call."""

    def __init__(self, *, messages=False, scheduled=False):
        self.seeded = threading.Event()
        self.submitted = threading.Event()
        self.reviewed = threading.Event()
        self.lock = threading.Lock()
        self.actions = {
            "team_leader": [
                (
                    "build_team",
                    {
                        "display_name": "Test",
                        "team_desc": "Review",
                        "leader_display_name": "Lead",
                        "leader_desc": "Coordinate",
                    },
                ),
                ("view_task", {"action": "list"}),
            ],
            "worker": [
                ("claim_task", {"task_id": "reviewed", "status": "claimed"}),
                ("claim_task", {"task_id": "reviewed", "status": "completed"}),
            ],
            "reviewer": [("verify_task", {"task_id": "reviewed", "decision": "pass"})],
        }
        if scheduled:
            self.actions["worker"] = [("member_complete_task", {"task_id": "reviewed", "note": "Done"})]
        if messages:
            self.actions["team_leader"][1:1] = [
                ("send_message", {"to": "worker", "content": "Process reviewed task"}),
                ("send_message", {"to": "reviewer", "content": "Review reviewed task"}),
            ]
        self.roles = set()

    def next(self, body):
        rendered = json.dumps(body)
        role = next(
            (
                name
                for name in ("worker", "reviewer")
                if f"R1F_PRIVATE_{name}" in rendered
            ),
            "team_leader",
        )
        if role == "worker":
            assert self.seeded.wait(30), "Task was not seeded"
        elif role == "reviewer":
            assert self.submitted.wait(30), "Task was not submitted"
        with self.lock:
            self.roles.add(role)
            action = self.actions[role].pop(0) if self.actions[role] else None
        if role == "team_leader" and action and action[0] == "view_task":
            assert self.reviewed.wait(30), "Review did not finish"
        return action

    def codex(self, body, index):
        action = self.next(body)
        if action:
            name, args = action
            return {
                "type": "function_call",
                "namespace": "mcp__jiuwenswarm_product_tools",
                "name": name,
                "id": f"fc_{index}",
                "call_id": f"call_{index}",
                "arguments": json.dumps(args),
            }
        return {
            "type": "message",
            "role": "assistant",
            "id": f"msg_{index}",
            "status": "completed",
            "content": [
                {"type": "output_text", "text": "TEAM-REVIEW-OK", "annotations": []}
            ],
        }

    def next_attempt(self):
        with self.lock:
            self.actions["team_leader"].append(("view_task", {"action": "list"}))


@pytest.mark.asyncio
@pytest.mark.parametrize("review_team", [False, True, "messages", "scheduled"], ids=["leader", "review-team", "review-messages", "scheduled-review"])
@pytest.mark.parametrize("provider", ["codex", "opencode"])
async def test_real_team_goal_two_attempts_and_durable_history(
    tmp_path, monkeypatch, provider, recovery_env, review_team, probe=None
):
    channel_id = getattr(probe, "channel_id", "local")
    scheduled = review_team == "scheduled"
    root = tmp_path / "project"
    home = tmp_path / "home"
    runtime_root = tmp_path / "provider-runtime"
    for path in (root, home, runtime_root):
        path.mkdir(mode=0o700)
    monkeypatch.setenv("OPENJIUWEN_HOME", str(home / "openjiuwen"))
    from jiuwenswarm.server.runtime.session import session_history

    monkeypatch.setattr(session_history, "get_agent_sessions_dir", lambda: recovery_env)
    previous = CheckpointerFactory.get_checkpointer()
    checkpoint_engine = None
    if scheduled:
        from sqlalchemy.ext.asyncio import create_async_engine
        from openjiuwen.core.session.checkpointer.persistence import PersistenceCheckpointerProvider
        checkpoint_engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'checkpoint.db'}")
        checkpoint = await PersistenceCheckpointerProvider().create({'db_client': checkpoint_engine})
        CheckpointerFactory.set_default_checkpointer(checkpoint)
    else:
        CheckpointerFactory.set_default_checkpointer(InMemoryCheckpointer())
    from openjiuwen.agent_teams.spawn.shared_resources import cleanup_shared_resources

    cleanup_shared_resources()
    token = set_session_id("team-local")
    scenario = _ReviewScenario(messages=review_team == "messages", scheduled=scheduled) if review_team else None
    backends = {}
    contexts = []
    databases = []
    engines = []
    coordinator = RuntimeSessionCoordinator()
    manager = TeamManager()
    adapter = None
    await coordinator.register_session("team-local", channel_id)
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.get_team_manager", lambda _: manager
    )
    monkeypatch.setattr(team_helpers, "get_team_manager", lambda _: manager)
    monkeypatch.setattr(
        team_helpers, "_persist_team_file_monitor_roots", lambda *args: None
    )
    original_member = module.ExternalTeamMemberFactory.build_member_runtime

    def member(factory, request):
        backends[request.context.member_name] = request.team_backend
        databases.append(request.team_backend.db)
        return original_member(factory, request)

    monkeypatch.setattr(
        module.ExternalTeamMemberFactory, "build_member_runtime", member
    )
    review_requests = []
    if scheduled:
        # Candidate-only integration gate: product scheduled admission stays closed.
        monkeypatch.setattr(module.ExternalTeamMemberFactory, "validate_team_spec",
                            lambda factory, spec: factory._validate_team_spec(spec, scheduled=True))
        original_review = module.ExternalTeamMemberFactory.build_review_runtime
        def review_runtime(factory, request):
            request = replace(request, system_prompt=request.system_prompt + "\nR1F_PRIVATE_reviewer")
            review_requests.append(request)
            return original_review(factory, request)
        monkeypatch.setattr(module.ExternalTeamMemberFactory, "build_review_runtime", review_runtime)
        from jiuwenswarm.runtime.harness import team_review
        original_gateway = team_review.ProductToolGateway
        def review_gateway(*args, **kwargs):
            result = original_gateway(*args, **kwargs)
            invoke = result.invoke
            async def observed(invocation):
                output = await invoke(invocation)
                tool_results.append(("reviewer", invocation.name, output))
                if invocation.name == "verify_task" and not output.is_error:
                    scenario.reviewed.set()
                return output
            result.invoke = observed
            return result
        monkeypatch.setattr(team_review, "ProductToolGateway", review_gateway)
    original_engine = module.create_harness_engine

    def engine(*args, **kwargs):
        result = original_engine(*args, **kwargs)
        engines.append(result)
        start = result.harness.start

        async def observed_start(context):
            await start(context)
            contexts.append(context)

        result.harness.start = observed_start
        return result

    monkeypatch.setattr(module, "create_harness_engine", engine)
    transports = []
    tool_results = []
    real_products = module.ExternalSubagentRuntime

    def products(*args, **kwargs):
        result = real_products(*args, **kwargs)
        invoke = result.gateway.invoke

        async def observed(invocation):
            output = await invoke(invocation)
            tool_results.append(
                (kwargs["invoke_kwargs"]["member_name"], invocation.name, output)
            )
            if scenario is not None and not output.is_error:
                if invocation.name == "build_team":
                    seeded = await backends["team_leader"].task_manager.add_graph(
                        [
                            TaskGraphSpec(
                                task_id="reviewed",
                                title="Work",
                                content="Verify member ownership",
                                assignee="worker",
                                reviewer=("reviewer",),
                            )
                        ]
                    )
                    assert seeded.ok
                    scenario.seeded.set()
                    # Exercise original startup without injecting an additional
                    # active-turn steer. Message interruption has a separate gate.
                    if review_team not in {"messages", "scheduled"}:
                        await backends["team_leader"].autostart_unstarted()
                elif invocation.name in {"claim_task", "member_complete_task"}:
                    task = await backends["worker"].task_manager.get("reviewed")
                    if task.status == "in_review":
                        scenario.submitted.set()
                elif invocation.name == "verify_task":
                    task = await backends["reviewer"].task_manager.get("reviewed")
                    assert task.status == "completed"
                    scenario.reviewed.set()
            return output

        result.gateway.invoke = observed
        return result

    monkeypatch.setattr(module, "ExternalSubagentRuntime", products)
    real_transport = module.ManagedProductToolTransport

    def transport(*args, **kwargs):
        result = real_transport(*args, **kwargs)
        transports.append(result)
        return result

    monkeypatch.setattr(module, "ManagedProductToolTransport", transport)
    async with AsyncExitStack() as stack:
        try:
            if provider == "codex":
                pytest.importorskip("openai_codex")
                model = stack.enter_context(_ResponsesFixture())
                if scenario is not None:
                    model.responder = scenario.codex
                codex_home = tmp_path / "codex-home"
                (codex_home / "skills").mkdir(parents=True)
                settings = {
                    "inherit_process_env": False,
                    "env": {
                        "HOME": str(home),
                        "CODEX_HOME": str(codex_home),
                        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                    },
                    "startup_source_roots": [str(root), str(codex_home / "skills")],
                    "mcp_required": True,
                    "model": {
                        "model": "gpt-5.6-sol",
                        "provider": "team_fixture",
                        "api_base": model.base_url,
                        "api_key": "local-only",
                    },
                }
            else:
                cli = Path(
                    os.environ.get(
                        "OPENCODE_OC1_CLI", str(Path.home() / ".opencode/bin/opencode")
                    )
                )
                if not cli.is_file():
                    pytest.skip("OpenCode CLI is not installed")
                model = _ModelFixture()
                app = web.Application()
                if scenario is None:
                    app.router.add_post("/v1/chat/completions", model.respond)
                else:
                    response_lock = asyncio.Lock()

                    async def respond(request):
                        action = await asyncio.to_thread(
                            scenario.next, await request.json()
                        )
                        async with response_lock:
                            model.actions[:] = (
                                [
                                    {
                                        "tool": "jiuwenswarm_product_tools_"
                                        + action[0],
                                        "args": action[1],
                                    }
                                ]
                                if action
                                else []
                            )
                            return await model.respond(request)

                    app.router.add_post("/v1/chat/completions", respond)
                runner = web.AppRunner(app)
                await runner.setup()
                stack.push_async_callback(runner.cleanup)
                sock = socket.socket()
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
                await web.SockSite(runner, sock).start()
                settings = {
                    "cli_path": str(cli),
                    "runtime_root": str(runtime_root),
                    "turn_timeout_s": 25,
                    "model": {
                        "model": "fixture",
                        "api_base": f"http://127.0.0.1:{port}/v1",
                        "api_key": "local-only",
                    },
                }
            if probe is not None:
                probe.configure(provider, settings, root, tmp_path / 'codex-home')
                if hasattr(probe, "prepare_model"):
                    await probe.prepare_model(stack, settings, scenario)
            config = {
                "execution": {
                    "default_profile_id": "team-local",
                    "profiles": {
                        "team-local": {
                            "provider_id": provider,
                            "config_revision": "team-local-1",
                            "provider_config": settings,
                            "authorization": {"full_access": probe is None},
                        }
                    },
                }
            }
            source = load_execution_catalog(config).source(
                explicit_profile_id="team-local"
            )
            binding = ExecutionBinding.create(
                source.resolve(),
                subject_id="fixture-owner",
                host_session_id="team-local",
                workspace=str(root),
            )
            paths = RuntimeWorkspacePaths(home, root, root, root)
            metadata = {
                "session_id": "team-local",
                "user_id": "fixture-owner",
                "channel_id": channel_id,
                "team_name": "team",
                "mode": "team.code.normal",
                "work_mode": "code",
                "project_dir": str(root),
                "execution_profile_id": "team-local",
                "execution_config_revision": binding.config_revision,
                "execution_config_fingerprint": binding.fingerprint,
            }
            monkeypatch.setattr(module, "_host_config", lambda: config)
            monkeypatch.setattr(module, "_session_metadata", lambda _: metadata)
            identity = build_surface_identity(
                metadata=metadata, binding=binding, paths=paths, channel_id=channel_id
            )
            route = AdmittedExecutionRoute(
                channel_id,
                source,
                ExecutionBindingStore(),
                BoundExecution(binding, source.resolve()),
                paths,
                surface=EffectiveSurfaceSnapshot(identity, metadata["mode"]),
            )
            recovery = SessionExecutionRecovery(
                session_id="team-local",
                execution_profile_id="team-local",
                binding=binding,
                runtime_paths=paths,
            )
            route = replace(route, recovery=recovery)

            def team_spec(**kwargs):
                spec = TeamAgentSpec(
                    agents={"leader": DeepAgentSpec(), "teammate": DeepAgentSpec()},
                    team_name="team",
                    evolution_enabled=False,
                    spawn_mode="inprocess",
                    storage=StorageSpec(type="memory"),
                )
                if review_team:
                    spec.storage = StorageSpec(
                        type="sqlite",
                        params={"connection_string": str(tmp_path / "team.db")},
                    )
                    spec.predefined_members = [
                        TeamMemberSpec(
                            member_name=name,
                            display_name=name,
                            prompt=f"R1F_PRIVATE_{name}",
                        )
                        for name in (("worker",) if scheduled else ("worker", "reviewer"))
                    ]
                if scheduled:
                    spec.dispatch_mode = "scheduled"
                    spec.enable_task_verification = True
                return module.attach_external_team_execution(spec, route)

            monkeypatch.setattr(
                manager,
                "get_swarm_enriched_team_spec",
                AsyncMock(side_effect=team_spec),
            )
            adapter = ExternalTeamAgentAdapter(route)
            await adapter.create_instance()
            attempts = []

            class Assessor:
                async def invoke(self, messages, **kwargs):
                    assert kwargs["tools"] == []
                    assert engines and all(
                        e.harness.state is HarnessState.TERMINATED for e in engines
                    )
                    assert all(t.exit_confirmed for t in transports)
                    evidence = adapter._goal_runtime.attempt
                    assert evidence.ready_for_assessment
                    if review_team:
                        task = await backends["team_leader"].task_manager.get(
                            "reviewed"
                        )
                        assert task.status == "completed" and task.assignee == "worker"
                        reviewers = json.loads(task.reviewer)
                        assert [
                            r["reviewer_id"] if isinstance(r, dict) else r
                            for r in reviewers
                        ] == ["reviewer"]
                        assert task.review_round == 1
                        members = await backends[
                            "team_leader"
                        ].db.member.get_team_members("team")
                        expected = {"team_leader", "worker"} if scheduled else {"team_leader", "worker", "reviewer"}
                        assert {m.member_name for m in members} == expected
                        if scheduled:
                            assert len(review_requests) == 1
                            from jiuwenswarm.runtime.harness.team_review import validate_review_recovery
                            owner_session = review_requests[0].team_session
                            validate_review_recovery(owner_session)
                            from openjiuwen.core.session.agent_team import create_agent_team_session
                            cold_session = create_agent_team_session(
                                session_id="team-local", team_id=owner_session.get_team_id())
                            await cold_session.pre_run()
                            validate_review_recovery(cold_session)
                            records = cold_session.get_state('external_team_reviews')
                            assert records and len(records) == 1
                            assert next(iter(records.values()))['invocation_id'] == review_requests[0].invocation_id
                            assert "/reviewer/" in attempts[0].transcript if attempts else "/reviewer/" in evidence.transcript
                    else:
                        assert evidence.turn_count == 1
                    attempts.append(evidence)
                    status = "continue" if len(attempts) == 1 else "complete"
                    return SimpleNamespace(
                        content=json.dumps(
                            {
                                "status": status,
                                "evidence": "verified local Team Goal",
                                "next_instruction": "Verify the task board again",
                            }
                        ),
                        usage_metadata={"input_tokens": 3, "output_tokens": 1},
                    )

            adapter.set_goal_assessor_factory(lambda: Assessor())
            runtime = probe.bind(coordinator, adapter) if probe is not None else coordinator
            original_round = adapter.process_goal_round

            async def goal_round(request, inputs, goal):
                if scenario is not None:
                    if attempts:
                        scenario.next_attempt()
                elif provider == "codex":
                    model.items.append(
                        {
                            "type": "function_call",
                            "namespace": "mcp__jiuwenswarm_product_tools",
                            "name": "view_task",
                            "id": f"fc_{len(model.requests)}",
                            "call_id": f"call_{len(model.requests)}",
                            "arguments": json.dumps({"action": "list"}),
                        }
                    )
                else:
                    model.actions.append(
                        {
                            "tool": "jiuwenswarm_product_tools_view_task",
                            "args": {"action": "list"},
                        }
                    )
                async for chunk in original_round(request, inputs, goal):
                    yield chunk

            monkeypatch.setattr(adapter, "process_goal_round", goal_round)
            request = AgentRequest(
                request_id="root-goal",
                channel_id=channel_id,
                session_id="team-local",
                user_id="fixture-owner",
                req_method=ReqMethod.COMMAND_GOAL,
                is_stream=True,
                params={
                    "mode": "team.code.normal",
                    "action": "set",
                    "objective": "Verify the original Team task board twice",
                    "max_attempts": 3,
                },
            )
            request._execution_route = route
            request._bound_execution = route.bound
            request._defer_execution_until_history = True
            chunks = []

            async def produce():
                async for chunk in adapter.process_message_stream_impl(
                    request, {"query": "goal"}
                ):
                    chunks.append(chunk)
                    if probe is not None:
                        probe.observe(chunk)

            await Runner.start()
            context_token = set_runtime_context(runtime, None)
            try:
                async with asyncio.timeout(110):
                    if probe is None:
                        await coordinator.run_unary(
                            "team-local", "root-goal", SessionWorkKind.GOAL_STREAM, produce
                        )
                    elif await probe.run(coordinator, produce, adapter, request, scenario):
                        assert not attempts
                        assert all(e.harness.state is HarnessState.TERMINATED for e in engines)
                        assert all(t.exit_confirmed and not t.started for t in transports)
                        assert not any(c.runtime_completion == 'completed' for c in chunks)
                        return
                    goal = adapter._goal_runtime.manager.peek()
                    assert goal.status is GoalStatus.COMPLETED, (
                        goal.to_dict(),
                        [
                            c.payload
                            for c in chunks
                            if isinstance(c.payload, dict)
                            and c.payload.get("event_type")
                            in {"chat.error", "team.error"}
                        ],
                    )
                    assert goal.attempt_count == 2 and len(attempts) == 2
                    assert (
                        goal.token_usage.total_tokens
                        == len(model.requests) * (2 if provider == "codex" else 22) + 8
                    )
                    assert all(not output.is_error for _, _, output in tool_results), (
                        tool_results
                    )
                    if review_team:
                        assert scenario.roles == {"team_leader", "worker", "reviewer"}
                        assert sum(tool == "send_message" for _, tool, _ in tool_results) == (
                            2 if review_team == "messages" else 0
                        )
                        assert (
                            sum(tool == "build_team" for _, tool, _ in tool_results)
                            == 1
                        )
                        assert (
                            sum(tool in {"claim_task", "member_complete_task"} for _, tool, _ in tool_results)
                            == (1 if scheduled else 2)
                        )
                        assert [
                            (name, tool)
                            for name, tool, _ in tool_results
                            if tool == "verify_task"
                        ] == [("reviewer", "verify_task")]
                        assert all(
                            name == "worker"
                            for name, tool, _ in tool_results
                            if tool in {"claim_task", "member_complete_task"}
                        )
                        assert (
                            sum(tool == "view_task" for _, tool, _ in tool_results) == 2
                        )
                        from openjiuwen.harness_protocol import ResumePolicy

                        resumed = [
                            c
                            for c in contexts
                            if c.resume_policy is ResumePolicy.REQUIRE_RESUME
                        ]
                        assert {c.agent_name for c in resumed} == ({"team_leader", "worker"} if scheduled else {"team_leader", "worker", "reviewer"})
                        assert all(c.checkpoint is not None for c in resumed)
                    else:
                        assert len(tool_results) == 2
                    assert adapter._goal_runtime.owner is not None
                    await adapter.complete_request_history(request)
                    assert adapter._goal_runtime.owner is None
                    restored = ExternalTeamAgentAdapter(route)
                    await restored.create_instance()
                    assert (
                        restored._goal_runtime.manager.peek().to_dict()
                        == goal.to_dict()
                    )
                    assert not restored._goal_runtime.cold_unconfirmed
                    rows = load_history_records("team-local")
                    assert (
                        sum(bool(row.get("is_goal_objective_message")) for row in rows)
                        == 1
                    )
                    assert (
                        sum(bool(row.get("is_goal_completed_message")) for row in rows)
                        == 1
                    )
                    assert sum(c.runtime_completion == "completed" for c in chunks) == 1
                    assert len(engines) >= 4 if review_team else len(engines) == 2
            finally:
                reset_runtime_context(context_token)
        finally:
            if adapter is not None:
                await adapter.cleanup()
            await coordinator.close()
            await Runner.stop()
            for db in set(databases):
                await db.close()
            if checkpoint_engine is not None:
                await checkpoint_engine.dispose()
            CheckpointerFactory.set_default_checkpointer(previous)
            cleanup_shared_resources()
            reset_session_id(token)
    assert transports and all(t.exit_confirmed and not t.started for t in transports)
