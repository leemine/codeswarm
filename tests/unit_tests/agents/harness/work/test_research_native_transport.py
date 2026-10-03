# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Locked Native transport diagnostics; HTTP is replaced, no remote model calls.

These tests establish timeout/retry and wire-mode behavior, not remote service
latency or research quality. Small stream timers only accelerate fault injection.
"""
import asyncio
import json

import httpx
import pytest

from openjiuwen.core.common.exception.errors import FrameworkError
from openjiuwen.core.foundation.llm import Model, ModelClientConfig, ModelRequestConfig, UserMessage


def _model(**overrides):
    return Model(
        model_client_config=ModelClientConfig(
            client_provider='OpenAI', api_base='https://example.invalid/v1', api_key='unused',
            timeout=90, use_shared_llm_http_client=False, **overrides,
        ),
        model_config=ModelRequestConfig(model='request-capture', max_tokens=8192, temperature=0.1),
    )


def _mock_http(monkeypatch, handler):
    original = httpx.AsyncClient

    class LocalClient(original):
        def __init__(self, *args, **kwargs):
            kwargs.pop('proxy', None)
            super().__init__(*args, **kwargs, transport=httpx.MockTransport(handler))

    monkeypatch.setattr(httpx, 'AsyncClient', LocalClient)


@pytest.mark.asyncio
async def test_nonstream_read_timeout_retries_once_at_unchanged_90_second_http_limit(monkeypatch):
    requests = []

    async def fail_read(request):
        requests.append(request)
        raise httpx.ReadTimeout('deterministic stalled response', request=request)

    _mock_http(monkeypatch, fail_read)
    model = _model()
    assert model.model_client_config.max_retries == 1
    with pytest.raises(FrameworkError, match='APITimeoutError'):
        await model.invoke([UserMessage(content='synthetic request')])
    assert len(requests) == 2  # Initial HTTP attempt plus one SDK retry.
    for request in requests:
        body = json.loads(request.content)
        assert body['stream'] is False
        assert body['max_tokens'] == 8192
        assert request.extensions['timeout']['read'] == 90
    assert model.model_client_config.stream_first_chunk_timeout == 300
    assert model.model_client_config.stream_idle_timeout == 60


@pytest.mark.asyncio
async def test_stream_keeps_budget_and_http_timeout_and_requests_usage(monkeypatch):
    requests = []
    payload = {
        'id': 'synthetic', 'object': 'chat.completion.chunk', 'created': 0,
        'model': 'request-capture',
        'choices': [{'index': 0, 'delta': {'content': 'complete'}, 'finish_reason': 'stop'}],
    }

    async def reply(request):
        requests.append(request)
        return httpx.Response(
            200, headers={'content-type': 'text/event-stream'},
            content=f'data: {json.dumps(payload)}\n\ndata: [DONE]\n\n',
        )

    _mock_http(monkeypatch, reply)
    chunks = [chunk async for chunk in _model().stream([UserMessage(content='synthetic request')])]
    assert ''.join(chunk.content or '' for chunk in chunks) == 'complete'
    assert len(requests) == 1
    request, = requests
    body = json.loads(request.content)
    assert body['stream'] is True
    assert body['max_tokens'] == 8192
    assert body['stream_options']['include_usage'] is True
    assert request.extensions['timeout']['read'] == 90


@pytest.mark.asyncio
@pytest.mark.parametrize('first_chunk', [True, False])
async def test_stream_first_or_idle_timeout_is_bounded_and_not_sdk_retry(monkeypatch, first_chunk):
    requests = []
    closed = []

    class StalledSSE(httpx.AsyncByteStream):
        async def __aiter__(self):
            if not first_chunk:
                yield (
                    b'data: {"id":"synthetic","object":"chat.completion.chunk","created":0,'
                    b'"model":"request-capture","choices":[{"index":0,"delta":{"content":"part"},'
                    b'"finish_reason":null}]}\n\n'
                )
            await asyncio.Event().wait()

        async def aclose(self):
            closed.append(True)

    async def reply(request):
        requests.append(request)
        return httpx.Response(200, headers={'content-type': 'text/event-stream'}, stream=StalledSSE())

    _mock_http(monkeypatch, reply)
    model = _model(stream_first_chunk_timeout=0.05, stream_idle_timeout=0.05)
    with pytest.raises(FrameworkError, match=f"stage={'first_chunk' if first_chunk else 'idle_chunk'}"):
        async with asyncio.timeout(2):
            async for _ in model.stream([UserMessage(content='synthetic request')]):
                pass
    assert len(requests) == 1
    assert closed


@pytest.mark.asyncio
@pytest.mark.parametrize('streaming', [False, True])
async def test_runner_entry_selects_model_wire_mode_and_preserves_terminal_result(tmp_path, monkeypatch, streaming):
    from openjiuwen.core.foundation.llm import AssistantMessage
    from openjiuwen.core.foundation.llm.schema.message_chunk import AssistantMessageChunk
    from openjiuwen.core.runner import Runner
    from openjiuwen.core.sys_operation import LocalWorkConfig, OperationMode, SysOperation, SysOperationCard
    from openjiuwen.harness import create_deep_agent

    calls = []

    async def invoke(_model, messages, **kwargs):
        calls.append('invoke')
        return AssistantMessage(content='Synthetic complete result.')

    async def stream(_model, messages, **kwargs):
        calls.append('stream')
        yield AssistantMessageChunk(content='Synthetic complete result.')

    monkeypatch.setattr(Model, 'invoke', invoke)
    monkeypatch.setattr(Model, 'stream', stream)
    operation = SysOperation(SysOperationCard(
        id=f'runner-mode-{streaming}', mode=OperationMode.LOCAL,
        work_config=LocalWorkConfig(sandbox_root=[str(tmp_path)], restrict_to_sandbox=True),
    ))
    agent = create_deep_agent(
        model=_model(), workspace=str(tmp_path), sys_operation=operation, rails=[],
        enable_task_loop=False, enable_read_image_multimodal=False,
        system_prompt='Synthetic transport fixture.',
    )
    await Runner.start()
    try:
        if streaming:
            chunks = [chunk async for chunk in Runner.run_agent_streaming(
                agent, {'query': 'Synthetic request'}, session=f'runner-mode-{streaming}',
            )]
            # A closed stream alone is insufficient: require the explicit final
            # structured ReAct result with its answer type and complete output.
            terminal = [chunk.payload for chunk in chunks if getattr(chunk, 'type', None) == 'answer']
            assert len(terminal) == 1
            result = terminal[0]
        else:
            result = await Runner.run_agent(
                agent, {'query': 'Synthetic request'}, session=f'runner-mode-{streaming}',
            )
        assert result['result_type'] == 'answer'
        assert result['output'] == 'Synthetic complete result.'
        assert calls == ['stream' if streaming else 'invoke']
    finally:
        await agent.stop()
        await Runner.stop()
