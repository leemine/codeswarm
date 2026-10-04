"""Explicit catalog and actual MCP SDK consumption under real resource policy."""

import copy
import json
from types import SimpleNamespace

import pytest
from openjiuwen.core.foundation.llm import ToolCall
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext

from jiuwenswarm.governance.mcp_credentials import (
    ConfiguredMcpCredentialResolver,
    NativeMcpCredentialAuthority,
    configured_native_mcp_catalog,
)
from jiuwenswarm.governance.resources import ResourceAccessDenied, ResourceDefinition
from jiuwenswarm.governance.tool_context import _NATIVE_SLICE, tool_authority_scope
from jiuwenswarm.server.runtime.mcp.native_registration import install_native_mcp_tools
from tests.unit_tests.agentserver.mcp import (
    test_native_registration as registration_fixtures,
)

native = registration_fixtures.native
SCHEMA = registration_fixtures.SCHEMA


def raw_config(project):
    return {
        "mcp": {
            "servers": [
                {
                    "name": "declared",
                    "enabled": True,
                    "transport": "streamable-http",
                    "url": "https://mcp.invalid/rpc",
                    "headers": {"Authorization": "Bearer synthetic-bob-token"},
                    "organization": {
                        "project_id": project,
                        "revision": "r1",
                        "credential_resource_id": "credential",
                        "credential_reference": "mcp:bob",
                        "credential_encoding": "plain",
                        "tools": [
                            {
                                "remote_name": "echo",
                                "description": "Host-declared text echo",
                                "input_schema": copy.deepcopy(SCHEMA),
                                "resource_id": "catalog-text",
                                "contract": "stateless-text-v1",
                            }
                        ],
                    },
                }
            ]
        }
    }


@pytest.fixture
async def catalog_native(native):
    n = native
    raw = raw_config(n.pid)

    def source():
        return raw

    (entry,) = configured_native_mcp_catalog(n.pid, config_source=source)
    n.store.register_resource(
        n.pid,
        ResourceDefinition("catalog-text", "tool", "mcp:declared:echo"),
        owner_subject_id="bob",
        actions=("invoke",),
        expected_revision=2,
    )
    registration = install_native_mcp_tools(
        **{**n.kwargs, "connection": entry.binding, "manifest": entry.manifest}
    )
    record = registration.records[0]
    authority = NativeMcpCredentialAuthority(
        n.execution,
        resource_authorizer=n.store,
        current_identity=lambda: n.identity,
        is_current_execution=lambda: n.current,
        owns_execution=lambda execution, owner: (
            execution == n.execution and owner is n.owner
        ),
        config_source=source,
    )
    n.bound.mcp_authorizer = lambda binding, **kw: authority(
        binding, **kw, is_current_host_request=lambda: n.current
    )

    async def invoke(text="hello"):
        token = _NATIVE_SLICE.set(n.bound)
        try:
            with tool_authority_scope(n.early):
                return await n.manager.execute(
                    AgentCallbackContext(agent=n.agent),
                    ToolCall(
                        id="catalog-call",
                        type="function",
                        name=record.alias,
                        arguments=json.dumps({"text": text}),
                    ),
                    session=n.session,
                )
        finally:
            _NATIVE_SLICE.reset(token)

    yield SimpleNamespace(**locals())
    registration.close()


def test_catalog_is_metadata_only_and_does_not_read_legacy_credentials():
    raw = raw_config("project")
    raw["mcp"]["servers"].append(
        {"name": "legacy", "headers": {"Authorization": object()}}
    )
    raw["mcp"]["servers"][0]["headers"]["Authorization"] = object()
    (entry,) = configured_native_mcp_catalog("project", config_source=lambda: raw)
    assert entry.binding.credential_use.reference == "mcp:bob"
    assert "Authorization" not in repr(entry)
    assert configured_native_mcp_catalog("other", config_source=lambda: raw) == ()
    with pytest.raises(ResourceAccessDenied):
        ConfiguredMcpCredentialResolver(
            entry, config_source=lambda: raw
        ).resolve_credential("mcp:bob")


@pytest.mark.parametrize(
    "change",
    [
        "duplicate",
        "transport",
        "headers",
        "env",
        "contract",
        "schema",
        "reference",
        "url",
    ],
)
def test_unsupported_or_ambiguous_selected_catalog_denies(change):
    raw = raw_config("project")
    row = raw["mcp"]["servers"][0]
    if change == "duplicate":
        raw["mcp"]["servers"].append(copy.deepcopy(row))
    elif change == "transport":
        row["transport"] = "stdio"
    elif change == "headers":
        row["headers"]["X-Token"] = "hidden"
    elif change == "env":
        row["env"] = {"TOKEN": "hidden"}
    elif change == "contract":
        row["organization"]["tools"][0]["contract"] = "shell"
    elif change == "schema":
        row["organization"]["tools"][0]["input_schema"]["properties"]["path"] = {
            "type": "string"
        }
    elif change == "reference":
        row["organization"]["credential_reference"] = ""
    else:
        row["url"] = "https://mcp.invalid/rpc?token=hidden"
    with pytest.raises((ResourceAccessDenied, ValueError)):
        configured_native_mcp_catalog("project", config_source=lambda: raw)


@pytest.mark.parametrize(
    "secret",
    [
        "Bearer ${ENV}",
        "Basic abc",
        "Bearer ",
        "Bearer a\nb",
        "Bearer jiuwen-login:token",
    ],
)
def test_no_placeholder_or_login_credential_fallback(secret):
    raw = raw_config("project")
    row = raw["mcp"]["servers"][0]
    row["headers"]["Authorization"] = secret
    (entry,) = configured_native_mcp_catalog("project", config_source=lambda: raw)
    with pytest.raises(ResourceAccessDenied):
        ConfiguredMcpCredentialResolver(
            entry, config_source=lambda: raw
        ).resolve_credential("mcp:bob")


def test_only_selected_instance_decoder_can_resolve_explicit_encryption():
    raw = raw_config("project")
    row = raw["mcp"]["servers"][0]
    row["organization"]["credential_encoding"] = "host_crypto"
    row["headers"]["Authorization"] = "Bearer CIPHERTEXT"
    (entry,) = configured_native_mcp_catalog("project", config_source=lambda: raw)
    calls = []

    def decoder(value):
        calls.append(value)
        return "synthetic-decoded"

    with pytest.raises(ResourceAccessDenied):
        ConfiguredMcpCredentialResolver(
            entry, config_source=lambda: raw
        ).resolve_credential("mcp:bob")
    assert (
        ConfiguredMcpCredentialResolver(
            entry, config_source=lambda: raw, credential_decoder=decoder
        ).resolve_credential("mcp:bob")
        == "synthetic-decoded"
    )
    assert calls == ["CIPHERTEXT"]


@pytest.mark.asyncio
async def test_actual_catalog_factory_and_sdk_consume_bound_credential(catalog_native):
    c = catalog_native
    result = await c.invoke("actual catalog text")
    assert "actual catalog text" in str(result)
    assert [m["method"] for m in c.n.sent] == [
        "initialize",
        "notifications/initialized",
        "tools/call",
        "tools/list",
    ]
    assert c.n.closed == 1


@pytest.mark.asyncio
async def test_catalog_change_before_method_blocks_without_network(catalog_native):
    c = catalog_native
    c.raw["mcp"]["servers"][0]["organization"]["revision"] = "changed"
    result = await c.invoke()
    assert "denied" in str(result).lower() or "unavailable" in str(result).lower()
    assert c.n.sent == []


@pytest.mark.asyncio
async def test_catalog_change_after_initial_http_prevents_next_consumption(
    catalog_native, monkeypatch
):
    from jiuwenswarm.server.runtime.mcp import governed_http

    c = catalog_native
    original = governed_http.httpx.AsyncHTTPTransport

    def factory(**kw):
        transport = original(**kw)
        send = transport.handle_async_request

        async def changed(request):
            result = await send(request)
            c.raw["mcp"]["servers"][0]["organization"]["revision"] = "changed"
            return result

        transport.handle_async_request = changed
        return transport

    monkeypatch.setattr(governed_http.httpx, "AsyncHTTPTransport", factory)
    result = await c.invoke()
    assert "denied" in str(result).lower()
    assert [m["method"] for m in c.n.sent] == ["initialize"]
    assert c.n.closed == 1
