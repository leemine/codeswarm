"""Real reviewer model failure settles the original root Goal, without replay."""

import asyncio
import json
import socket

import pytest
from aiohttp import ClientSession, web
from openjiuwen.harness.goal import GoalStatus
from openjiuwen.core.session.agent_team import create_agent_team_session
from jiuwenswarm.runtime.harness.team_review import validate_review_recovery
from jiuwenswarm.server.runtime.agent_adapter.team_engine_adapter import (
    ExternalTeamAgentAdapter,
)
from tests.system_tests.test_external_team_runtime_interactions_local import (
    RuntimeProbe,
    _run_goal,
    recovery_env,
    pytestmark,
)


class FailedReviewerProbe(RuntimeProbe):
    def __init__(self):
        super().__init__("allow")
        self.rejected = []
        self.chunks = []

    async def prepare_model(self, stack, settings, scenario):
        target = settings["model"]["api_base"]
        client = await stack.enter_async_context(ClientSession())

        async def proxy(request):
            body = await request.read()
            if "R1F_PRIVATE_reviewer" in body.decode():
                self.rejected.append(json.loads(body))
                scenario.reviewed.set()  # only releases the fixture leader model wait
                return web.json_response(
                    {
                        "error": {
                            "message": "reviewer fixture failure",
                            "type": "invalid_request_error",
                            "code": "invalid_api_key",
                        }
                    },
                    status=401,
                )
            async with client.post(
                target + request.path.removeprefix("/v1"),
                data=body,
                headers={"Content-Type": "application/json"},
            ) as response:
                return web.Response(
                    body=await response.read(),
                    status=response.status,
                    headers={"Content-Type": response.headers["Content-Type"]},
                )

        app = web.Application()
        app.router.add_post("/v1/{path:.*}", proxy)
        server = web.AppRunner(app)
        await server.setup()
        stack.push_async_callback(server.cleanup)
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        await web.SockSite(server, sock).start()
        settings["model"]["api_base"] = f"http://127.0.0.1:{sock.getsockname()[1]}/v1"

    def observe(self, chunk):
        self.chunks.append(chunk)
        super().observe(chunk)

    async def settle_failure(self, adapter, request):
        assert self.rejected and not self.review_questions
        goal = adapter._goal_runtime.manager.peek()
        assert goal.status is GoalStatus.BLOCKED and goal.attempt_count == 1, (
            goal.to_dict()
        )
        assert any(c.runtime_completion == "failed" for c in self.chunks)
        assert not any(c.runtime_completion == "completed" for c in self.chunks)
        assert adapter._goal_runtime.owner is not None
        await adapter.complete_request_history(request)
        assert adapter._goal_runtime.owner is None
        restored = ExternalTeamAgentAdapter(adapter.route)
        await restored.create_instance()
        assert restored._goal_runtime.manager.peek().to_dict() == goal.to_dict()
        assert restored._goal_runtime.accounting_unknown
        result = await restored._goal_runtime.control({"action": "resume"})
        assert result["result_type"] == "goal_error", result
        session = create_agent_team_session(session_id="team-local")
        await session.pre_run()
        assert session.get_state("external_team_reviews")
        with pytest.raises(ValueError, match="unconfirmed"):
            validate_review_recovery(session)


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["codex", "opencode"])
async def test_real_reviewer_failure_blocks_root_goal(
    tmp_path, monkeypatch, recovery_env, provider
):
    probe = FailedReviewerProbe()
    try:
        async with asyncio.timeout(100):
            await _run_goal(
                tmp_path, monkeypatch, provider, recovery_env, "scheduled", probe=probe
            )
    finally:
        if probe.runtime is not None:
            await probe.runtime.close()
