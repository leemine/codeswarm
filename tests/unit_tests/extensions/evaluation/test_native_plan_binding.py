"""Regression: first Code plan sync must not allocate a legacy Native child."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.runtime.plan import PlanModeController
from jiuwenswarm.server.runtime.agent_adapter.interface import JiuWenSwarm


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["agent.code.normal", "agent.code.plan"])
async def test_plan_state_binds_admitted_route_before_creating_live_child(
    mode, monkeypatch
):
    selected = []
    request = AgentRequest(
        request_id="request",
        channel_id="web",
        session_id="new-session",
        req_method=ReqMethod.CHAT_SEND,
        params={"mode": mode, "work_mode": "code"},
    )
    request._bound_execution = object()
    adapter = SimpleNamespace(
        select_execution_for_request=lambda value: selected.append(value)
    )
    agent = JiuWenSwarm.__new__(JiuWenSwarm)
    agent._runtime_execution_route = SimpleNamespace(provider_id="native")
    monkeypatch.setattr(agent, "_ensure_adapter", Mock(return_value=adapter))
    state = SimpleNamespace(plan_mode=SimpleNamespace(mode="normal", plan_slug=None))
    deep = SimpleNamespace(load_state=lambda session: state)
    controller = PlanModeController()

    async def opened(owner, session_id):
        assert owner is agent and session_id == "new-session"
        assert selected == [request], "would otherwise allocate the legacy route"
        return deep, object(), True

    monkeypatch.setattr(controller, "open_state_session", opened)

    # Stop after observing preparation order, before unrelated state mutation.
    class ReachedLiveState(Exception):
        pass

    def read_state(session):
        raise ReachedLiveState

    deep.load_state = read_state
    with pytest.raises(ReachedLiveState):
        await controller.ensure_state(
            request, "code", "plan" if mode.endswith(".plan") else "normal", agent
        )


def test_unbound_legacy_preparation_preserves_existing_route(monkeypatch):
    adapter = SimpleNamespace(select_execution_for_request=Mock())
    agent = JiuWenSwarm.__new__(JiuWenSwarm)
    monkeypatch.setattr(agent, "_ensure_adapter", Mock(return_value=adapter))
    request = AgentRequest(request_id="legacy", params={"mode": "agent.code.normal"})
    agent.prepare_session_execution(request)
    adapter.select_execution_for_request.assert_not_called()
