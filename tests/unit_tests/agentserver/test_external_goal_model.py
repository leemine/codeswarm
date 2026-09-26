"""Assessor selection follows server configuration, never provider credentials."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.runtime.model_catalog import ModelCatalogError
from jiuwenswarm.runtime.harness import goal_assessment
from jiuwenswarm.server.runtime.agent_adapter import goal_model as module


@pytest.mark.parametrize("requested,stored,expected", [("explicit", "stored", "explicit"), ("", "stored", "stored"), ("", "", "default#0")])
def test_model_selector_precedence_and_server_snapshot(monkeypatch, requested, stored, expected):
    runtime = SimpleNamespace(
        resolve_model_capability=Mock(return_value=SimpleNamespace(selection_key="chosen#2", is_agentos=False)),
        list_model_capabilities=Mock(return_value=SimpleNamespace(current_selection="default#0")),
    )
    entries = [{"server": ["only"]}]
    factory = Mock(return_value="factory")
    monkeypatch.setattr(module, "get_current_runtime", lambda: runtime)
    monkeypatch.setattr(module, "get_default_models", lambda: entries)
    monkeypatch.setattr(module, "get_session_metadata", lambda *args, **kwargs: {"model": stored})
    monkeypatch.setattr(goal_assessment, "catalog_model_factory", factory)
    request = AgentRequest(request_id="r", session_id="s", params={"model_name": requested, "api_key": "untrusted", "api_base": "https://untrusted.invalid"})
    assert module.request_goal_assessor_factory(request) == "factory"
    runtime.resolve_model_capability.assert_called_once_with(expected)
    assert factory.call_args.args == (entries, "chosen#2")
    assert factory.call_args.args[0] is not entries
    if requested or stored:
        runtime.list_model_capabilities.assert_not_called()


def test_invalid_explicit_model_does_not_fall_back(monkeypatch):
    runtime = SimpleNamespace(resolve_model_capability=Mock(side_effect=ModelCatalogError("unknown", code="MODEL_NOT_FOUND")))
    monkeypatch.setattr(module, "get_current_runtime", lambda: runtime)
    with pytest.raises(ModelCatalogError, match="unknown"):
        module.request_goal_assessor_factory(AgentRequest(request_id="r", params={"model_name": "missing"}))


def test_raw_login_credentials_are_rejected_before_registration_or_factory(monkeypatch):
    lookup = Mock()
    monkeypatch.setattr(module, "get_current_runtime", lookup)
    request = AgentRequest(request_id="r", params={module.E2A_MODEL_AUTH_PARAM_KEY: {"api_key": "never-use"}})
    with pytest.raises(ModelCatalogError) as exc:
        module.request_goal_assessor_factory(request)
    assert exc.value.code == "GOAL_ASSESSOR_AUTHORIZATION_REQUIRED"
    assert "never-use" not in str(exc.value)
    lookup.assert_not_called()
