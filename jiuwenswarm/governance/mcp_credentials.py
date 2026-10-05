"""Explicit per-project MCP catalog, resolved only at actual authorized IO.

This uses existing mcp.servers configuration and ResourceGuard. It never reads
legacy state.json, CredentialStore, environment placeholders or global clients.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from .credential_resources import BoundCredentialAuthority, CredentialUse
from .resources import ResourceAccessDenied, ResourceDefinition
from .tool_resources import BoundToolResourceAuthority
from jiuwenswarm.server.runtime.mcp.governed_http import McpHttpBinding


def _deny():
    raise ResourceAccessDenied("configured MCP resource unavailable")


@dataclass(frozen=True, slots=True)
class NativeMcpCatalogEntry:
    project_id: str
    binding: McpHttpBinding
    manifest: tuple
    credential_encoding: str


def _raw_entries(source):
    if source is None:
        from jiuwenswarm.common.config import get_config_raw

        source = get_config_raw
    raw = source()
    if not isinstance(raw, dict) or not isinstance(raw.get("mcp", {}), dict):
        _deny()
    rows = raw.get("mcp", {}).get("servers", [])
    if not isinstance(rows, list):
        _deny()
    return rows


def _entry(row):
    from .native_mcp_tools import NativeMcpToolSpec

    if (
        type(row) is not dict
        or set(row) - {"name", "enabled", "transport", "url", "headers", "organization"}
        or row.get("enabled", True) is not True
        or row.get("transport") != "streamable-http"
        or type(row.get("headers")) is not dict
        or set(row["headers"]) != {"Authorization"}
    ):
        _deny()
    org = row.get("organization")
    if type(org) is not dict or set(org) != {
        "project_id",
        "revision",
        "credential_resource_id",
        "credential_reference",
        "credential_encoding",
        "tools",
    }:
        _deny()
    for value in (row.get("name"), org["project_id"], org["revision"]):
        if (
            type(value) is not str
            or not value
            or value != value.strip()
            or "${" in value
        ):
            _deny()
    if (
        org["credential_encoding"] not in {"plain", "host_crypto"}
        or type(org["tools"]) is not list
        or not org["tools"]
    ):
        _deny()
    specs = []
    for tool in org["tools"]:
        if type(tool) is not dict or set(tool) != {
            "remote_name",
            "description",
            "input_schema",
            "resource_id",
            "contract",
        }:
            _deny()
        ref = f"mcp:{row['name']}:{tool['remote_name']}"
        specs.append(
            NativeMcpToolSpec(
                tool["remote_name"],
                tool["description"],
                tool["input_schema"],
                ResourceDefinition(tool["resource_id"], "tool", ref),
                tool["contract"],
            )
        )
    if len({spec.remote_name for spec in specs}) != len(specs):
        _deny()
    # No header value enters the catalog checksum or any returned object.
    metadata = {key: row[key] for key in row if key != "headers"}
    material = json.dumps(
        metadata, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    if "${" in material:
        _deny()
    revision = hashlib.sha256(material.encode()).hexdigest()
    use = CredentialUse(
        org["credential_resource_id"],
        org["credential_reference"],
        "mcp",
        row.get("url"),
    )
    return NativeMcpCatalogEntry(
        org["project_id"],
        McpHttpBinding(row["name"], row["url"], revision, use),
        tuple(specs),
        org["credential_encoding"],
    )


def configured_native_mcp_catalog(project_id, *, config_source=None):
    """Metadata-only explicit catalog; selected malformed/duplicate rows deny."""
    entries = []
    for row in _raw_entries(config_source):
        if type(row) is not dict or "organization" not in row:
            continue  # Legacy entries are neither resolved nor consumed.
        org = row["organization"]
        if type(org) is not dict:
            _deny()
        if org.get("project_id") != project_id or row.get("enabled", True) is False:
            continue
        entries.append(_entry(row))
    if len({item.binding.connection_id for item in entries}) != len(entries):
        _deny()
    return tuple(entries)


class ConfiguredMcpCredentialResolver:
    def __init__(self, entry, *, config_source=None, credential_decoder=None):
        self.entry = entry
        self._source = config_source
        self._decoder = credential_decoder

    def resolve_credential(self, reference):
        if reference != self.entry.binding.credential_use.reference:
            _deny()
        matches = []
        for row in _raw_entries(self._source):
            if (
                type(row) is dict
                and row.get("name") == self.entry.binding.connection_id
                and type(row.get("organization")) is dict
                and row["organization"].get("project_id") == self.entry.project_id
            ):
                if _entry(row) != self.entry:
                    _deny()
                matches.append(row)
        if len(matches) != 1:
            _deny()
        value = matches[0]["headers"]["Authorization"]
        if type(value) is not str or not value.startswith("Bearer "):
            _deny()
        secret = value[len("Bearer ") :]
        if not secret or "${" in secret or "\n" in secret or "\r" in secret:
            _deny()
        if self.entry.credential_encoding == "host_crypto":
            if self._decoder is None:
                _deny()
            secret = self._decoder(secret)
        if (
            type(secret) is not str
            or not secret
            or "${" in secret
            or "\n" in secret
            or "\r" in secret
            or secret.startswith("jiuwen-login:")
        ):
            _deny()
        return secret


class NativeMcpCredentialAuthority:
    """One Runtime submission; fixed host request is added by NativeSession."""

    def __init__(
        self,
        execution,
        *,
        resource_authorizer,
        current_identity,
        is_current_execution,
        owns_execution,
        config_source=None,
        credential_decoder=None,
    ):
        self.execution = execution
        self._resources = resource_authorizer
        self._identity = current_identity
        self._current = is_current_execution
        self._owns = owns_execution
        self._source = config_source
        self._decoder = credential_decoder

    def __call__(
        self,
        binding,
        *,
        executor_binding,
        actual_operation,
        source_execution,
        execution_slice,
        native_session,
        is_current_host_request,
    ):
        from types import SimpleNamespace
        from .native_mcp_tools import McpOperationAuthority, actual_mcp_resources
        from .tool_context import current_native_execution_slice

        try:
            if (
                type(binding) is not McpHttpBinding
                or executor_binding.connection != binding
                or executor_binding.native_session is not native_session
                or self.execution.provider_id != "native"
            ):
                _deny()
            catalog = configured_native_mcp_catalog(
                self.execution.project_id, config_source=self._source
            )
            matches = [
                entry
                for entry in catalog
                if entry.binding == binding and executor_binding.spec in entry.manifest
            ]
            if len(matches) != 1:
                _deny()
            entry = matches[0]

            def current():
                return (
                    self._identity() == self.execution.identity
                    and self._current() is True
                    and is_current_host_request() is True
                    and current_native_execution_slice() is execution_slice
                    and execution_slice.active
                    and self._owns(self.execution, native_session) is True
                    and source_execution.is_current_origin() is True
                    and configured_native_mcp_catalog(
                        self.execution.project_id, config_source=self._source
                    )
                    == catalog
                )

            if source_execution.is_current() is not True or not current():
                _deny()
            resolver = SimpleNamespace(
                resources_for_tool=lambda execution, operation: actual_mcp_resources(
                    execution,
                    operation,
                    executor_binding=executor_binding,
                    source_execution=source_execution,
                )
            )
            tools = BoundToolResourceAuthority(
                self.execution,
                authorizer=self._resources,
                resolver=resolver,
                current_identity=self._identity,
                is_current_execution=current,
            )

            def admit(target):
                from openjiuwen.harness_protocol import json_value_to_builtin

                arguments = json.dumps(
                    json_value_to_builtin(actual_operation.arguments),
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                )
                return (
                    current()
                    and target.method == "POST"
                    and target.url == binding.endpoint
                    and target.tool_name == executor_binding.spec.remote_name
                    and target.arguments_json == arguments
                    and target.rpc_method
                    in {
                        "initialize",
                        "notifications/initialized",
                        "tools/call",
                        "tools/list",
                    }
                    and tools.check(actual_operation)
                )

            if not tools.check(actual_operation):
                _deny()
            credential = BoundCredentialAuthority(
                self.execution,
                uses=(binding.credential_use,),
                authorizer=self._resources,
                resolver=ConfiguredMcpCredentialResolver(
                    entry, config_source=self._source, credential_decoder=self._decoder
                ),
                current_identity=self._identity,
                is_current_execution=current,
            )
            credential.check_for_request(
                binding.credential_use, destination=binding.endpoint
            )
            return McpOperationAuthority(credential, admit, current)
        except Exception:
            raise ResourceAccessDenied("MCP execution authority unavailable") from None
