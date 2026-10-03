"""Instance assembly contracts; no installer or external service is invoked."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from openjiuwen.core.runner.callback.framework import AsyncCallbackFramework

from jiuwenswarm.extensions.loader import ExtensionLoader
from jiuwenswarm.extensions.manager import ExtensionManager
from jiuwenswarm.extensions.registry import ExtensionCapabilityError, ExtensionRegistry
from jiuwenswarm.extensions.sdk.application_plugin import ApplicationPluginExtension


def registry(config=None):
    return ExtensionRegistry(AsyncCallbackFramework(), config or {}, MagicMock())


class Resource:
    def __init__(self, name, log, fail=False):
        self.name, self.log, self.fail = name, log, fail

    async def shutdown(self):
        self.log.append(self.name)
        if self.fail:
            raise RuntimeError("cleanup error")


def module_loader(monkeypatch, reg, register):
    loader = ExtensionLoader(reg)
    monkeypatch.setattr(
        loader,
        "_import_module",
        lambda root: SimpleNamespace(register_extensions=register),
    )
    return loader


@pytest.mark.asyncio
async def test_instances_keep_configuration_capabilities_and_callbacks_separate(
    monkeypatch, tmp_path
):
    source = {"policy": {"label": "alice"}}
    first = registry(source)
    second = registry({"policy": {"label": "bob"}})
    source["policy"]["label"] = "mutated"
    calls, closed = [], []

    async def register(reg):
        resource = Resource(reg.config.config["policy"]["label"], closed)
        reg.register_capability("policy", resource, version="1.2")

        async def callback():
            calls.append(resource.name)

        reg.register("event", callback)
        return [resource]

    loaders = [module_loader(monkeypatch, reg, register) for reg in (first, second)]
    for loader in loaders:
        await loader.load_extension(tmp_path, manifest={})
    assert first.get_capability("policy") is not second.get_capability("policy")
    await first.trigger("event")
    await second.trigger("event")
    assert calls == ["alice", "bob"]
    await loaders[0].shutdown_loaded()
    await loaders[0].shutdown_loaded()
    assert closed == ["alice"]
    await first.trigger("event")
    await second.trigger("event")
    assert calls == ["alice", "bob", "bob"]
    assert first.get_capability("policy") is None
    assert second.get_capability("policy") is not None
    await loaders[1].shutdown_loaded()
    assert closed == ["alice", "bob"]


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_partial_registration_is_invisible_and_rolled_back(
    monkeypatch, tmp_path, cancel
):
    reg = registry()
    entered, proceed = asyncio.Event(), asyncio.Event()
    closed, callbacks = [], []
    old = Resource("borrowed", closed)
    reg.register_third_agent(old)

    async def register(active):
        active.register_third_agent(Resource("first", closed))
        active.register_capability("policy", Resource("second", closed, fail=True))
        active.register("event", lambda: callbacks.append("leak"))
        entered.set()
        await proceed.wait()
        raise ValueError("initial failure")

    loader = module_loader(monkeypatch, reg, register)
    task = asyncio.create_task(loader.load_extension(tmp_path, manifest={}))
    await entered.wait()
    assert reg.get_third_agent_extension() is old
    assert reg.get_capability("policy") is None
    await reg.trigger("event")
    assert callbacks == []
    if cancel:
        task.cancel()
    else:
        proceed.set()
    with pytest.raises(
        asyncio.CancelledError if cancel else ValueError,
        match=None if cancel else "initial failure",
    ):
        await task
    assert closed == ["second", "first"]
    assert reg.get_third_agent_extension() is old
    assert reg.get_capability("policy") is None
    await loader.shutdown_loaded()
    assert closed == ["second", "first"]


@pytest.mark.asyncio
async def test_application_initialize_failure_is_cleaned_once(monkeypatch, tmp_path):
    reg = registry()
    calls = []

    class Plugin(ApplicationPluginExtension):
        plugin_id = "broken"

        async def initialize(self, config):
            calls.append("initialize")
            raise ValueError("application init failed")

        async def shutdown(self):
            calls.append("shutdown")

    plugin = Plugin()

    async def register(active):
        active.register_application_plugin(plugin)
        return [plugin]

    loader = module_loader(monkeypatch, reg, register)
    with pytest.raises(ValueError, match="application init failed"):
        await loader.load_extension(tmp_path, manifest={})
    assert reg.get_application_plugins() == ()
    assert calls == ["initialize", "shutdown"]


@pytest.mark.asyncio
async def test_cleanup_preserves_borrowed_values_and_later_replacements(
    monkeypatch, tmp_path
):
    reg, closed = registry(), []
    borrowed = Resource("borrowed", closed)
    replacement = Resource("replacement", closed)
    reg.register_third_agent(borrowed)

    async def register(active):
        owned = Resource("owned", closed)
        active.register_third_agent(owned)
        return [borrowed, owned]

    loader = module_loader(monkeypatch, reg, register)
    await loader.load_extension(tmp_path, manifest={})
    reg.register_third_agent(replacement)
    await loader.shutdown_loaded()
    assert reg.get_third_agent_extension() is replacement
    assert closed == ["owned"]


@pytest.mark.asyncio
async def test_unregister_and_shutdown_do_not_remove_borrowed_callback(
    monkeypatch, tmp_path
):
    reg, calls = registry(), []

    async def callback():
        calls.append("called")

    reg.register("event", callback)

    class Extension:
        async def shutdown(self):
            reg.unregister("event", callback)

    async def register(active):
        active.register("event", callback)
        return [Extension()]

    loader = module_loader(monkeypatch, reg, register)
    await loader.load_extension(tmp_path, manifest={})
    await reg.trigger("event")
    assert calls == ["called", "called"]
    await loader.shutdown_loaded()
    await reg.trigger("event")
    assert calls == ["called", "called", "called"]


def manager(monkeypatch, tmp_path, reg, requirements=None):
    result = ExtensionManager(
        reg, config={}, root_dir=tmp_path, required_capabilities=requirements
    )
    monkeypatch.setattr(
        result.loader, "discover_extension_roots", lambda: [Path("good"), Path("bad")]
    )
    monkeypatch.setattr(result.loader, "load_manifest", lambda root: {})
    return result


@pytest.mark.asyncio
async def test_required_failure_cleans_successes_but_optional_failure_is_diagnostic(
    monkeypatch, tmp_path
):
    for requirements, must_fail in [
        ({"missing": ">=1"}, True),
        ({"policy": ">=1,<2"}, False),
    ]:
        reg, closed = registry(), []
        mgr = manager(monkeypatch, tmp_path, reg, requirements)

        async def good(active):
            resource = Resource("good", closed)
            active.register_capability("policy", resource, version="1.3")
            return [resource]

        async def bad(active):
            active.register_third_agent(Resource("bad", closed))
            raise ValueError("optional failed")

        monkeypatch.setattr(
            mgr.loader,
            "_import_module",
            lambda root: SimpleNamespace(
                register_extensions=good if root.name == "good" else bad
            ),
        )
        if must_fail:
            with pytest.raises(ExtensionCapabilityError, match="missing"):
                await mgr.load_all_extensions()
            assert reg.get_capability("policy") is None
            assert closed == ["bad", "good"]
        else:
            await mgr.load_all_extensions()
            assert reg.get_capability("policy") is not None
            assert closed == ["bad"]
        assert mgr.diagnostics == [{"path": "bad", "error": "optional failed"}]
        await mgr.shutdown_all_extensions()
        assert closed == ["bad", "good"]


def test_explicit_search_configuration_avoids_globals(monkeypatch, tmp_path):
    import jiuwenswarm.extensions.manager as implementation

    def forbidden():
        raise AssertionError("global configuration read")

    monkeypatch.setattr(implementation, "get_config", forbidden)
    monkeypatch.setattr(implementation, "get_root_dir", forbidden)
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    managers = []
    for path in (a, b):
        config = {"extensions": {"extension_dirs": str(path)}}
        managers.append(
            ExtensionManager(registry(config), config=config, root_dir=path)
        )
    assert a in managers[0].loader._search_paths
    assert a not in managers[1].loader._search_paths
    assert b in managers[1].loader._search_paths
    with pytest.raises(AttributeError):
        managers[0].registry = registry()


def test_capability_versions_fail_closed():
    reg = registry()
    reg.register_capability("policy", object(), version="1.2")
    reg.require_capabilities({"policy": ">=1,<2"})
    with pytest.raises(ExtensionCapabilityError):
        reg.require_capabilities({"policy": ">=2"})
    with pytest.raises(ValueError):
        reg.register_capability("policy", object())


@pytest.mark.asyncio
async def test_dependency_and_runtime_versions_rejected_before_import(
    monkeypatch, tmp_path
):
    import jiuwenswarm.extensions.loader as implementation

    reg = registry()
    loader = ExtensionLoader(reg)
    monkeypatch.setattr(
        implementation.importlib.metadata, "version", lambda package: "1.0"
    )
    monkeypatch.setattr(
        loader,
        "_import_module",
        lambda root: pytest.fail("must not import incompatible code"),
    )
    with pytest.raises(RuntimeError, match="dependency incompatible"):
        await loader.load_extension(
            tmp_path, manifest={"dependencies": {"example": ">=2"}}
        )
    monkeypatch.setattr(implementation, "__version__", "0.2.5.beta1")
    with pytest.raises(RuntimeError, match="requires jiuwenswarm"):
        await loader.load_extension(
            tmp_path, manifest={"min_jiuwenswarm_version": "0.3"}
        )


@pytest.mark.asyncio
async def test_missing_dependency_install_failure_is_not_ignored(monkeypatch, tmp_path):
    import subprocess
    import jiuwenswarm.extensions.loader as implementation

    def missing(package):
        raise implementation.importlib.metadata.PackageNotFoundError(package)

    def failed(*args, **kwargs):
        raise subprocess.CalledProcessError(1, "isolated-test-no-install")

    monkeypatch.setattr(implementation.importlib.metadata, "version", missing)
    monkeypatch.setattr(subprocess, "check_call", failed)
    with pytest.raises(RuntimeError, match="installation failed"):
        await ExtensionLoader(registry()).load_extension(
            tmp_path, manifest={"dependencies": {"missing-example": ">=1"}}
        )


@pytest.mark.asyncio
async def test_extension_dependencies_are_ordered_and_checked_separately(
    monkeypatch, tmp_path
):
    reg, calls = registry(), []
    mgr = ExtensionManager(reg, config={}, root_dir=tmp_path)
    manifests = {
        Path("consumer"): {
            "id": "consumer",
            "version": "2",
            "requires_extensions": {"base": ">=1,<2"},
        },
        Path("base"): {"id": "base", "version": "1.1"},
        Path("bad"): {
            "id": "bad",
            "version": "1",
            "requires_extensions": {"base": ">=2"},
        },
    }
    monkeypatch.setattr(mgr.loader, "discover_extension_roots", lambda: list(manifests))
    monkeypatch.setattr(mgr.loader, "load_manifest", manifests.__getitem__)

    def module(root):
        async def register(active):
            calls.append(root.name)

        return SimpleNamespace(register_extensions=register)

    monkeypatch.setattr(mgr.loader, "_import_module", module)
    await mgr.load_all_extensions()
    assert calls == ["base", "consumer"]
    assert len(mgr.diagnostics) == 1
    assert "required extension unavailable: base >=2" in mgr.diagnostics[0]["error"]
    await mgr.shutdown_all_extensions()
    assert not reg._values


@pytest.mark.asyncio
async def test_later_cancellation_closes_earlier_loads_and_preserves_borrowed(
    monkeypatch, tmp_path
):
    reg, closed = registry(), []
    borrowed = Resource("borrowed", closed)
    reg.register_crypto_utility(borrowed)
    mgr = manager(monkeypatch, tmp_path, reg)

    async def register(active):
        if active.get_capability("first") is None:
            resource = Resource("first", closed)
            active.register_capability("first", resource)
            return [resource]
        active.register_third_agent(Resource("second", closed, fail=True))
        raise asyncio.CancelledError()

    monkeypatch.setattr(
        mgr.loader,
        "_import_module",
        lambda root: SimpleNamespace(register_extensions=register),
    )
    with pytest.raises(asyncio.CancelledError):
        await mgr.load_all_extensions()
    assert closed == ["second", "first"]
    assert reg.get_crypto_utility_extension() is borrowed
    assert reg.get_capability("first") is None


@pytest.mark.asyncio
async def test_legacy_initialization_is_not_repeated_and_close_is_reverse(
    monkeypatch, tmp_path
):
    reg, log = registry(), []

    class Legacy(Resource):
        async def initialize(self, config):
            log.append("init:" + self.name)

    async def register(active):
        resources = [Legacy("first", log), Legacy("second", log)]
        for resource in resources:
            await resource.initialize(active.config)
        return resources

    loader = module_loader(monkeypatch, reg, register)
    await loader.load_extension(tmp_path, manifest={})
    assert log == ["init:first", "init:second"]
    await loader.shutdown_loaded()
    await loader.shutdown_loaded()
    assert log == ["init:first", "init:second", "second", "first"]


@pytest.mark.asyncio
async def test_shutdown_reports_errors_after_attempting_all_resources(
    monkeypatch, tmp_path
):
    reg, log = ExtensionRegistry(AsyncCallbackFramework(), {}, None), []

    async def register(active):
        resources = [Resource("first", log), Resource("second", log, fail=True)]
        active.register_capability("policy", resources[1])
        return resources

    loader = module_loader(monkeypatch, reg, register)
    await loader.load_extension(tmp_path, manifest={})
    with pytest.raises(ExceptionGroup, match="extension shutdown failed"):
        await loader.shutdown_loaded()
    assert log == ["second", "first"]
    assert reg.get_capability("policy") is None


@pytest.mark.asyncio
async def test_missing_logger_and_cleanup_error_preserve_initial_failure(
    monkeypatch, tmp_path
):
    reg, log = ExtensionRegistry(AsyncCallbackFramework(), {}, None), []

    async def register(active):
        active.register_capability("policy", Resource("failed", log, fail=True))
        raise ValueError("keep this error")

    loader = module_loader(monkeypatch, reg, register)
    with pytest.raises(ValueError, match="keep this error"):
        await loader.load_extension(tmp_path, manifest={})
    assert log == ["failed"]


@pytest.mark.asyncio
async def test_legacy_faas_bootstrap_supplies_real_config(monkeypatch, tmp_path):
    from jiuwenswarm.extensions import clawee
    from jiuwenswarm.common import config as config_module

    supplied = {"gateway": {"agent_client": {"type": "yuanrong"}}}
    monkeypatch.setattr(config_module, "get_config", lambda: supplied)
    monkeypatch.setattr(clawee, "_server_initialized", False)
    monkeypatch.setattr(ExtensionRegistry, "_instance", None)
    observed = []

    async def stop_after_extensions(manager):
        observed.append(manager.registry.config.config)
        raise RuntimeError("stop before server startup")

    monkeypatch.setattr(ExtensionManager, "_setup_search_paths", lambda manager: None)
    monkeypatch.setattr(ExtensionManager, "load_all_extensions", stop_after_extensions)
    with pytest.raises(RuntimeError, match="stop before server startup"):
        await clawee._ensure_server_initialized_async()
    assert observed == [supplied]


def test_conflicting_manager_config_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="must match registry"):
        ExtensionManager(
            registry({"policy": "alice"}), config={"policy": "bob"}, root_dir=tmp_path
        )
