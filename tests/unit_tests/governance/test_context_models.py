"""Real locked SDK compressors must retain the original final HTTP authority."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from openjiuwen.core.context_engine import ContextEngine, ContextEngineConfig
from openjiuwen.core.context_engine.processor import forked
from openjiuwen.core.foundation.llm import Model, ModelClientConfig, ModelRequestConfig, UserMessage
from jiuwenswarm.governance.context_models import bind_context_model_factory
from jiuwenswarm.governance.resources import ResourceAccessDenied
from openjiuwen.core.common.exception.errors import ModelRequestDenied
from jiuwenswarm.governance.model_consumer import NativeModelRequestAuthority
from jiuwenswarm.governance.model_credentials import ModelCredentialBinding
from jiuwenswarm.governance.tool_context import ExecutionResourceAuthorities, tool_authority_scope
from jiuwenswarm.agents.harness.common.rails.permissions.resource_authority_rail import NativeExecutionScopeRail


def owning_agent(*, governed=True):
    client = ModelClientConfig(client_provider='OpenAI', api_base='https://fixture.invalid/v1',
                               api_key='synthetic-config-key', model_name='fixture-model',
                               credential_reference='fixture:alice', credential_encoding='plain', max_retries=0)
    request = ModelRequestConfig(model='fixture-model')
    authority = NativeModelRequestAuthority(ModelCredentialBinding.from_config(client.model_dump()), 'e' * 64)
    source = Model(client, request, **({'request_authority': authority} if governed else {}))
    agent = SimpleNamespace(deep_config=SimpleNamespace(model=source),
                            react_agent=SimpleNamespace(context_engine=ContextEngine(ContextEngineConfig())))
    return agent, source, authority


def processor(agent, name='DialogueCompressor', **overrides):
    forked.activate()
    source = agent.deep_config.model
    cfg = getattr(forked, name+'Config')(model_client=source.model_client_config,
                                       model=source.model_config, **overrides)
    return agent.react_agent.context_engine._create_processor(name, cfg)


@pytest.mark.parametrize('name', ['DialogueCompressor', 'CurrentRoundCompressor', 'RoundLevelCompressor'])
def test_scope_init_preserves_exact_compressor_authority_without_global_registry_change(name):
    agent, source, authority = owning_agent()
    forked.activate()
    registered = dict(ContextEngine._PROCESSOR_MAP)
    NativeExecutionScopeRail().init(agent)
    value = processor(agent, name)
    assert value.processor_type() == name
    assert value._model._client._request_authority is authority
    assert value._model.model_client_config.api_key == 'MODEL_REQUEST_AUTHORITY'
    assert value._model.model_config == source.model_config
    assert ContextEngine._PROCESSOR_MAP == registered
    assert 'synthetic-config-key' not in value.config.model_dump_json()


def test_factory_is_idempotent_and_follows_owning_model_hot_reload():
    agent, _, first = owning_agent()
    bind_context_model_factory(agent)
    original = agent.react_agent.context_engine._create_processor._native_original_factory
    bind_context_model_factory(agent)
    assert agent.react_agent.context_engine._create_processor._native_original_factory == original
    assert processor(agent)._model._client._request_authority is first
    other, _, second = owning_agent()
    agent.deep_config.model = other.deep_config.model
    assert processor(agent)._model._client._request_authority is second


@pytest.mark.parametrize('defect', ['endpoint', 'credential', 'model', 'authority'])
def test_mismatched_compression_override_or_lost_authority_fails_before_http(defect):
    agent, source, _ = owning_agent()
    bind_context_model_factory(agent)
    cfg = forked.DialogueCompressorConfig(model_client=source.model_client_config,
                                         model=source.model_config)
    if defect in {'endpoint', 'credential'}:
        change = {'api_base': 'https://other.invalid/v1'} if defect == 'endpoint' else {'credential_reference': 'fixture:bob'}
        cfg.model_client = cfg.model_client.model_copy(update=change)
    elif defect == 'model':
        cfg.model = cfg.model.model_copy(update={'model_name': 'other-model'})
    else:
        source._client._request_authority = None
        value = agent.react_agent.context_engine._create_processor('DialogueCompressor', cfg)
        with pytest.raises(ResourceAccessDenied):
            value._model._client._request_authority.bind_for_call()
        return
    with pytest.raises(ResourceAccessDenied):
        agent.react_agent.context_engine._create_processor('DialogueCompressor', cfg)


def test_legacy_processor_keeps_literal_key_and_settings(monkeypatch):
    from jiuwenswarm.governance import organization_auth
    monkeypatch.setattr(organization_auth, 'configured_authenticator', lambda: None)
    agent, _, _ = owning_agent(governed=False)
    bind_context_model_factory(agent)
    value = processor(agent, trigger_context_ratio=0.55)
    assert value._model.model_client_config.api_key == 'synthetic-config-key'
    assert value.config.trigger_context_ratio == 0.55
    assert value._model._client._request_authority is None


@pytest.mark.asyncio
@pytest.mark.parametrize('mismatch', [False, True])
async def test_cold_history_factory_waits_for_exact_owner_and_captures_original_call(mismatch):
    agent, source, _ = owning_agent()
    bind_context_model_factory(agent)
    cfg = forked.DialogueCompressorConfig(model_client=source.model_client_config,
                                         model=source.model_config)
    agent.deep_config.model = None
    value = agent.react_agent.context_engine._create_processor('DialogueCompressor', cfg)
    factory = value._model._client._request_authority
    with pytest.raises(ResourceAccessDenied):
        factory.bind_for_call()
    agent.deep_config.model = source
    if mismatch:
        cfg.model_client = cfg.model_client.model_copy(update={'api_base': 'https://other.invalid/v1'})
        # A distinct deferred factory must not borrow the original credential.
        agent.deep_config.model = None
        value = agent.react_agent.context_engine._create_processor('DialogueCompressor', cfg)
        agent.deep_config.model = source
        with pytest.raises(ResourceAccessDenied):
            value._model._client._request_authority.bind_for_call()
        return
    first = AsyncMock(return_value={'Authorization': 'Bearer first'})
    second = AsyncMock(return_value={'Authorization': 'Bearer second'})
    with tool_authority_scope(None, provider_authorizers=ExecutionResourceAuthorities({}, first)):
        call = factory.bind_for_call()
        await call(SimpleNamespace())
    with tool_authority_scope(None, provider_authorizers=ExecutionResourceAuthorities({}, second)):
        with pytest.raises(ResourceAccessDenied):
            await call(SimpleNamespace())
    second.assert_not_awaited()
    assert first.await_args.kwargs == {'model_entry_fingerprint': 'e' * 64}


@pytest.mark.asyncio
async def test_compressor_http_uses_live_owner_and_denies_missing_scope(monkeypatch):
    requests=[]
    def respond(request):
        requests.append(request)
        assert request.headers['Authorization'] == 'Bearer synthetic-current-owner'
        return httpx.Response(200, json={'id':'fixture', 'object':'chat.completion', 'model':'fixture-model',
            'choices':[{'index':0,'message':{'role':'assistant','content':'ok'},'finish_reason':'stop'}]})
    monkeypatch.setattr(httpx.AsyncClient, '_init_transport',
                        lambda *args, **kwargs: httpx.MockTransport(respond))
    agent, _, _ = owning_agent()
    bind_context_model_factory(agent)
    model = processor(agent)._model
    with pytest.raises(ModelRequestDenied):
        await model.invoke([UserMessage(content='public fixture')])
    assert not requests
    authorize = AsyncMock(return_value={'Authorization':'Bearer synthetic-current-owner'})
    with tool_authority_scope(None, provider_authorizers=ExecutionResourceAuthorities({}, authorize)):
        assert (await model.invoke([UserMessage(content='public fixture')])).content == 'ok'
    assert len(requests) == 1
    assert authorize.await_args.kwargs == {'model_entry_fingerprint': 'e' * 64}
    denied = AsyncMock(return_value=None)
    with tool_authority_scope(None, provider_authorizers=ExecutionResourceAuthorities({}, denied)):
        with pytest.raises(ModelRequestDenied):
            await model.invoke([UserMessage(content='must not leave')])
    assert len(requests) == 1
    await model._client.aclose()


@pytest.mark.asyncio
async def test_real_deep_agent_initialization_binds_preset_compressor():
    from openjiuwen.core.single_agent.schema.agent_card import AgentCard
    from openjiuwen.harness.deep_agent import DeepAgent
    from openjiuwen.harness.schema.config import DeepAgentConfig
    from openjiuwen.harness.rails.context_engineer import ContextProcessorRail
    _, source, authority = owning_agent()
    card = AgentCard(id='context-authority-init-fixture', name='fixture')
    agent = DeepAgent(card).configure(DeepAgentConfig(
        card=card, model=source, tools=[], subagents=[], add_general_purpose_agent=False,
        enable_task_loop=False, enable_read_image_multimodal=False,
        rails=[NativeExecutionScopeRail(), ContextProcessorRail()],
    ))
    await agent.ensure_initialized()
    kind, cfg = next((name, cfg) for name, cfg in agent.react_agent.config.context_processors
                     if name == 'DialogueCompressor')
    value = agent.react_agent.context_engine._create_processor(kind, cfg)
    assert value._model._client._request_authority is authority
