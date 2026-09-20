# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Host selection and immutable binding acceptance."""
from concurrent.futures import ThreadPoolExecutor

import pytest

from openjiuwen.harness_protocol import AgentExecutionSpec
from jiuwenswarm.runtime.harness.binding_store import ExecutionBindingStore
from jiuwenswarm.runtime.harness.bridge import prepare_execution
from jiuwenswarm.runtime.harness.config_source import (
    ExecutionConfigCatalog,
    ExecutionConfigSource,
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
