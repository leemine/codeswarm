"""External Goal ingress retains the original channel and admission contracts."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.runtime.harness.request_binding import AdmittedExecutionRoute
from jiuwenswarm.server.runtime.agent_adapter import goal_model
from jiuwenswarm.server.runtime.agent_adapter.interface import (
    JiuWenSwarm,
    _external_goal_slash_intent,
)


def request(*, provider="codex", channel="tui", method=ReqMethod.CHAT_SEND, **params):
    item = AgentRequest(request_id="goal", session_id="session", channel_id=channel,
                        req_method=method, params=params)
    if provider is not None:
        item._execution_route = AdmittedExecutionRoute(
            channel, None, None,
            SimpleNamespace(binding=SimpleNamespace(provider_id=provider)), None,
        )
        item._bound_execution = item._execution_route.bound
    return item


@pytest.mark.parametrize("channel,provider,query,attach,expected", [
    ("tui", "codex", "/goal pause", False, {"action": "pause"}),
    ("tui", "codex", "/goal get", False, {"action": "set", "objective": "get"}),
    ("tui", "codex", "/goal stop", False, {"action": "set", "objective": "stop"}),
    ("web", "codex", "/goal pause", False, None),
    ("tui", "native", "/goal pause", False, None),
    ("tui", None, "/goal pause", False, None),
    ("tui", "codex", "/goal pause", True, None),
    ("tui", "codex", "ordinary chat", False, None),
])
def test_slash_control_only_after_external_tui_admission(channel, provider, query, attach, expected):
    assert _external_goal_slash_intent(request(
        provider=provider, channel=channel, query=query, attach_goal=attach,
    )) == expected


@pytest.mark.parametrize("container", ["params", "metadata"])
def test_relayed_session_message_cannot_become_goal_control(container):
    from jiuwenswarm.common.session_message import SESSION_MESSAGE_INTERNAL_KEY

    item = request(query="/goal clear")
    setattr(item, container, {
        **(getattr(item, container) or {}),
        SESSION_MESSAGE_INTERNAL_KEY: {"source_session_id": "other"},
    })
    assert _external_goal_slash_intent(item) is None


@pytest.mark.parametrize("method,params,provider,expected", [
    (ReqMethod.COMMAND_GOAL, {"action": "set"}, "codex", True),
    (ReqMethod.COMMAND_GOAL, {"action": "resume"}, "codex", True),
    (ReqMethod.COMMAND_GOAL, {"action": "pause"}, "codex", False),
    (ReqMethod.COMMAND_GOAL, {"action": "get"}, "codex", False),
    (ReqMethod.CHAT_SEND, {"query": "/goal resume"}, "codex", True),
    (ReqMethod.CHAT_SEND, {"query": "ordinary"}, "codex", False),
    (ReqMethod.COMMAND_GOAL, {"action": "set"}, "native", False),
    (ReqMethod.COMMAND_GOAL, {"action": "set"}, None, False),
])
def test_assessor_injected_only_for_admitted_external_work(monkeypatch, method, params, provider, expected):
    selected = False
    factory = object()
    def select(_request):
        nonlocal selected
        selected = True
    def resolve(_request):
        assert selected, "model resolution must follow execution selection"
        return factory
    resolver = Mock(side_effect=resolve)
    adapter = SimpleNamespace(select_execution_for_request=select, set_goal_assessor_factory=Mock())
    monkeypatch.setattr(goal_model, "request_goal_assessor_factory", resolver)
    item = request(method=method, provider=provider, **params)
    JiuWenSwarm._select_execution_before_mcp(adapter, item)
    if expected:
        resolver.assert_called_once_with(item)
        adapter.set_goal_assessor_factory.assert_called_once_with(factory, request=item)
    else:
        resolver.assert_not_called()
        adapter.set_goal_assessor_factory.assert_not_called()


@pytest.mark.parametrize("channel,query,expected", [
    ("tui", "/goal pause", []),
    ("tui", "ordinary", ["ordinary"]),
    ("web", "/goal pause", ["/goal pause"]),
])
async def test_stream_history_does_not_duplicate_tui_goal_control(monkeypatch, channel, query, expected):
    from jiuwenswarm.server.runtime.agent_adapter import interface as module
    from tests.unit_tests.agentserver.test_goal_history_bubble_parity import _ScriptedAdapter

    facade = JiuWenSwarm()
    adapter = _ScriptedAdapter([])
    adapter.select_execution_for_request = lambda _request: None
    monkeypatch.setattr(facade, "_adapter", adapter)
    monkeypatch.setattr(facade, "_sdk_name", "harness")
    rows = []
    monkeypatch.setattr(module, "append_history_record", lambda **kwargs: rows.append(kwargs))
    monkeypatch.setattr(module, "get_config", lambda: {"preferred_language": "zh"})
    monkeypatch.setattr(module, "get_memory_mode", lambda _config: "off")
    monkeypatch.setattr(module, "build_user_prompt", lambda q, **kwargs: q)
    item = request(channel=channel, query=query, mode="agent")
    async for _chunk in facade.process_message_stream(item):
        pass
    assert [row["content"] for row in rows if row.get("role") == "user"] == expected


@pytest.mark.parametrize("starts_work", [True, False])
def test_attach_resolves_assessor_only_when_admitted_owner_can_start(monkeypatch, starts_work):
    adapter = SimpleNamespace(
        select_execution_for_request=Mock(),
        needs_goal_assessor=Mock(return_value=starts_work),
        set_goal_assessor_factory=Mock(),
    )
    factory = object()
    resolver = Mock(return_value=factory)
    monkeypatch.setattr(goal_model, "request_goal_assessor_factory", resolver)
    item = request(channel="web", attach_goal=True)
    JiuWenSwarm._select_execution_before_mcp(adapter, item)
    adapter.needs_goal_assessor.assert_called_once_with(item)
    if starts_work:
        resolver.assert_called_once_with(item)
        adapter.set_goal_assessor_factory.assert_called_once_with(factory, request=item)
    else:
        resolver.assert_not_called()
        adapter.set_goal_assessor_factory.assert_not_called()
