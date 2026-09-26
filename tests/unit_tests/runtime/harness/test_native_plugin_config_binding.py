"""Host-owned native plugin snapshot binding tests."""

from openjiuwen.harness_protocol import AgentExecutionSpec

from jiuwenswarm.runtime.harness.binding_store import ExecutionBindingStore
from jiuwenswarm.runtime.harness.config_source import ExecutionConfigCatalog, ExecutionConfigSource

SCOPE = dict(subject_id="alice", host_session_id="s1", workspace="/tmp/work")


def test_native_plugin_selection_is_frozen_in_the_server_owned_profile():
    plugin = {
        "plugin_id": "fixed@local",
        "source_type": "local",
        "source_locator": "/prepared/fixed",
        "version": "1.2.3",
        "content_sha256": "a" * 64,
        "enabled": True,
        "required_components": ["skills", "mcp"],
        "mcp_server_names": ["fixed_marker"],
    }
    catalog = ExecutionConfigCatalog(
        {
            "codex": {
                "provider_id": "codex",
                "config_revision": "plugin-r1",
                "provider_config": {"inherit_process_env": False, "native_plugins": [plugin]},
            }
        },
        default_profile_id="codex",
    )
    plugin["enabled"] = False
    plugin["mcp_server_names"].append("ambient")
    resolved = catalog.source(explicit_profile_id="codex").resolve()
    snapshot = resolved.provider_config["native_plugins"][0]
    assert snapshot["enabled"] is True
    assert snapshot["mcp_server_names"] == ("fixed_marker",)

    store = ExecutionBindingStore()
    bound = store.bind(catalog.source(explicit_profile_id="codex"), **SCOPE)
    changed = AgentExecutionSpec(
        "codex",
        "plugin-r2",
        provider_config={"inherit_process_env": False, "native_plugins": [{**plugin, "enabled": False}]},
    )
    assert store.bind(ExecutionConfigSource(default=changed), **SCOPE) is bound
    next_session = store.bind(
        ExecutionConfigSource(explicit=changed),
        **(SCOPE | {"host_session_id": "s2"}),
    )
    assert next_session.binding.fingerprint != bound.binding.fingerprint


def test_opencode_native_plugin_hooks_and_tools_are_frozen_in_the_server_owned_profile():
    plugin = {
        "plugin_id": "sql2java-workflow",
        "source_type": "local",
        "source_locator": "/prepared/sql2java/.opencode",
        "version": "git-f2dce8ace60f",
        "content_sha256": "b" * 64,
        "entrypoint": "plugins/workflow-engine.ts",
        "export_name": "WorkflowEnginePlugin",
        "required_hooks": ["event", "tool.execute.before", "tool.execute.after"],
        "required_tools": ["saveArtifact", "workflow"],
    }
    catalog = ExecutionConfigCatalog(
        {
            "opencode-sql2java": {
                "provider_id": "opencode",
                "config_revision": "oc-p3-r1",
                "provider_config": {"native_plugins": [plugin]},
            }
        },
        default_profile_id="opencode-sql2java",
    )
    plugin["required_hooks"].append("permission.ask")
    plugin["required_tools"].append("ambient")
    snapshot = catalog.source(explicit_profile_id="opencode-sql2java").resolve().provider_config[
        "native_plugins"
    ][0]
    assert snapshot["required_hooks"] == ("event", "tool.execute.before", "tool.execute.after")
    assert snapshot["required_tools"] == ("saveArtifact", "workflow")
