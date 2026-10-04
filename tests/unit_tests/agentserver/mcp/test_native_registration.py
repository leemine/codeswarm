"""Actual AbilityManager/MCPTool/SDK chain with isolated real resource grants."""

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest
from mcp import types
from openjiuwen.core.foundation.llm import ToolCall
from openjiuwen.core.foundation.tool import current_tool_execution
from openjiuwen.core.foundation.tool.mcp.base import MCPTool
from openjiuwen.core.runner import Runner
from openjiuwen.core.runner.callback.events import ToolCallEvents
from openjiuwen.core.single_agent.ability_manager import AbilityManager
from openjiuwen.core.single_agent.rail.base import (
    AgentCallbackContext,
    AgentCallbackEvent,
)
from openjiuwen.harness.engine import ExecutionBinding

from jiuwenswarm.agents.harness.common.rails.permissions.resource_authority_rail import (
    NativeResourceAuthorityRail,
)
from jiuwenswarm.governance.contracts import TrustedIdentity
from jiuwenswarm.governance.credential_resources import (
    BoundCredentialAuthority,
    CredentialUse,
)
from jiuwenswarm.governance.native_mcp_tools import (
    McpOperationAuthority,
    NativeMcpToolSpec,
    actual_mcp_resources,
    native_mcp_resources,
)
from jiuwenswarm.governance.resources import ResourceDefinition, ResourceGuard
from jiuwenswarm.governance.tool_context import (
    NativeExecutionSlice,
    _NATIVE_SLICE,
    tool_authority_scope,
)
from jiuwenswarm.governance.tool_resources import (
    BoundToolResourceAuthority,
    ResourceExecutionContext,
)
from jiuwenswarm.server.runtime.mcp import governed_http
from jiuwenswarm.server.runtime.mcp.native_registration import install_native_mcp_tools
from jiuwenswarm.server.runtime.session import project_store
from jiuwenswarm.server.runtime.session.project_access import ProjectAccessStore

SCHEMA = {
    "type": "object",
    "properties": {"text": {"type": "string"}},
    "required": ["text"],
    "additionalProperties": False,
}


@pytest.fixture
def native(tmp_path, monkeypatch):
    monkeypatch.setenv("JIUWENSWARM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(
        project_store, "get_agent_root_dir", lambda: tmp_path / "projects"
    )
    project_store.invalidate_cache()
    work = tmp_path / "work"
    work.mkdir()
    pid = project_store.create_project("MCP fixture", str(work)).project_id
    store = ProjectAccessStore()
    store.initialize(pid, "bob")
    identity = TrustedIdentity("bob", "bob", "fixture")
    tool_resource = ResourceDefinition("text-tool", "tool", "mcp:declared-text")
    credential = CredentialUse(
        "credential", "mcp:bob", "mcp", "https://mcp.invalid/rpc"
    )
    for revision, resource, actions in (
        (0, tool_resource, ("invoke",)),
        (
            1,
            ResourceDefinition("credential", "credential", credential.reference),
            ("use",),
        ),
    ):
        store.register_resource(
            pid,
            resource,
            owner_subject_id="bob",
            actions=actions,
            expected_revision=revision,
        )
    binding = ExecutionBinding("bob", "sid", str(work), "native", "1", "0" * 64)
    session = SimpleNamespace(
        get_session_id=lambda: "sid", get_state=lambda *_a, **_kw: None
    )
    manager = AbilityManager(owner_id="mcp-" + tmp_path.name)
    rail = NativeResourceAuthorityRail()

    class Callbacks:
        async def execute(self, event, ctx):
            if event is AgentCallbackEvent.BEFORE_TOOL_CALL:
                await rail.before_tool_call(ctx)

    agent = SimpleNamespace(
        card=SimpleNamespace(id="agent", name="agent"),
        ability_manager=manager,
        agent_callback_manager=Callbacks(),
    )
    execution = ResourceExecutionContext(pid, identity, "sid", str(work), "native")
    owner = SimpleNamespace(engine=SimpleNamespace(binding=binding))
    owner.owns_tool_session = lambda ex, ag, se: (
        ex == execution and ag is agent and se is session
    )
    connection = governed_http.McpHttpBinding(
        "declared", credential.destination, "revision1", credential
    )
    spec = NativeMcpToolSpec(
        "echo", "Host-declared stateless text operation", SCHEMA, tool_resource
    )
    kwargs = dict(
        project_id=pid,
        agent=agent,
        session=session,
        native_session=owner,
        execution_binding=binding,
        connection=connection,
        manifest=(spec,),
    )
    registration = install_native_mcp_tools(**kwargs)
    record = registration.records[0]
    state = SimpleNamespace(
        sent=[],
        captures=[],
        checks=[],
        child_captures=[],
        resolved=0,
        closed=0,
        current=True,
        factory_hook=None,
        resolver_hook=None,
    )
    resource_guard = ResourceGuard(store)
    early = BoundToolResourceAuthority(
        execution,
        authorizer=store,
        resolver=SimpleNamespace(resources_for_tool=native_mcp_resources),
        current_identity=lambda: identity,
        is_current_execution=lambda: state.current,
    )

    def factory(connection, **call):
        state.captures.append(call)
        source = call["source_execution"]
        operation = call["actual_operation"]
        bound = call["execution_slice"]
        assert current_tool_execution() is source
        if state.factory_hook:
            state.factory_hook(call)

        def current():
            return state.current and bound.active and source.is_current_origin()

        async def resolve(reference):
            assert reference == credential.reference
            state.resolved += 1
            if state.resolver_hook:
                await state.resolver_hook()
            return "synthetic-bob-token"

        authority = BoundCredentialAuthority(
            execution,
            uses=(credential,),
            authorizer=store,
            resolver=SimpleNamespace(resolve_credential=resolve),
            current_identity=lambda: identity,
            is_current_execution=current,
        )

        def admit(target):
            state.child_captures.append(
                (asyncio.current_task() is source.owning_task, current_tool_execution())
            )
            uses = actual_mcp_resources(
                execution,
                operation,
                executor_binding=call["executor_binding"],
                source_execution=source,
            )
            decisions = tuple(
                resource_guard.check(pid, identity, use.request) for use in uses
            )
            assert tuple(d.reference for d in decisions) == tuple(
                u.reference for u in uses
            )
            state.checks.append((target, decisions))
            return current()

        return McpOperationAuthority(authority, admit, current)

    # Base fixture predates the independently integrated mcp_authorizer field.
    # It still uses the real Native slice ContextVar; no authorization is mocked.
    class FixtureSlice(NativeExecutionSlice):
        pass

    bound = FixtureSlice(owner, early, None, None)
    bound.mcp_authorizer = factory

    async def handler(request):
        message = json.loads(request.content)
        assert request.headers["authorization"] == "Bearer synthetic-bob-token"
        state.sent.append(message)
        method = message["method"]
        if method == "notifications/initialized":
            return httpx.Response(202)
        if method == "initialize":
            result = {
                "protocolVersion": types.LATEST_PROTOCOL_VERSION,
                "capabilities": {},
                "serverInfo": {"name": "synthetic", "version": "1"},
            }
        elif method == "tools/list":
            result = {"tools": [{"name": "echo", "inputSchema": SCHEMA}]}
        else:
            assert message["params"]["name"] == "echo"
            result = {
                "content": [
                    {"type": "text", "text": message["params"]["arguments"]["text"]}
                ],
                "isError": False,
            }
        return httpx.Response(
            200, json={"jsonrpc": "2.0", "id": message["id"], "result": result}
        )

    class Transport(httpx.MockTransport):
        async def aclose(self):
            state.closed += 1
            await super().aclose()

    monkeypatch.setattr(
        governed_http.httpx, "AsyncHTTPTransport", lambda **_kwargs: Transport(handler)
    )

    async def invoke(args=None):
        token = _NATIVE_SLICE.set(bound)
        try:
            with tool_authority_scope(early):
                return await manager.execute(
                    AgentCallbackContext(agent=agent),
                    ToolCall(
                        id="actual-call",
                        type="function",
                        name=record.alias,
                        arguments=json.dumps(args or {"text": "hello"}),
                    ),
                    session=session,
                )
        finally:
            _NATIVE_SLICE.reset(token)

    state.__dict__.update(locals())
    yield state
    registration.close()
    manager.teardown_tools()
    project_store.invalidate_cache()


@pytest.mark.asyncio
async def test_actual_tool_parser_sdk_and_both_resource_grants(native):
    phases = []

    async def parsed(formatted_inputs, **_kwargs):
        phases.append(dict(formatted_inputs))
        formatted_inputs["text"] = "post-parse"

    await Runner.callback_framework.register(ToolCallEvents.TOOL_PARSE_FINISHED, parsed)
    try:
        result = await native.invoke()
    finally:
        await Runner.callback_framework.unregister(
            ToolCallEvents.TOOL_PARSE_FINISHED, parsed
        )
    assert "post-parse" in str(result), result
    assert phases == [{"text": "hello"}]
    assert [message["method"] for message in native.sent] == [
        "initialize",
        "notifications/initialized",
        "tools/call",
        "tools/list",
    ]
    call = native.captures[0]
    assert call["source_execution"].operation.arguments["text"] == "hello"
    assert call["actual_operation"].arguments["text"] == "post-parse"
    assert call["source_execution"].is_current_origin() is False
    assert len(native.captures) == 1 and native.closed == 1 and native.resolved == 4
    assert any(not original for original, _ in native.child_captures)
    assert all(
        value is None for original, value in native.child_captures if not original
    )
    assert all(
        {d.request.action for d in decisions} == {"invoke", "use"}
        for _, decisions in native.checks
    )
    assert "synthetic-bob-token" not in str(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("resource_id", ["text-tool", "credential"])
async def test_either_missing_grant_denies_before_network(native, resource_id):
    native.store.revoke_resource(
        native.pid, native.identity, resource_id, subject_id="bob", expected_revision=2
    )
    result = await native.invoke()
    assert "PERMISSION_DENIED" in str(result)
    assert not native.sent and not native.captures


@pytest.mark.asyncio
async def test_direct_client_has_no_tool_certificate(native):
    with pytest.raises(PermissionError, match="Native MCP execution denied"):
        await native.record.client.call_tool(native.record.alias, {"text": "hello"})
    assert not native.sent


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    ["schema", "card", "registry", "client", "binding", "owner", "slice", "subject"],
)
async def test_changed_owned_objects_fail_closed(native, change):
    if change == "schema":
        native.record.card.input_params["properties"]["text"]["type"] = "integer"
    elif change == "card":
        native.record.executor._card = native.record.card.model_copy(deep=True)
    elif change == "registry":
        Runner.resource_mgr.remove_tool(native.record.registry_id)
    elif change == "client":
        native.record.executor._mcp_client = object()
    elif change == "binding":
        native.owner.engine.binding = replace(native.binding)
    elif change == "owner":
        native.owner.owns_tool_session = lambda *_: False
    elif change == "slice":
        native.bound.active = False
    else:
        native.bound.subject = object()
    result = await native.invoke()
    assert not native.sent, result


@pytest.mark.asyncio
async def test_parse_extra_resource_parameters_denied(native):
    async def parsed(formatted_inputs, **_kwargs):
        formatted_inputs["destination"] = "/unrelated"

    await Runner.callback_framework.register(ToolCallEvents.TOOL_PARSE_FINISHED, parsed)
    try:
        result = await native.invoke()
    finally:
        await Runner.callback_framework.unregister(
            ToolCallEvents.TOOL_PARSE_FINISHED, parsed
        )
    assert not native.sent and not native.captures
    assert "Native MCP execution denied" in str(result)


def test_schema_deep_snapshot_and_unsupported_contract(native):
    schema = json.loads(json.dumps(SCHEMA))
    spec = NativeMcpToolSpec("echo", "", schema, native.tool_resource)
    schema["properties"]["text"]["type"] = "integer"
    assert spec.input_schema["properties"]["text"]["type"] == "string"
    with pytest.raises(TypeError):
        spec.input_schema["properties"]["text"]["type"] = "integer"
    with pytest.raises(ValueError):
        replace(spec, contract="arbitrary-mcp")


def test_separate_installation_alias_and_exact_cleanup(native):
    second = install_native_mcp_tools(**native.kwargs)
    other = second.records[0]
    assert (
        other.alias != native.record.alias and other.client is not native.record.client
    )
    native.registration.close()
    assert other.is_current()
    second.close()
    assert not other.is_current()


def test_cleanup_preserves_foreign_registry_slot(native):
    card = native.record.card.model_copy(deep=True)
    foreign = MCPTool(object(), card)
    Runner.resource_mgr.add_tool(foreign, refresh=True)
    native.registration.close()
    assert (
        Runner.resource_mgr.get_tool(native.record.registry_id, session=None) is foreign
    )
    Runner.resource_mgr.remove_tool(native.record.registry_id)


@pytest.mark.asyncio
async def test_expired_certificate_and_child_capture_rejected(native):
    await native.invoke()
    captured = native.captures[0]
    with pytest.raises(PermissionError):
        actual_mcp_resources(
            native.execution,
            captured["actual_operation"],
            executor_binding=native.record,
            source_execution=captured["source_execution"],
        )

    async def direct_child():
        assert current_tool_execution() is None
        await native.record.client.call_tool(native.record.alias, {"text": "hello"})

    with pytest.raises(PermissionError):
        await asyncio.create_task(direct_child())
    assert len(native.sent) == 4


@pytest.mark.asyncio
async def test_foreign_execution_context_cannot_map_captured_certificate(native):
    denied = []

    def check(call):
        for changes in (
            {"project_id": "other"},
            {"session_id": "other"},
            {"workspace": str(native.work.parent)},
            {"provider_id": "opencode"},
            {"identity": TrustedIdentity("other", "other", "fixture")},
        ):
            with pytest.raises(PermissionError):
                actual_mcp_resources(
                    replace(native.execution, **changes),
                    call["actual_operation"],
                    executor_binding=native.record,
                    source_execution=call["source_execution"],
                )
            denied.append(changes)

    native.factory_hook = check
    result = await native.invoke()
    assert "hello" in str(result) and len(denied) == 5


@pytest.mark.asyncio
async def test_factory_failure_is_secret_free(native, caplog):
    def fail(_call):
        raise ValueError("synthetic-private-diagnostic")

    native.factory_hook = fail
    result = await native.invoke()
    assert "Native MCP execution denied" in str(result)
    assert "synthetic-private-diagnostic" not in str(result) + caplog.text
    assert not native.sent


@pytest.mark.asyncio
async def test_captured_request_lifetime_rechecked_after_credential_wait(native):
    async def invalidate():
        native.bound.active = False

    native.resolver_hook = invalidate
    result = await native.invoke()
    assert not native.sent
    assert "hello" not in str(result)
    assert native.closed == 1


@pytest.mark.asyncio
async def test_factory_cannot_replace_installed_card(native):
    native.factory_hook = lambda _call: setattr(
        native.record.card, "description", "changed"
    )
    result = await native.invoke()
    assert not native.sent and "Native MCP execution denied" in str(result)


def test_partial_installation_rolls_back_owned_slots(native, monkeypatch):
    original = native.manager.add_ability
    installed = []

    def fail(card, executor):
        installed.append((card.name, card.id))
        original(card, executor)
        raise RuntimeError("synthetic-install-failure")

    monkeypatch.setattr(native.manager, "add_ability", fail)
    with pytest.raises(RuntimeError, match="synthetic-install-failure"):
        install_native_mcp_tools(**native.kwargs)
    assert len(installed) == 1
    alias, slot = installed[0]
    assert native.manager.get(alias) is None
    assert Runner.resource_mgr.get_tool(slot, session=None) is None
    assert native.record.is_current()


def test_registration_never_overwrites_collision(native, monkeypatch):
    from jiuwenswarm.server.runtime.mcp import native_registration

    nonce = native.record.alias.split("_")[1] + "0" * 16
    monkeypatch.setattr(
        native_registration.uuid, "uuid4", lambda: SimpleNamespace(hex=nonce)
    )
    with pytest.raises(ValueError, match="slot already occupied"):
        install_native_mcp_tools(**native.kwargs)
    assert native.record.is_current()


@pytest.mark.asyncio
async def test_parse_child_cannot_capture_or_call_private_client(native):
    outcomes = []

    async def parsed(**_kwargs):
        async def child():
            assert current_tool_execution() is None
            with pytest.raises(PermissionError):
                await native.record.client.call_tool(
                    native.record.alias, {"text": "child"}
                )
            outcomes.append("denied")

        await asyncio.create_task(child())

    await Runner.callback_framework.register(ToolCallEvents.TOOL_PARSE_FINISHED, parsed)
    try:
        result = await native.invoke()
    finally:
        await Runner.callback_framework.unregister(
            ToolCallEvents.TOOL_PARSE_FINISHED, parsed
        )
    assert "hello" in str(result) and outcomes == ["denied"]
    assert len(native.captures) == 1
