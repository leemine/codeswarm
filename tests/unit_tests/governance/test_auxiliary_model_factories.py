"""Auxiliary constructors retain the same host binding as the primary model.

Model is replaced at the construction boundary: these are wiring tests, not
HTTP/SPI integration acceptance against the installed pre-SPI core version.
"""
import importlib

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from openjiuwen.core.common.exception.errors import ValidationError as CoreValidationError

from jiuwenswarm.governance import organization_auth
from jiuwenswarm.governance.model_consumer import NativeModelRequestAuthority
from jiuwenswarm.governance.resources import ResourceAccessDenied
from jiuwenswarm.governance.tool_context import ExecutionResourceAuthorities, tool_authority_scope



@pytest.fixture(scope="module", autouse=True)
def load_factories_before_constructor_spies():
    # Keep real Model types in transitive module annotations/imports. Spies below
    # replace only the late-bound constructor dependency used by each factory.
    for module in (
        "agents.harness.team.team_runtime_inheritance",
        "agents.swarm.providers.evolution_rails",
        "agents.harness.common.tools.wiki_tools",
        "agents.harness.common.tools.image_tools",
        "agents.harness.common.recommendation.proactive_actions",
        "agents.harness.common.auto_harness.service",
        "agents.harness.common.memory.dreaming.sweeper",
        "server.hooks.executor",
        "server.runtime.session.kv_cache.kv_cache_model_provider",
        "symphony.llm", "gateway.im_pipeline.im_inbound",
        "gateway.im_pipeline.im_outbound",
        "gateway.channel_manager.web.app_web_handlers",
        "gateway.channel_manager.tui.tui_connect",
    ):
        importlib.import_module("jiuwenswarm." + module)


@pytest.fixture
def catalog(monkeypatch):
    from jiuwenswarm.common import config
    mcc = dict(model_name='fixture-model', client_provider='OpenAI',
               api_base='https://fixture.invalid/v1', api_key='stored-synthetic-secret',
               credential_reference='catalog:alice', credential_encoding='plain')
    entry = {'model_client_config': mcc, 'model_config_obj': {'temperature': 0.3}, 'is_default': True}
    cfg = {'models': {'defaults': [entry], 'default': entry}, 'react': entry}
    monkeypatch.setattr(config, 'get_config', lambda: deepcopy(cfg))
    monkeypatch.setattr(config, 'get_default_models', lambda *_: [deepcopy(entry)])
    return mcc, entry, cfg


def build_auxiliary(name, monkeypatch, catalog):
    mcc, entry, cfg = catalog
    capture = Mock(side_effect=lambda **kwargs: SimpleNamespace(**kwargs))
    class CapturedModel:
        def __new__(cls, **kwargs):
            return capture(**kwargs)
    monkeypatch.setattr('openjiuwen.core.foundation.llm.Model', CapturedModel)
    if name == 'team':
        from jiuwenswarm.agents.harness.team.team_runtime_inheritance import build_evolution_llm
        model = build_evolution_llm(deepcopy(cfg))[0]
    elif name == 'team-provider':
        from jiuwenswarm.agents.swarm.providers.evolution_rails import _build_evolution_llm_from
        model = _build_evolution_llm_from({**deepcopy(entry), 'model_name': mcc['model_name']})[0]
    elif name == 'wiki':
        from jiuwenswarm.agents.harness.common.tools import wiki_tools
        monkeypatch.setattr(wiki_tools, 'Model', capture)
        monkeypatch.setattr(wiki_tools, 'get_default_models', lambda: [deepcopy(entry)])
        model = wiki_tools._get_default_model()
    elif name == 'proactive':
        from jiuwenswarm.agents.harness.common.recommendation.proactive_actions import _get_model
        model = _get_model(temperature=0.2)
    elif name == 'symphony':
        from jiuwenswarm.symphony.llm import LLMConfig
        model = LLMConfig.from_model_entry(deepcopy(entry)).create_model()
    elif name == 'im-inbound':
        from jiuwenswarm.gateway.im_pipeline import im_inbound
        monkeypatch.setattr(im_inbound, 'Model', capture)
        processor = object.__new__(im_inbound.IMConversationProcessor)
        processor._llm = None
        processor._model_client_raw = deepcopy(mcc)
        processor._model_config_raw = {}
        processor._model_name = mcc['model_name']
        model = processor._ensure_llm()
    else:
        from jiuwenswarm.gateway.im_pipeline import im_outbound
        processor = im_outbound.IMOutboundPipeline()
        assert processor._ensure_llm()
        model = processor._llm
    capture.assert_called_once()
    return model


@pytest.mark.parametrize('name', ['team', 'team-provider', 'wiki', 'proactive', 'symphony', 'im-inbound', 'im-outbound'])
@pytest.mark.parametrize('governed', [False, True])
def test_auxiliary_factories_preserve_binding_and_legacy_configuration(monkeypatch, catalog, name, governed):
    monkeypatch.setattr(organization_auth, 'configured_authenticator', lambda: object() if governed else None)
    model = build_auxiliary(name, monkeypatch, catalog)
    if governed:
        assert model.model_client_config.api_key == 'MODEL_REQUEST_AUTHORITY'
        assert isinstance(model.request_authority, NativeModelRequestAuthority)
        assert model.request_authority.binding.credential_reference == 'catalog:alice'
        assert model.request_authority.binding.credential_encoding == 'plain'
        assert model.request_authority.binding.model == 'fixture-model'
        with pytest.raises(ResourceAccessDenied):
            model.request_authority.bind_for_call()  # Background construction gives no ambient authority.
    else:
        assert model.model_client_config.api_key == 'stored-synthetic-secret'
        assert not hasattr(model, 'request_authority')
    assert model.model_config.model_name == 'fixture-model'


@pytest.mark.asyncio
async def test_auxiliary_shared_model_captures_each_call_authority(monkeypatch, catalog):
    monkeypatch.setattr(organization_auth, 'configured_authenticator', lambda: object())
    model = build_auxiliary('team-provider', monkeypatch, catalog)
    first, second = AsyncMock(return_value={'Authorization': 'first'}), AsyncMock(return_value={'Authorization': 'second'})
    with tool_authority_scope(None, provider_authorizers=ExecutionResourceAuthorities({}, first)):
        old_call = model.request_authority.bind_for_call()
        assert await old_call(object()) == {'Authorization': 'first'}
    with tool_authority_scope(None, provider_authorizers=ExecutionResourceAuthorities({}, second)):
        with pytest.raises(ResourceAccessDenied):
            await old_call(object())
        assert await model.request_authority.bind_for_call()(object()) == {'Authorization': 'second'}
    first.assert_awaited_once()
    second.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('consumer', ['hook', 'memory'])
async def test_hook_and_memory_models_retain_binding(monkeypatch, catalog, consumer, tmp_path):
    monkeypatch.setattr(organization_auth, 'configured_authenticator', lambda: object())
    captured = []
    def construct(**kwargs):
        captured.append(kwargs)
        return SimpleNamespace(invoke=AsyncMock(return_value=SimpleNamespace(content='[]')))
    class CapturedModel:
        def __new__(cls, **kwargs):
            return construct(**kwargs)
    monkeypatch.setattr('openjiuwen.core.foundation.llm.Model', CapturedModel)
    if consumer == 'hook':
        from jiuwenswarm.server.hooks.executor import HookExecutor
        await HookExecutor()._query_llm('fixture')
    else:
        from jiuwenswarm.agents.harness.common.memory.dreaming.sweeper import Sweeper
        await Sweeper(str(tmp_path), str(tmp_path))._extract_via_llm('fixture', '')
    assert len(captured) == 1
    assert captured[0]['model_client_config'].api_key == 'MODEL_REQUEST_AUTHORITY'
    assert captured[0]['request_authority'].binding.credential_reference == 'catalog:alice'


@pytest.mark.parametrize('consumer', ['auto-env', 'kv-cache'])
def test_unbound_auxiliary_models_reject_before_model_construction(monkeypatch, catalog, consumer):
    monkeypatch.setattr(organization_auth, 'configured_authenticator', lambda: object())
    capture = Mock(side_effect=AssertionError('unbound Model must not be constructed'))
    class CapturedModel:
        def __new__(cls, **kwargs):
            return capture(**kwargs)
    monkeypatch.setattr('openjiuwen.core.foundation.llm.Model', CapturedModel)
    if consumer == 'auto-env':
        from jiuwenswarm.agents.harness.common.auto_harness import service
        monkeypatch.setattr(service, 'Model', capture)
        monkeypatch.setenv('API_KEY', 'synthetic-ambient')
        monkeypatch.setenv('API_BASE', 'https://fixture.invalid/v1')
        monkeypatch.setenv('MODEL_NAME', 'fixture-model')
        invoke = service.AutoHarnessService._build_model_from_env
    else:
        from jiuwenswarm.server.runtime.session.kv_cache import kv_cache_model_provider as provider
        monkeypatch.setattr(provider, 'get_config', lambda: deepcopy(catalog[2]))
        monkeypatch.setattr(provider, 'get_default_models', lambda *_: [deepcopy(catalog[1])])
        monkeypatch.setattr(provider, 'has_kv_cache_affinity_capability', lambda _: True)
        invoke = provider.create_default_kv_cache_model
    with pytest.raises(ResourceAccessDenied):
        invoke()
    capture.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize('name', ['_invoke_openai_vision', '_invoke_gemini_vision', '_invoke_model_image_generation'])
async def test_unscoped_image_consumers_reject_before_io(monkeypatch, name):
    from jiuwenswarm.agents.harness.common.tools import image_tools
    monkeypatch.setattr(organization_auth, 'configured_authenticator', lambda: object())
    credentials = Mock(side_effect=AssertionError('no ambient credentials'))
    monkeypatch.setattr(image_tools, '_get_vision_api_credentials', credentials)
    monkeypatch.setattr(image_tools, 'OpenAI', credentials)
    call = getattr(image_tools, name)
    args = ('fixture',) if name == '_invoke_model_image_generation' else ('fixture', 'question')
    with pytest.raises(ResourceAccessDenied):
        await call(*args)
    credentials.assert_not_called()


@pytest.mark.parametrize("name", ["team", "team-provider", "wiki", "proactive", "symphony", "im-inbound", "im-outbound"])
@pytest.mark.parametrize("missing", ["credential_encoding", "api_base"])
def test_auxiliary_factories_do_not_fill_missing_host_metadata(monkeypatch, catalog, name, missing):
    monkeypatch.setattr(organization_auth, "configured_authenticator", lambda: object())
    del catalog[0][missing]
    monkeypatch.setenv("API_BASE", "https://ambient.invalid/v1")
    constructor = Mock(side_effect=AssertionError("unbound model must not be constructed"))
    monkeypatch.setattr("openjiuwen.core.foundation.llm.Model", constructor)
    if name == "team":
        from jiuwenswarm.agents.harness.team.team_runtime_inheritance import build_evolution_llm
        def invoke():
            return build_evolution_llm(deepcopy(catalog[2]))
    elif name == "team-provider":
        from jiuwenswarm.agents.swarm.providers.evolution_rails import _build_evolution_llm_from
        def invoke():
            return _build_evolution_llm_from({**deepcopy(catalog[1]), "model_name": "fixture-model"})
    elif name == "wiki":
        from jiuwenswarm.agents.harness.common.tools import wiki_tools
        monkeypatch.setattr(wiki_tools, "Model", constructor)
        monkeypatch.setattr(wiki_tools, "get_default_models", lambda: [deepcopy(catalog[1])])
        invoke = wiki_tools._get_default_model
    elif name == "proactive":
        from jiuwenswarm.agents.harness.common.recommendation.proactive_actions import _get_model
        invoke = _get_model
    elif name == "symphony":
        from jiuwenswarm.symphony.llm import LLMConfig
        def invoke():
            return LLMConfig.from_model_entry(deepcopy(catalog[1])).create_model()
    elif name == "im-inbound":
        from jiuwenswarm.gateway.im_pipeline import im_inbound
        monkeypatch.setattr(im_inbound, "Model", constructor)
        processor = im_inbound.IMConversationProcessor()
        invoke = processor._ensure_llm
    else:
        from jiuwenswarm.gateway.im_pipeline.im_outbound import IMOutboundPipeline
        invoke = IMOutboundPipeline()._ensure_llm
    # Existing optional helper fallbacks may return None/False. No fallback may
    # construct a model with credentials or an endpoint borrowed from env.
    try:
        assert not invoke()
    except (ResourceAccessDenied, ValueError, RuntimeError, CoreValidationError):
        pass
    constructor.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["web", "tui"])
async def test_connection_probe_cannot_use_unbound_user_credentials(monkeypatch, kind):
    monkeypatch.setattr(organization_auth, "configured_authenticator", lambda: object())
    handlers = {}
    channel = SimpleNamespace(
        channel_id=kind,
        register_method=lambda name, fn: handlers.__setitem__(name, fn),
        register_local_handler=lambda path, name, fn: handlers.__setitem__(name, fn),
        on_connect=lambda fn: None,
        on_disconnect=lambda fn: None,
        send_response=AsyncMock(),
    )
    if kind == "web":
        from jiuwenswarm.gateway.channel_manager.web import app_web_handlers as module
        module._register_web_handlers(module.WebHandlersBindParams(channel=channel))
    else:
        from jiuwenswarm.gateway.channel_manager.tui import tui_connect as module
        module.register_cli_handlers(module.CliHandlersBindParams(channel=channel, path="/tui"))
    constructor = Mock(side_effect=AssertionError("unbound model must not be constructed"))
    probe = AsyncMock(side_effect=AssertionError("unbound probe must not run"))
    monkeypatch.setattr(module, "Model", constructor)
    monkeypatch.setattr(module, "probe_model_connection", probe)
    with pytest.raises(ResourceAccessDenied):
        await handlers["config.validate_model"](object(), "fixture", {
            "model_provider": "openai", "model": "fixture-model",
            "api_base": "https://fixture.invalid/v1", "api_key": "untrusted-key",
        }, "session-fixture")
    constructor.assert_not_called()
    probe.assert_not_awaited()
