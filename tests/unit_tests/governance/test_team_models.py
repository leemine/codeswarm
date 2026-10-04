"""Pure Team factory and real core/SDK mock HTTP boundary; no Provider."""
import asyncio
import json
from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from openjiuwen.core.common.exception.errors import ModelRequestDenied
from openjiuwen.core.foundation.llm import ModelClientConfig, ModelRequestConfig
from openjiuwen.harness.execution_subject import ExecutionSubject, execution_subject_scope
from openjiuwen.harness.schema.build_context import BuildContext
from openjiuwen.harness.schema.deep_agent_spec import ModelSpec

from jiuwenswarm.governance.resources import ResourceAccessDenied
from jiuwenswarm.governance.team_models import make_native_team_model_factory
from jiuwenswarm.governance.tool_context import (
    NativeExecutionSlice, begin_native_execution_slice, end_native_execution_slice,
    current_native_execution_slice, native_authority_source_scope,
)


def metadata(model='model', base='https://model.example/v1', reference='account:one'):
    return {'model_client_config': dict(model_name=model, api_base=base, client_provider='OpenAI',
        api_key='MODEL_REQUEST_AUTHORITY', credential_reference=reference, credential_encoding='plain')}


def spec(model='model', base='https://model.example/v1', **overrides):
    return ModelSpec(model_client_config=ModelClientConfig(
        client_provider='OpenAI', api_base=base, api_key='must-not-enter-model', **overrides),
        model_request_config=ModelRequestConfig(model=model))


@pytest.fixture
def members():
    # Same visible names and card ids are deliberately not an ownership proof.
    contexts = [BuildContext(member_name='same', member_card_id='same-card', role='teammate') for _ in range(2)]
    owners = [object(), object()]
    live = [True, True]
    subjects = [ExecutionSubject('member-'+str(i), 'same', 'team_member', session_id='session-'+str(i))
                for i in range(2)]
    def index(context):
        return next((i for i, item in enumerate(contexts) if item is context), None)
    def owns_build(context):
        i = index(context)
        return i is not None and live[i]
    def owns_slice(context, bound):
        i = index(context)
        return i is not None and bound.owner is owners[i] and bound.subject is subjects[i] and live[i]
    return SimpleNamespace(**locals())


def factory(members, rows=None, **kwargs):
    return make_native_team_model_factory(catalog_metadata=rows if rows is not None else [metadata()],
        owns_build_context=kwargs.get('owns_build_context', members.owns_build),
        owns_execution_slice=kwargs.get('owns_execution_slice', members.owns_slice))


@contextmanager
def active(members, i, callback):
    bound = NativeExecutionSlice(members.owners[i], None, callback, members.subjects[i])
    def source():
        actual = current_native_execution_slice()
        return actual.model_authorizer if actual is not None and actual.active else None
    with native_authority_source_scope(lambda: None, model_source=source, slice_source=lambda _: bound):
        with execution_subject_scope(members.subjects[i]):
            handle = begin_native_execution_slice(object())
            try:
                yield bound
            finally:
                end_native_execution_slice(handle)


def authority(model):
    return model._client._request_authority


def test_explicit_factory_has_no_ambient_dependency_and_copies_model_config(members):
    rows = [metadata()]
    build = factory(members, rows)
    rows[0]['model_client_config']['api_base'] = 'https://changed.example/v1'
    original = spec()
    model = build(original, members.contexts[0])
    assert model.model_client_config.api_key == 'MODEL_REQUEST_AUTHORITY'
    assert original.model_client_config.api_key == 'must-not-enter-model'
    assert model.model_config is not original.model_request_config
    assert authority(model).delegate.binding.reference == 'account:one'
    assert authority(model).delegate.binding.api_base == 'https://model.example/v1'
    assert 'must-not-enter-model' not in repr(model.model_client_config)


@pytest.mark.parametrize('case', ['missing', 'duplicate', 'other_ref', 'other_encoding', 'headers',
                                  'responses', 'unknown_extra', 'wrong_model', 'child', 'unowned', 'unknown_role'])
def test_unsupported_or_ambiguous_construction_denied(members, case):
    rows, selected, context = [metadata()], spec(), members.contexts[0]
    if case == 'missing':
        rows = []
    elif case == 'duplicate':
        rows.append(metadata(reference='account:two'))
    elif case == 'other_ref':
        selected = spec(credential_reference='account:other')
    elif case == 'other_encoding':
        selected = spec(credential_encoding='host_crypto')
    elif case == 'headers':
        selected = spec(custom_headers={'Authorization': 'synthetic'})
    elif case == 'responses':
        selected = spec(api_mode='responses')
    elif case == 'unknown_extra':
        selected = spec(arbitrary_callback=lambda: None)
    elif case == 'wrong_model':
        selected = spec(model='other')
    elif case == 'child':
        context.subagent_name = 'child'
    elif case == 'unowned':
        context = replace(context)
    elif case == 'unknown_role':
        context.role = 'external_cli'
    with pytest.raises((ResourceAccessDenied, ValueError)):
        factory(members, rows)(selected, context)


def test_explicit_reference_selects_one_of_two_accounts_without_secret(members):
    rows = [metadata(), metadata(reference='account:two')]
    model = factory(members, rows)(spec(credential_reference='account:two'), members.contexts[0])
    assert authority(model).delegate.binding.reference == 'account:two'
    rows[0]['model_client_config']['api_key'] = 'synthetic-secret'
    with pytest.raises(ValueError, match='nonsecret'):
        factory(members, rows)


@pytest.mark.asyncio
async def test_two_same_named_members_cannot_exchange_slices(members):
    models = [factory(members)(spec(), context) for context in members.contexts]
    callbacks = [AsyncMock(return_value={'Authorization': 'Bearer synthetic-'+str(i)}) for i in range(2)]
    calls = []
    for i in range(2):
        with active(members, i, callbacks[i]):
            with pytest.raises(ResourceAccessDenied):
                authority(models[1-i]).bind_for_call()
            call = authority(models[i]).bind_for_call()
            calls.append(call)
            assert await call(object()) == {'Authorization': 'Bearer synthetic-'+str(i)}
    with active(members, 1, callbacks[1]):
        with pytest.raises(ResourceAccessDenied):
            await calls[0](object())
        with pytest.raises(ResourceAccessDenied):
            await calls[1](object())  # Same member, new slice is still another call source.
    assert [callback.await_count for callback in callbacks] == [1, 1]


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['owner', 'subject', 'callback', 'context', 'revoke', 'inactive'])
async def test_authorization_await_cannot_change_captured_member(members, change):
    model = factory(members)(spec(), members.contexts[0])
    async def callback(*_):
        bound = current_native_execution_slice()
        if change == 'owner':
            bound.owner = members.owners[1]
        elif change == 'subject':
            bound.subject = members.subjects[1]
        elif change == 'callback':
            bound.model_authorizer = AsyncMock()
        elif change == 'context':
            members.contexts[0].member_card_id = 'changed'
        elif change == 'revoke':
            members.live[0] = False
        else:
            bound.active = False
        await asyncio.sleep(0)
        return {'Authorization': 'Bearer must-be-discarded'}
    with active(members, 0, callback):
        call = authority(model).bind_for_call()
        with pytest.raises(ResourceAccessDenied):
            await call(object())


@pytest.mark.asyncio
async def test_factory_predicate_exceptions_are_secret_free(members):
    def denied(*_):
        raise RuntimeError('synthetic-secret')
    with pytest.raises(ResourceAccessDenied) as result:
        factory(members, owns_build_context=denied)(spec(), members.contexts[0])
    assert result.value.__context__ is None
    async def cancelled(*_):
        raise asyncio.CancelledError('synthetic-secret')
    model = factory(members)(spec(), members.contexts[0])
    with active(members, 0, cancelled):
        with pytest.raises(asyncio.CancelledError) as result:
            await authority(model).bind_for_call()(object())
    assert not result.value.args and result.value.__context__ is None


@pytest.fixture
def mock_http(monkeypatch):
    def install(respond):
        real_client = httpx.AsyncClient
        class FixtureClient(real_client):
            def __init__(self, **kwargs):
                super().__init__(transport=httpx.MockTransport(respond), **kwargs)
        monkeypatch.setattr(httpx, 'AsyncClient', FixtureClient)
    return install


@pytest.mark.asyncio
async def test_real_sdk_http_retries_recheck_original_slice(members, mock_http):
    requests, checks = [], []
    def respond(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(500, json={'error': {'message': 'retry'}})
        return httpx.Response(200, json={'id': 'synthetic', 'object': 'chat.completion',
            'created': 1, 'model': 'model', 'choices': [{'index': 0, 'message': {'role': 'assistant',
            'content': 'ok'}, 'finish_reason': 'stop'}]})
    mock_http(respond)
    async def callback(binding, target):
        checks.append((binding.reference, target.model, target.url))
        return {'Authorization': 'Bearer synthetic-'+str(len(checks))}
    model = factory(members)(spec(max_retries=1), members.contexts[0])
    with active(members, 0, callback):
        result = await asyncio.wait_for(model.invoke(messages=[{'role': 'user', 'content': 'fixture'}]), 5)
    assert result.content == 'ok'
    assert checks == [('account:one', 'model', 'https://model.example/v1/chat/completions')] * 2
    assert [request.headers['Authorization'] for request in requests] == ['Bearer synthetic-1', 'Bearer synthetic-2']
    with pytest.raises(ModelRequestDenied):
        await model.invoke(messages=[{'role': 'user', 'content': 'no source'}])
    assert len(requests) == 2


@pytest.mark.asyncio
async def test_real_sdk_retry_cannot_borrow_revoked_member(members, mock_http):
    requests = []
    def respond(request):
        requests.append(request)
        members.live[0] = False
        return httpx.Response(500, json={'error': {'message': 'retry'}})
    mock_http(respond)
    callback = AsyncMock(return_value={'Authorization': 'Bearer synthetic'})
    model = factory(members)(spec(max_retries=1), members.contexts[0])
    with active(members, 0, callback):
        with pytest.raises(ModelRequestDenied):
            await model.invoke('fixture')
    assert len(requests) == callback.await_count == 1


@pytest.mark.asyncio
async def test_real_sdk_stream_uses_member_slice_and_closes_cleanly(members, mock_http):
    requests = []
    def respond(request):
        requests.append(request)
        chunk = {'id': 'fixture', 'object': 'chat.completion.chunk', 'created': 1, 'model': 'model',
            'choices': [{'index': 0, 'delta': {'role': 'assistant', 'content': 'ok'}, 'finish_reason': None}]}
        return httpx.Response(200, headers={'content-type': 'text/event-stream'},
            content=('data: '+json.dumps(chunk)+'\n\ndata: [DONE]\n\n').encode())
    mock_http(respond)
    callback = AsyncMock(return_value={'Authorization': 'Bearer stream-fixture'})
    model = factory(members)(spec(max_retries=0), members.contexts[0])
    with active(members, 0, callback) as bound:
        iterator = model.stream('fixture')
        chunks = [chunk async for chunk in iterator]
        assert current_native_execution_slice() is bound
        await iterator.aclose()
    assert ''.join(chunk.content or '' for chunk in chunks) == 'ok'
    assert len(requests) == callback.await_count == 1
    assert callback.call_args.args[1].operation == 'stream'
    assert requests[0].headers['Authorization'] == 'Bearer stream-fixture'


@pytest.mark.asyncio
async def test_leader_requires_corresponding_execution_subject(members):
    members.contexts[0].role = 'leader'
    model = factory(members)(spec(), members.contexts[0])
    callback = AsyncMock(return_value={'Authorization': 'Bearer leader-fixture'})
    with active(members, 0, callback):
        with pytest.raises(ResourceAccessDenied):
            authority(model).bind_for_call()
    members.subjects[0] = replace(members.subjects[0], kind='team_leader')
    with active(members, 0, callback):
        assert await authority(model).bind_for_call()(object()) == {'Authorization': 'Bearer leader-fixture'}


@pytest.mark.parametrize('verdict', [1, None, 'yes'])
def test_build_owner_requires_exact_true(members, verdict):
    with pytest.raises(ResourceAccessDenied):
        factory(members, owns_build_context=lambda _: verdict)(spec(), members.contexts[0])
