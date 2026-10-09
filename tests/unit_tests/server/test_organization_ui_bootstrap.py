"""Organization bootstrap is a typed display DTO, never a config editor dump."""

from __future__ import annotations

import hashlib
import json
import secrets
import time
from types import SimpleNamespace

import pytest

from jiuwenswarm.common import config
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.governance import organization_auth
from jiuwenswarm.server.runtime.gateway_adapter.config_adapter import (
    ConfigAdapter,
    organization_ui_projection,
)
from jiuwenswarm.gateway.channel_manager.web import app_web_handlers


@pytest.fixture
def organization(tmp_path, monkeypatch):
    token = secrets.token_urlsafe(32)
    path = tmp_path / "organization.json"
    path.write_text(
        json.dumps(
            {
                "authority": "organization:bootstrap",
                "signing_key": secrets.token_hex(32),
                "credentials": [
                    {
                        "actor_id": "alice",
                        "sha256": hashlib.sha256(token.encode()).hexdigest(),
                        "expires_at": time.time() + 3600,
                        "revoked": False,
                    }
                ],
            }
        )
    )
    path.chmod(0o600)
    monkeypatch.setenv(organization_auth.CONFIG_ENV, str(path))
    auth = organization_auth.configured_authenticator()
    principal = auth.principal({"Authorization": "Bearer " + token})
    raw = {
        "a2ui": {"enabled": True},
        "rsi": {"enabled": False},
        "symphony": {"enabled": True},
        "setup_guide": {"enabled": True},
        "trajectory_ui": {"enabled": True},
        "permissions": {"enabled": True, "mode": "auto", "secret_policy": "private"},
        "browser": {"chrome_path": "/secret/private/browser"},
        "models": {
            "defaults": [
                {
                    "model_client_config": {
                        "model_name": "visible-model",
                        "client_provider": "OpenAI",
                        "api_key": "SECRET_API_KEY",
                        "api_base": "https://user:password@private.example",
                        "custom_headers": {"Authorization": "SECRET_HEADER"},
                        "vendor_key": "private-vendor",
                    },
                    "model_config_obj": {"context_window": 12345},
                    "alias": "visible-alias",
                    "is_default": True,
                }
            ]
        },
        "other": {"nested_credential": "SECRET_NESTED"},
    }
    monkeypatch.setattr(config, "get_config_raw", lambda: raw)
    monkeypatch.setattr(config, "get_config", lambda: raw)
    return SimpleNamespace(auth=auth, principal=principal, raw=raw, path=path)


class Channel:
    channel_id = "web"

    def __init__(self):
        self.methods = {}
        self.responses = []

    def register_method(self, name, handler):
        self.methods[name] = handler

    def on_connect(self, handler):
        pass

    def on_disconnect(self, handler):
        pass

    async def send_response(self, ws, req_id, **response):
        self.responses.append(response)


def request(method, params=None, channel="web"):
    return AgentRequest(
        request_id="bootstrap",
        channel_id=channel,
        req_method=ReqMethod(method),
        params=params,
    )


def no_legacy(*args, **kwargs):
    pytest.fail("full config, remote catalog or login-model path reached")


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["config.get", "models.list"])
async def test_authenticated_web_and_adapter_use_same_secret_free_projection(
    organization, monkeypatch, method
):
    monkeypatch.setattr(app_web_handlers, "get_config_raw", no_legacy)
    monkeypatch.setattr(app_web_handlers, "get_config", no_legacy)
    monkeypatch.setattr(app_web_handlers, "get_available_models", no_legacy)
    monkeypatch.setenv("API_KEY", "SECRET_ENV")
    channel = Channel()
    app_web_handlers._register_web_handlers(
        app_web_handlers.WebHandlersBindParams(channel=channel)
    )
    with organization_auth.authenticated_scope(organization.principal):
        await channel.methods[method](
            SimpleNamespace(_jiuwen_auth_session="other-login"), "web", {}, None
        )
        direct = await ConfigAdapter().handle(request(method))
        tui = await ConfigAdapter().handle(request(method, channel="tui"))
    assert direct.ok and tui.ok and channel.responses[-1]["ok"]
    assert direct.payload == tui.payload == channel.responses[-1]["payload"]
    body = json.dumps(direct.payload)
    for secret in (
        "SECRET_",
        "/secret",
        "private.example",
        "private-vendor",
        "secret_policy",
        "custom_headers",
        "origin_index",
    ):
        assert secret not in body
    if method == "config.get":
        assert set(direct.payload) == {
            "app_version",
            "runtime_platform",
            "external_cli_agents_supported",
            "a2ui_enabled",
            "rsi_enabled",
            "evaluation_enabled",
            "symphony_enabled",
            "permissions_profile",
            "permissions_enabled",
            "setup_guide_enabled",
            "trajectory_ui_enabled",
        }
        assert direct.payload["permissions_profile"] == "automatic"
        assert (
            direct.payload["setup_guide_enabled"]
            == direct.payload["trajectory_ui_enabled"]
            == "false"
        )
    else:
        model = direct.payload["models"][0]
        assert model == {
            "model_name": "visible-model",
            "selection_key": "visible-model#0",
            "model_provider": "OpenAI",
            "alias": "visible-alias",
            "is_default": True,
            "is_agentos": False,
            "is_free": False,
            "context_window_tokens": 12345,
            "read_only": True,
            "api_key": "",
            "api_base": "",
        }
        assert direct.payload["active_model"] == "visible-model"


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["config.get", "models.list"])
@pytest.mark.parametrize(
    "params",
    [{"include_secrets": True}, {"user_id": "bob"}, {"organization": False}, []],
)
async def test_bootstrap_rejects_parameter_smuggling_before_configuration_reads(
    organization, monkeypatch, method, params
):
    monkeypatch.setattr(config, "get_config_raw", no_legacy)
    monkeypatch.setattr(config, "get_config", no_legacy)
    with organization_auth.authenticated_scope(organization.principal):
        if isinstance(params, dict):
            result = await ConfigAdapter().handle(request(method, params))
            assert not result.ok and result.payload["code"] == "FORBIDDEN"
        else:
            with pytest.raises(PermissionError):
                organization_ui_projection(method, params)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method", ["config.set", "models.replace_all", "path.get", "path.set"]
)
async def test_organization_adapter_keeps_non_bootstrap_operations_closed(
    organization, method
):
    with organization_auth.authenticated_scope(organization.principal):
        result = await ConfigAdapter().handle(request(method, {}))
    assert not result.ok and result.payload["code"] == "FORBIDDEN"


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["config.get", "models.list"])
async def test_no_principal_or_revoke_during_read_never_releases_buffer(
    organization, monkeypatch, method
):
    result = await ConfigAdapter().handle(request(method, {}))
    assert not result.ok and result.payload["code"] == "FORBIDDEN"

    def revoke_read():
        organization.auth.revoke(organization.principal)
        return organization.raw

    monkeypatch.setattr(
        config,
        "get_config_raw",
        revoke_read,
    )
    with organization_auth.authenticated_scope(organization.principal):
        result = await ConfigAdapter().handle(request(method, {}))
    assert not result.ok and result.payload["code"] == "FORBIDDEN"
    assert "SECRET_" not in json.dumps(result.payload)


def test_projection_validates_types_and_lengths_without_stringifying_nested_data(
    organization, monkeypatch
):
    raw = organization.raw
    raw["a2ui"]["enabled"] = {"private": "SECRET_BOOL"}
    raw["permissions"] = {"enabled": "true", "mode": {"secret": "SECRET_MODE"}}
    monkeypatch.setenv("JIUWENSWARM_RUNTIME_PLATFORM", "/secret/platform")
    model = raw["models"]["defaults"][0]
    model["alias"] = {"private": "SECRET_ALIAS"}
    model["is_default"] = ["SECRET_DEFAULT"]
    model["is_free"] = "true"
    model["model_client_config"]["client_provider"] = {"private": "SECRET_PROVIDER"}
    model["model_config_obj"]["context_window"] = True
    raw["models"]["defaults"].append({"model_client_config": {"model_name": "X" * 257}})
    with organization_auth.authenticated_scope(organization.principal):
        flags = organization_ui_projection("config.get", {})
        models = organization_ui_projection("models.list", {})
    assert (
        flags["a2ui_enabled"] == "false"
        and flags["permissions_profile"] == "full_access"
    )
    assert flags["runtime_platform"] == "default"
    assert len(models["models"]) == 1
    shown = models["models"][0]
    assert shown["alias"] == shown["model_provider"] == ""
    # The existing catalog canonicalizes a sole model as default before DTO projection.
    assert shown["is_default"] is True and shown["is_free"] is False
    assert (
        type(shown["context_window_tokens"]) is int
        and shown["context_window_tokens"] > 1
    )
    assert "SECRET_" not in json.dumps((flags, models))


@pytest.mark.asyncio
async def test_loader_exception_never_echoes_secret_exception(
    organization, monkeypatch
):
    def fail():
        raise ValueError("SECRET_EXCEPTION")

    monkeypatch.setattr(config, "get_config_raw", fail)
    with organization_auth.authenticated_scope(organization.principal):
        result = await ConfigAdapter().handle(request("config.get", {}))
    assert result.payload["code"] == "INTERNAL_ERROR"
    assert "SECRET_EXCEPTION" not in json.dumps(result.payload)


def test_single_user_does_not_project_or_intercept_old_configuration(monkeypatch):
    monkeypatch.delenv(organization_auth.CONFIG_ENV, raising=False)
    assert organization_ui_projection("config.get", {"old": "params"}) is None
    assert (
        organization_ui_projection("config.set", {"api_key": "single-user-owned"})
        is None
    )


def test_projection_never_passes_untyped_catalog_flags(organization, monkeypatch):
    monkeypatch.setattr(
        config,
        "get_default_models",
        lambda _: [
            {
                "model_client_config": {"model_name": "typed-model"},
                "model_config_obj": {"context_window": {"secret": "PRIVATE_CONTEXT"}},
                "is_default": ["PRIVATE_DEFAULT"],
                "is_free": {"private": "PRIVATE_FREE"},
            }
        ],
    )
    with organization_auth.authenticated_scope(organization.principal):
        result = organization_ui_projection("models.list", {})
    shown = result["models"][0]
    assert shown["is_default"] is False and shown["is_free"] is False
    assert type(shown["context_window_tokens"]) is int
    assert "PRIVATE_" not in json.dumps(result)


def test_organization_model_keys_keep_complete_catalog_indices(organization, monkeypatch):
    entries = [
        {"model_client_config": {"model_name": "same", "api_key": "PRIVATE_A"}, "alias": "Alice"},
        {"model_client_config": {"model_name": ""}},
        {"model_client_config": {"model_name": "same", "api_key": "PRIVATE_B"}, "alias": "Bob"},
    ]
    monkeypatch.setattr(config, "get_default_models", lambda _: entries)
    with organization_auth.authenticated_scope(organization.principal):
        result = organization_ui_projection("models.list", {})
    assert [item["selection_key"] for item in result["models"]] == ["same#0", "same#2"]
    assert [item["model_name"] for item in result["models"]] == ["same", "same"]
    assert [item["alias"] for item in result["models"]] == ["Alice", "Bob"]
    assert "PRIVATE_" not in json.dumps(result)
