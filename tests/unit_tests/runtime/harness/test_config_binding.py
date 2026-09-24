# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Host selection and immutable binding acceptance."""
from concurrent.futures import ThreadPoolExecutor

import pytest

from openjiuwen.harness_protocol import AgentExecutionSpec
from openjiuwen.harness_protocol import HarnessContext, HostCapability
from jiuwenswarm.common.runtime_workspace import RuntimeWorkspacePaths
from jiuwenswarm.runtime.harness.binding_store import ExecutionBindingStore
from jiuwenswarm.runtime.harness.bridge import prepare_execution, prepare_execution_session
from jiuwenswarm.runtime.harness.config_source import (
    ExecutionConfigCatalog,
    ExecutionConfigSource,
    load_execution_catalog,
    parse_execution_config,
)

SCOPE = dict(subject_id="alice", host_session_id="s1", workspace="/tmp/work")


def test_execution_config_is_separate_from_model_provider():
    config = parse_execution_config({"provider_id": "claudecode", "config_revision": "r1",
                                     "provider_config": {"model": {"model": "test-model"}}})
    assert config.provider_id == "claudecode"
    with pytest.raises(ValueError):
        parse_execution_config({"provider": "model-vendor"})
    with pytest.raises(ValueError):
        parse_execution_config({"provider_id": "native", "config_revision": "r1", "typo": True})


def test_catalog_resolves_only_server_owned_snapshots_by_id():
    native = {"provider_id": "native", "config_revision": "r1"}
    external = {
        "provider_id": "codex",
        "config_revision": "r2",
        "provider_config": {"model": "server-owned"},
    }
    catalog = ExecutionConfigCatalog(
        {"builtin": native, "external": external}, default_profile_id="builtin"
    )
    external["provider_config"]["model"] = "mutated"
    assert catalog.profile_ids == ("builtin", "external")
    assert catalog.source().resolve().provider_id == "native"
    assert catalog.source(project_profile_id="external").resolve().provider_id == "codex"
    selected = catalog.source(
        explicit_profile_id="builtin", project_profile_id="external"
    ).resolve()
    assert selected.provider_id == "native"
    assert catalog.source(explicit_profile_id="external").resolve().provider_config["model"] == "server-owned"
    with pytest.raises(ValueError, match="unknown execution profile ID"):
        catalog.source(explicit_profile_id="missing")
    with pytest.raises(ValueError, match="unknown execution profile ID"):
        catalog.source(explicit_profile_id={"provider_id": "native"})


def test_optional_server_config_catalog_does_not_silently_fall_back():
    assert load_execution_catalog({"models": {}}) is None
    catalog = load_execution_catalog({
        "execution": {
            "default_profile_id": "native",
            "profiles": {
                "native": {"provider_id": "native", "config_revision": "r1"},
            },
        },
    })
    assert catalog.source().resolve().provider_id == "native"
    with pytest.raises(ValueError, match="default execution profile"):
        load_execution_catalog({
            "execution": {
                "default_profile_id": "missing",
                "profiles": {"native": {"provider_id": "native", "config_revision": "r1"}},
            },
        })
    with pytest.raises(TypeError, match="execution configuration"):
        load_execution_catalog({"execution": "native"})
    with pytest.raises(TypeError, match="execution configuration"):
        load_execution_catalog({"execution": None})


def test_full_access_projects_into_new_codex_execution_snapshot_only():
    codex_profile = {
        "provider_id": "codex",
        "config_revision": "codex-r1",
        "provider_config": {
            "codex_bin": "/opt/codex",
            "bypass_approvals_and_sandbox": False,
            "mcp_default_tools_approval_mode": "prompt",
        },
    }
    native_profile = {
        "provider_id": "native",
        "config_revision": "native-r1",
        "provider_config": {"marker": "unchanged"},
    }
    config = {
        "permissions": {"enabled": False, "mode": "manual"},
        "execution": {
            "default_profile_id": "codex",
            "profiles": {"codex": codex_profile, "native": native_profile},
        },
    }

    catalog = load_execution_catalog(config)
    effective = catalog.source().resolve().provider_config

    assert effective == {
        "codex_bin": "/opt/codex",
        "bypass_approvals_and_sandbox": True,
        "mcp_default_tools_approval_mode": "auto",
    }
    assert catalog.source(explicit_profile_id="native").resolve().provider_config == {
        "marker": "unchanged"
    }
    assert codex_profile["provider_config"] == {
        "codex_bin": "/opt/codex",
        "bypass_approvals_and_sandbox": False,
        "mcp_default_tools_approval_mode": "prompt",
    }


def test_enabled_permissions_preserve_codex_approval_configuration():
    catalog = load_execution_catalog({
        "permissions": {"enabled": True, "mode": "manual"},
        "execution": {
            "default_profile_id": "codex",
            "profiles": {
                "codex": {
                    "provider_id": "codex",
                    "config_revision": "codex-r1",
                    "provider_config": {
                        "bypass_approvals_and_sandbox": False,
                        "mcp_default_tools_approval_mode": "prompt",
                    },
                },
            },
        },
    })

    assert catalog.source().resolve().provider_config == {
        "bypass_approvals_and_sandbox": False,
        "mcp_default_tools_approval_mode": "prompt",
    }


def test_codex_bypass_does_not_advertise_conflicting_host_approval(tmp_path):
    root = tmp_path.resolve()
    source = ExecutionConfigSource(
        explicit=AgentExecutionSpec(
            "codex",
            "codex-r1",
            provider_config={"bypass_approvals_and_sandbox": True},
        )
    )
    session = prepare_execution_session(
        source,
        bindings=ExecutionBindingStore(),
        subject_id="alice",
        host_session_id="session-1",
        runtime_paths=RuntimeWorkspacePaths(
            internal_workspace_dir=root,
            runtime_workspace_root=root,
            cwd=root,
            project_root=root,
        ),
    )
    prepared = session.io.prepare_context(HarnessContext(
        agent_name="external",
        agent_id="external-1",
        host_session_id="session-1",
        system_prompt="",
        cwd=str(root),
    ))

    assert HostCapability.USER_INPUT in prepared.host_capabilities
    assert HostCapability.TOOL_APPROVAL not in prepared.host_capabilities


def test_defaults_change_only_new_sessions_and_explicit_change_is_rejected():
    store = ExecutionBindingStore()
    first = ExecutionConfigSource(default=AgentExecutionSpec("native", "r1"))
    old = store.bind(first, **SCOPE)
    changed = ExecutionConfigSource(default=AgentExecutionSpec("codex", "r2"))
    assert store.bind(changed, **SCOPE) is old
    assert store.bind(changed, **(SCOPE | {"host_session_id": "new"})).spec.provider_id == "codex"
    with pytest.raises(ValueError, match="does not match"):
        store.bind(ExecutionConfigSource(explicit=changed.default), **SCOPE)


def test_scope_and_same_revision_payload_isolation():
    store = ExecutionBindingStore()
    source = ExecutionConfigSource(default=AgentExecutionSpec("native", "r1"))
    one = store.bind(source, **SCOPE)
    assert store.bind(source, **(SCOPE | {"subject_id": "bob"})).binding != one.binding
    assert store.bind(source, **(SCOPE | {"workspace": "/tmp/other"})).binding != one.binding
    altered = AgentExecutionSpec("native", "r1", provider_config={"language": "en"})
    with pytest.raises(ValueError):
        store.bind(ExecutionConfigSource(explicit=altered), **SCOPE)


def test_concurrent_bind_and_stale_release():
    store = ExecutionBindingStore()
    source = ExecutionConfigSource(default=AgentExecutionSpec("native", "r1"))
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: store.bind(source, **SCOPE), range(20)))
    assert all(item is results[0] for item in results)
    old = results[0].binding
    store.release(old)
    new = store.bind(source, **SCOPE)
    store.release(old)
    assert store.bind(source, **SCOPE) is new


def test_bridge_constructs_native_and_external_without_shared_instances():
    store = ExecutionBindingStore()
    for provider in ("native", "claudecode"):
        source = ExecutionConfigSource(explicit=AgentExecutionSpec(provider, "r1"))
        scope = SCOPE | {"host_session_id": provider}
        one = prepare_execution(source, bindings=store, **scope)
        two = prepare_execution(source, bindings=store, **scope)
        assert one.binding is two.binding
        assert one.harness is not two.harness
        assert one.harness.provider_session_id is None


@pytest.mark.parametrize("value", [{}, {"full_access": "false"}, {"full_access": 1},
                                    {"full_access": True, "extra": False}, True])
def test_public_authorization_rejects_malformed_config(value):
    with pytest.raises((TypeError, ValueError)):
        parse_execution_config({"provider_id": "codex", "config_revision": "r1", "authorization": value})


@pytest.mark.parametrize("provider_id", ["codex", "opencode"])
@pytest.mark.parametrize("enabled", [False, True])
def test_host_authorization_overrides_new_profiles_without_vendor_json(
    provider_id, enabled
):
    from openjiuwen.harness_protocol import ExecutionAuthorization
    profile = {"provider_id": provider_id, "config_revision": "r1",
               "authorization": {"full_access": enabled}, "provider_config": {}}
    config = {"permissions": {"enabled": enabled},
              "execution": {"default_profile_id": "new", "profiles": {"new": profile}}}
    spec = load_execution_catalog(config).source().resolve()
    assert spec.authorization == ExecutionAuthorization(not enabled)
    assert spec.provider_config == {}
    assert profile["authorization"]["full_access"] is enabled


@pytest.mark.parametrize("provider_id", ["codex", "opencode"])
@pytest.mark.parametrize("full_access", [False, True])
def test_public_authorization_controls_product_approval_and_binding(
    tmp_path, provider_id, full_access
):
    from dataclasses import replace
    from openjiuwen.harness_protocol import ExecutionAuthorization
    spec = AgentExecutionSpec(
        provider_id, "r1", authorization=ExecutionAuthorization(full_access)
    )
    store = ExecutionBindingStore()
    session = prepare_execution_session(
        ExecutionConfigSource(explicit=spec), bindings=store, subject_id="alice", host_session_id="s1",
        runtime_paths=RuntimeWorkspacePaths(internal_workspace_dir=tmp_path, runtime_workspace_root=tmp_path,
                                            cwd=tmp_path, project_root=tmp_path),
    )
    context = session.io.prepare_context(HarnessContext(agent_name="external", agent_id="external-1",
                                                         host_session_id="s1", system_prompt="", cwd=str(tmp_path)))
    assert (HostCapability.TOOL_APPROVAL in context.host_capabilities) is not full_access
    assert HostCapability.USER_INPUT in context.host_capabilities
    changed = replace(spec, authorization=ExecutionAuthorization(not full_access))
    with pytest.raises(ValueError, match="does not match"):
        store.bind(ExecutionConfigSource(explicit=changed), subject_id="alice", host_session_id="s1",
                   workspace=str(tmp_path))
