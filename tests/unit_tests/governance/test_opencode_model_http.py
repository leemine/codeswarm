"""Actual ASGI/core source/resource authority/HTTPX, with synthetic native/model I/O."""

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
import pytest_asyncio

from openjiuwen.harness_providers.opencode import OpenCodeModelConfig
from jiuwenswarm.governance.contracts import TrustedIdentity
from jiuwenswarm.governance.credential_resources import (
    BoundCredentialAuthority,
    CredentialUse,
)
from jiuwenswarm.governance.model_credentials import ModelCredentialBinding
from jiuwenswarm.governance.opencode_model_http import OpenCodeModelOperationAuthority
from jiuwenswarm.governance.resources import ResourceDefinition
from jiuwenswarm.governance.tool_resources import ResourceExecutionContext
from jiuwenswarm.runtime.harness.execution_session import ExecutionExitState
from jiuwenswarm.runtime.harness.tool_transport import ManagedProductToolTransport
from jiuwenswarm.server.runtime.session import project_store
from jiuwenswarm.server.runtime.session.project_access import ProjectAccessStore
from tests.unit_tests.runtime.harness.test_opencode_preflight_binding import (
    fixture_session,
)


@pytest_asyncio.fixture
async def model(tmp_path, monkeypatch):
    monkeypatch.setattr(project_store, "get_agent_root_dir", lambda: tmp_path)
    project_store.invalidate_cache()
    project = project_store.create_project("Model fixture", str(tmp_path))
    store = ProjectAccessStore()
    store.initialize(project.project_id, "owner")
    identity = TrustedIdentity("owner", "owner", "fixture-auth")
    binding = ModelCredentialBinding("fixture", "https://model.example/v1")
    store.register_resource(
        project.project_id,
        ResourceDefinition("credential", "credential", binding.reference),
        owner_subject_id="owner",
        actions=("use",),
        expected_revision=0,
    )
    session, harness, context = fixture_session(tmp_path, model=OpenCodeModelConfig(binding.model, binding.api_base))
    state = SimpleNamespace(current=True, requests=[], response=None)
    resolver = Mock(return_value="synthetic-upstream-key")
    use = CredentialUse("credential", binding.reference, "model", binding.destination)
    authority = BoundCredentialAuthority(
        ResourceExecutionContext(
            project.project_id, identity, "parent", str(tmp_path), "opencode"
        ),
        uses=(use,),
        authorizer=store,
        resolver=SimpleNamespace(resolve_credential=resolver),
        current_identity=lambda: identity,
        is_current_execution=lambda: state.current,
    )
    record = OpenCodeModelOperationAuthority(authority, use, lambda: state.current)
    records = {"turn-one": record}

    async def upstream(request):
        state.requests.append(request)
        if state.response is not None:
            return await state.response(request)
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "ordinary-result"}}]}
        )

    from jiuwenswarm.governance import opencode_model_http

    monkeypatch.setattr(
        opencode_model_http.httpx,
        "AsyncHTTPTransport",
        lambda **kw: httpx.MockTransport(upstream),
    )

    async def start(transport):
        transport._port = 12345
        transport._serve_task = asyncio.get_running_loop().create_future()

    monkeypatch.setattr(ManagedProductToolTransport, "start", start)
    session.bind_model_gateway(binding, records.get)
    await session._prepare_tool_context(context)
    transport = session._tool_transport
    harness._context = context
    harness._session_id = "ses_one"
    harness._active_turn = SimpleNamespace(
        turn_id="turn-one", abort_requested=False, stop_requested=False
    )
    harness._preflight.begin(harness.active_turn, "msg_root")

    async def native_request(method, path):
        assert (method, path) == ("GET", "/session/ses_one/message")
        return [
            {
                "info": {
                    "id": "msg_root",
                    "role": "user",
                    "sessionID": "ses_one",
                    "agent": "build",
                    "model": {"providerID": "openjiuwen", "modelID": "fixture"},
                },
                "parts": [],
            }
        ]

    harness._transport = SimpleNamespace(request=native_request)
    session._started = True
    session._exit_state = ExecutionExitState.RUNNING
    app, _ = transport._build_app(12345)
    headers = {
        "Authorization": "Bearer " + transport._token,
        "x-openjiuwen-session": "ses_one",
        "x-openjiuwen-root": "msg_root",
        "x-openjiuwen-generation": transport._preflight_generation,
        "x-openjiuwen-agent": "build",
        "x-openjiuwen-model": "fixture",
        "x-openjiuwen-provider": "openjiuwen",
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:12345"
    ) as client:
        yield SimpleNamespace(
            client=client,
            headers=headers,
            state=state,
            session=session,
            harness=harness,
            transport=transport,
            store=store,
            project=project.project_id,
            identity=identity,
            resolver=resolver,
            authority=authority,
            record=record,
            records=records,
            binding=binding,
            app=app,
        )
    if not transport._serve_task.done():
        transport._serve_task.set_result(None)
    await transport.stop()
    assert transport.exit_confirmed
    project_store.invalidate_cache()


async def request(m, **updates):
    return await m.client.post(
        "/model/v1/chat/completions",
        headers=m.headers,
        json={"model": "fixture", "messages": [], **updates},
    )


@pytest.mark.asyncio
async def test_actual_post_and_retry_resolve_only_original_bound_secret(model):
    for _ in range(2):
        response = await request(model)
        assert response.status_code == 200 and "ordinary-result" in response.text
    assert model.resolver.call_count == 2 and len(model.state.requests) == 2
    for actual in model.state.requests:
        assert str(actual.url) == model.binding.destination and actual.method == "POST"
        assert actual.headers["authorization"] == "Bearer synthetic-upstream-key"
        assert not any(key.startswith("x-openjiuwen-") for key in actual.headers)
        assert model.transport._token not in actual.content.decode()
    assert "synthetic-upstream-key" not in response.text


@pytest.mark.asyncio
async def test_retry_after_real_resource_revocation_does_not_resolve(model):
    assert (await request(model)).status_code == 200
    model.store.revoke_resource(
        model.project,
        model.identity,
        "credential",
        subject_id="owner",
        expected_revision=1,
    )
    assert (await request(model)).status_code == 403
    assert model.resolver.call_count == 1 and len(model.state.requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [("provider_id", "native"), ("session_id", "other"), ("workspace", "/other")],
)
async def test_actual_binding_scope_not_just_model_match(model, field, value):
    model.authority.execution = replace(model.authority.execution, **{field: value})
    assert (await request(model)).status_code == 403
    assert not model.state.requests and not model.resolver.called


@pytest.mark.asyncio
async def test_foreign_subject_denied_before_secret(model):
    model.authority.execution = replace(
        model.authority.execution,
        identity=TrustedIdentity("owner", "other", "fixture-auth"),
    )
    assert (await request(model)).status_code == 403
    assert not model.resolver.called


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["host", "turn", "map", "binding"])
async def test_resolver_await_cannot_borrow_replacement_authority(model, change):
    async def resolve(_):
        await asyncio.sleep(0)
        if change == "host":
            model.state.current = False
        elif change == "turn":
            model.harness._preflight.clear()
        elif change == "map":
            model.records["turn-one"] = replace(model.record)
        else:
            model.authority.execution = replace(
                model.authority.execution, session_id="replacement"
            )
        return "never-send-this-secret"

    model.authority._resolver.resolve_credential = resolve
    response = await request(model)
    assert response.status_code == 403 and "never-send" not in response.text
    assert not model.state.requests


@pytest.mark.asyncio
async def test_response_wait_rechecks_original_turn_and_returns_no_provider_body(model):
    async def upstream(_):
        model.harness._preflight.clear()
        return httpx.Response(200, json={"secret": "never-deliver-model-body"})

    model.state.response = upstream
    response = await request(model)
    assert response.status_code == 403 and "never-deliver" not in response.text


@pytest.mark.asyncio
async def test_stream_rechecks_after_chunk_wait_and_closes_response(model):
    class Stream(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            yield b'data: {"choices":[]}\n\n'
            model.state.current = False
            await asyncio.sleep(0)
            yield b"data: never-deliver\n\n"

        async def aclose(self):
            self.closed = True

    stream = Stream()

    async def upstream(_):
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=stream
        )

    model.state.response = upstream
    sent = []
    messages = [{"type": "http.request", "body": b'{"model":"fixture","stream":true}'}]

    async def receive():
        return messages.pop(0)

    async def send(value):
        sent.append(value)

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/model/v1/chat/completions",
        "headers": [
            (k.encode(), v.encode())
            for k, v in {
                **model.headers,
                "host": "127.0.0.1:12345",
                "content-type": "application/json",
            }.items()
        ],
    }
    with pytest.raises(RuntimeError, match="could not be authorized") as error:
        await model.app(scope, receive, send)
    assert error.value.__context__ is None and stream.closed
    output = b"".join(event.get("body", b"") for event in sent)
    assert b"choices" in output and b"never-deliver" not in output


@pytest.mark.asyncio
async def test_redirect_does_not_follow_or_deliver_untrusted_body(model):
    async def upstream(_):
        return httpx.Response(
            307,
            headers={"location": "https://other.invalid"},
            text="sensitive-upstream",
        )

    model.state.response = upstream
    response = await request(model)
    assert response.status_code == 403 and len(model.state.requests) == 1
    assert "sensitive" not in response.text


@pytest.mark.asyncio
async def test_missing_source_wrong_model_and_duplicate_json_deny(model):
    model.headers.pop("x-openjiuwen-root")
    assert (await request(model)).status_code == 403
    model.headers["x-openjiuwen-root"] = "msg_root"
    assert (await request(model, model="other")).status_code == 403
    response = await model.client.post(
        "/model/v1/chat/completions",
        headers={**model.headers, "content-type": "application/json"},
        content=b'{"model":"other","model":"fixture"}',
    )
    assert response.status_code == 403 and not model.state.requests


@pytest.mark.asyncio
async def test_stop_requires_private_http_client_close_and_can_retry(model):
    consumer = model.transport._model_consumer
    original = consumer.close

    async def fail():
        raise RuntimeError("fixture-close")

    consumer.close = fail
    model.transport._serve_task.set_result(None)
    with pytest.raises(RuntimeError, match="fixture-close"):
        await model.transport.stop()
    assert (
        not model.transport.exit_confirmed and not model.transport._accepting_preflight
    )
    consumer.close = original
    await model.transport.stop()
    assert model.transport.exit_confirmed


@pytest.mark.asyncio
@pytest.mark.parametrize("expired", [False, True])
async def test_adapter_captures_once_before_first_await_without_latest_fallback(
    model, tmp_path, monkeypatch, expired
):
    from unittest.mock import AsyncMock
    from openjiuwen.harness_protocol import HarnessInput
    from jiuwenswarm.common.schema.message import ReqMethod
    from jiuwenswarm.server.runtime.agent_adapter import engine_adapter as module
    from tests.unit_tests.runtime.harness.test_external_execution_route import _route

    route = _route(tmp_path / "adapter", provider_id="opencode")
    factory = Mock(return_value=model.record)
    adapter = module.EngineAgentAdapter(
        route, model_gateway_binding=model.binding, model_authority_factory=factory
    )
    monkeypatch.setattr(module, "get_current_runtime", lambda: None)
    entered, release = asyncio.Event(), asyncio.Event()

    async def ensure(_):
        entered.set()
        await release.wait()

    adapter._ensure_started = ensure

    async def build(**_):
        return HarnessInput(content="fixture")

    monkeypatch.setattr(module, "build_external_input", build)
    adapter._projection = SimpleNamespace(register_turn=lambda *a, **k: None)
    session = SimpleNamespace(
        binding=route.bound.binding,
        closed=False,
        exit_state=ExecutionExitState.RUNNING,
        send=AsyncMock(
            return_value=SimpleNamespace(turn_id="turn-one", message_id="message-one")
        ),
    )
    adapter._session = session
    request_value = SimpleNamespace(
        params={},
        metadata={},
        req_method=ReqMethod.CHAT_SEND,
        session_id=route.bound.binding.host_session_id,
        request_id="request",
        channel_id="web",
    )
    stream = adapter.process_message_stream_impl(request_value, {"query": "ordinary"})
    first = asyncio.create_task(anext(stream))
    await entered.wait()
    assert factory.call_count == 1
    adapter._model_authority_factory = Mock(
        side_effect=AssertionError("must not select new authority")
    )
    if expired:
        model.state.current = False
    release.set()
    if expired:
        with pytest.raises(PermissionError, match="original model"):
            await first
        session.send.assert_not_called()
    else:
        assert (await first).payload["event_type"] == "runtime.accepted"
        assert adapter._turn_model_authorities["turn-one"] is model.record
        await adapter.complete_detached_turn("turn-one")
        assert not adapter._turn_model_authorities
    await stream.aclose()
    assert factory.call_count == 1 and not adapter._model_authority_factory.called


def test_adapter_factory_error_and_cancellation_are_secret_free(model, tmp_path):
    # This synchronous test's model fixture supplies a real BoundCredentialAuthority.
    from jiuwenswarm.server.runtime.agent_adapter.engine_adapter import (
        EngineAgentAdapter,
    )
    from tests.unit_tests.runtime.harness.test_external_execution_route import _route

    route = _route(tmp_path / "adapter", provider_id="opencode")
    for error in (
        RuntimeError("synthetic-secret-error"),
        asyncio.CancelledError("synthetic-secret-cancel"),
    ):
        adapter = EngineAgentAdapter(
            route,
            model_gateway_binding=model.binding,
            model_authority_factory=Mock(side_effect=error),
        )
        with pytest.raises((PermissionError, asyncio.CancelledError)) as raised:
            adapter._capture_model_authority()
        assert (
            "synthetic-secret" not in str(raised.value)
            and raised.value.__context__ is None
        )


@pytest.mark.asyncio
async def test_session_gateway_cannot_rebind_or_run_without_mandatory_tool_scope(model):
    with pytest.raises(ValueError, match="binding is unavailable"):
        model.session.bind_model_gateway(model.binding, model.records.get)
    fresh, _, context = fixture_session(
        model.session.runtime_paths.runtime_workspace_root
    )
    fresh.bind_model_gateway(model.binding, model.records.get)
    with pytest.raises(ValueError, match="mandatory native authority"):
        await fresh._prepare_tool_context(replace(context, tool_authorizer=None))
    assert fresh._tool_transport is None


@pytest.mark.asyncio
async def test_stream_success_and_private_http_settings(model):
    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"choices":[]}\n\n'
            yield b"data: [DONE]\n\n"

    async def upstream(_):
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=Stream()
        )

    model.state.response = upstream
    response = await request(model, stream=True)
    assert response.status_code == 200 and "[DONE]" in response.text
    client = model.transport._model_consumer._client
    assert not client.trust_env and not client.follow_redirects
    assert "authorization" not in client.headers


@pytest.mark.asyncio
async def test_real_openai_sdk_retry_reenters_original_http_authority(model):
    from openai import AsyncOpenAI

    count = 0

    async def upstream(_):
        nonlocal count
        count += 1
        if count == 1:
            return httpx.Response(
                503,
                json={"error": {"message": "must-not-forward-private-provider-error"}},
            )
        return httpx.Response(
            200,
            json={
                "id": "fixture",
                "object": "chat.completion",
                "created": 0,
                "model": "fixture",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "sdk-result"},
                        "finish_reason": "stop",
                    }
                ],
            },
        )

    model.state.response = upstream
    client = AsyncOpenAI(
        api_key=model.transport._token,
        base_url="http://127.0.0.1:12345/model/v1",
        http_client=model.client,
        default_headers=model.headers,
        max_retries=1,
    )
    result = await client.chat.completions.create(
        model="fixture", messages=[{"role": "user", "content": "ordinary"}]
    )
    assert result.choices[0].message.content == "sdk-result"
    assert count == 2 and model.resolver.call_count == 2
    assert all(
        request.headers["authorization"] == "Bearer synthetic-upstream-key"
        for request in model.state.requests
    )


@pytest.mark.asyncio
async def test_original_scope_rechecked_after_synchronous_policy_callback(
    model, monkeypatch
):
    authorize = model.store.authorize_resource

    def mutate(*args):
        result = authorize(*args)
        model.authority.execution = replace(
            model.authority.execution, session_id="changed-in-policy"
        )
        return result

    monkeypatch.setattr(model.store, "authorize_resource", mutate)
    assert (await request(model)).status_code == 403
    assert not model.resolver.called and not model.state.requests


@pytest.mark.asyncio
@pytest.mark.parametrize("cancelled", [False, True])
async def test_resolver_errors_do_not_escape_http_consumer(model, cancelled):
    error = (
        asyncio.CancelledError("synthetic-secret-marker")
        if cancelled
        else RuntimeError("synthetic-secret-marker")
    )
    model.resolver.side_effect = error
    if cancelled:
        with pytest.raises(asyncio.CancelledError) as raised:
            await request(model)
        assert not str(raised.value) and raised.value.__context__ is None
    else:
        response = await request(model)
        assert (
            response.status_code == 403
            and "synthetic-secret-marker" not in response.text
        )
    assert not model.state.requests


@pytest.mark.asyncio
async def test_real_openai_sdk_stream_consumes_only_authorized_chunks(model):
    from openai import AsyncOpenAI

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            chunk = {
                "id": "fixture",
                "object": "chat.completion.chunk",
                "created": 0,
                "model": "fixture",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": "sdk-stream"},
                        "finish_reason": None,
                    }
                ],
            }
            yield ("data: " + json.dumps(chunk) + "\n\n").encode()
            yield b"data: [DONE]\n\n"

    async def upstream(_):
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=Stream()
        )

    model.state.response = upstream
    client = AsyncOpenAI(
        api_key=model.transport._token,
        base_url="http://127.0.0.1:12345/model/v1",
        http_client=model.client,
        default_headers=model.headers,
        max_retries=0,
    )
    stream = await client.chat.completions.create(
        model="fixture", messages=[], stream=True
    )
    values = [chunk.choices[0].delta.content async for chunk in stream]
    assert values == ["sdk-stream"] and model.resolver.call_count == 1


@pytest.mark.asyncio
async def test_httpx_closed_flag_is_not_a_successful_transport_close_receipt(
    model, monkeypatch
):
    consumer = model.transport._model_consumer
    calls = 0

    async def close_transport():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("synthetic-partial-close")

    monkeypatch.setattr(consumer._http_transport, "aclose", close_transport)
    model.transport._serve_task.set_result(None)
    with pytest.raises(RuntimeError, match="partial-close"):
        await model.transport.stop()
    assert consumer._client.is_closed and not consumer.closed
    assert not model.transport.exit_confirmed
    await model.transport.stop()
    assert calls == 2 and consumer.closed and model.transport.exit_confirmed


@pytest.mark.asyncio
async def test_original_uvicorn_request_registry_must_drain_before_exit(
    model, monkeypatch
):
    from jiuwenswarm.runtime.harness import tool_transport

    release = asyncio.Event()
    task = asyncio.create_task(release.wait())
    server = SimpleNamespace(
        should_exit=False, server_state=SimpleNamespace(tasks={task})
    )
    model.transport._uvicorn = server
    model.transport._serve_task.set_result(None)
    monkeypatch.setattr(tool_transport, "_STOP_TIMEOUT_S", 0.01)
    try:
        with pytest.raises((RuntimeError, TimeoutError)):
            await model.transport.stop()
        assert model.transport._uvicorn is server and not model.transport.exit_confirmed
        assert model.transport._model_consumer.closed and not task.done()
        release.set()
        await task
        await model.transport.stop()
        assert model.transport.exit_confirmed
    finally:
        release.set()
        await task
