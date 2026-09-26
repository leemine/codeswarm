"""TUI Goal lane classification uses persisted server configuration facts."""
from copy import deepcopy

import pytest
from openjiuwen.harness.engine.config import config_fingerprint

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.runtime.harness.config_source import load_execution_catalog
from jiuwenswarm.runtime.service import AgentRuntime
from jiuwenswarm.runtime.session.model import SessionWorkKind


@pytest.fixture
def classification(monkeypatch):
    config = {"execution": {"default_profile_id": "native", "profiles": {
        "native": {"provider_id": "native", "config_revision": "r1"},
        "codex": {"provider_id": "codex", "config_revision": "r1"},
    }}}
    spec = load_execution_catalog(config).source(explicit_profile_id="codex").resolve()
    metadata = {"execution_profile_id": "codex", "execution_config_revision": "r1",
                "execution_config_fingerprint": config_fingerprint(spec)}
    monkeypatch.setattr("jiuwenswarm.common.config.get_config", lambda: config)
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.session_metadata.get_session_metadata",
        lambda *args, **kwargs: deepcopy(metadata),
    )
    return object.__new__(AgentRuntime), config, metadata


@pytest.mark.parametrize("query,expected", [
    ("/goal objective", SessionWorkKind.GOAL_STREAM),
    ("/goal resume", SessionWorkKind.GOAL_STREAM),
    ("/goal", SessionWorkKind.GOAL_CONTROL),
    ("/goal pause", SessionWorkKind.GOAL_CONTROL),
    ("/goal clear", SessionWorkKind.GOAL_CONTROL),
    ("ordinary", SessionWorkKind.CHAT_STREAM),
])
def test_server_bound_tui_goal_uses_existing_goal_lane(classification, query, expected):
    runtime, _, _ = classification
    req = AgentRequest(request_id="r", session_id="s", channel_id="tui",
                       req_method=ReqMethod.CHAT_SEND, is_stream=True,
                       params={"mode": "agent", "query": query})
    assert runtime._request_work_kind(req) is expected
    # The public Native classifier contract is unchanged.
    assert AgentRuntime.session_work_kind(req) is SessionWorkKind.CHAT_STREAM


@pytest.mark.parametrize("change", ["web", "native", "unbound", "revision", "fingerprint", "client_only", "cross_session"])
def test_no_external_control_lane_from_untrusted_or_stale_route(classification, change):
    runtime, config, metadata = classification
    req = AgentRequest(request_id="r", session_id="s", channel_id="tui",
                       req_method=ReqMethod.CHAT_SEND, is_stream=True,
                       params={"mode": "agent", "query": "/goal set test"})
    if change == "web":
        req.channel_id = "web"
    elif change == "native":
        metadata["execution_profile_id"] = "native"
        spec = load_execution_catalog(config).source(explicit_profile_id="native").resolve()
        metadata["execution_config_fingerprint"] = config_fingerprint(spec)
    elif change == "revision":
        metadata["execution_config_revision"] = "old"
    elif change == "fingerprint":
        metadata["execution_config_fingerprint"] = "old"
    elif change == "cross_session":
        from jiuwenswarm.common.session_message import SESSION_MESSAGE_INTERNAL_KEY

        req.params[SESSION_MESSAGE_INTERNAL_KEY] = {"source_session_id": "other"}
    else:
        metadata.clear()
        if change == "client_only":
            req.params.update(execution_profile_id="codex", provider_id="codex")
    assert runtime._request_work_kind(req) is SessionWorkKind.CHAT_STREAM
