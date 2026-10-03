from __future__ import annotations

import asyncio
import importlib
import importlib.util
import importlib.metadata

from packaging.specifiers import SpecifierSet
from packaging.version import Version
from pathlib import Path
from typing import Any

from jiuwenswarm.extensions.registry import (
    ExtensionRegistry,
    _RegistrationTransaction,
    _ACTIVE_TRANSACTION,
)
from jiuwenswarm.extensions.sdk.application_plugin import (
    ApplicationPluginExtension,
    ManifestApplicationPlugin,
)
from jiuwenswarm.common.utils import logger
from jiuwenswarm.common.version import __version__

MANIFEST_FILENAME = "extension.yaml"
ENTRY_FILENAME = "extension.py"


def _find_manifest(root: Path) -> Path | None:
    p = root / MANIFEST_FILENAME
    return p if p.exists() else None


def _find_entry_script(root: Path) -> Path | None:
    p = root / ENTRY_FILENAME
    return p if p.exists() else None


def _is_extension_root(path: Path) -> bool:
    return _find_manifest(path) is not None or _find_entry_script(path) is not None


def _extension_display_name(manifest: dict, root: Path) -> str:
    name = str(manifest.get("name", "")).strip()
    return name or root.name


class _ExtensionLifecycleConflict(RuntimeError):
    """A rejected lifecycle operation has not consumed any cleanup receipts."""


class ExtensionLoader:
    def __init__(self, registry: ExtensionRegistry):
        self.registry = registry
        self._search_paths: list[Path] = []
        self._loads: list[_RegistrationTransaction] = []

    def add_search_path(self, path: Path) -> None:
        if path.exists():
            self._search_paths.append(path)

    def discover_extension_roots(self) -> list[Path]:
        roots: list[Path] = []
        logger.info("[ExtensionLoader] 开始搜索扩展路径: %s", self._search_paths)
        for base_path in self._search_paths:
            if not base_path.exists():
                continue
            for subdir in base_path.iterdir():
                if not subdir.is_dir():
                    continue
                if _is_extension_root(subdir):
                    roots.append(subdir)
        return roots

    @staticmethod
    def load_manifest(root: Path) -> dict:
        """Read extension metadata without importing its entry module."""
        return _load_manifest_dict(root)

    def _check_lifecycle_idle(self) -> None:
        if self.registry._loading is not None or self.registry._closing is not None:
            raise _ExtensionLifecycleConflict("concurrent or nested extension lifecycle operation is unsupported")

    async def load_extension(self, root: Path, *, manifest: dict | None = None) -> Any:
        manifest = manifest if manifest is not None else _load_manifest_dict(root)
        self._check_lifecycle_idle()
        transaction = _RegistrationTransaction(self.registry)
        self.registry._loading = transaction
        token = _ACTIVE_TRANSACTION.set(transaction)
        try:
            self._check_compatibility(manifest)
            await self._install_dependencies(manifest, root)
            self.registry.require_capabilities(
                manifest.get("requires_capabilities", {})
            )
            for extension_id, specifier in manifest.get(
                "requires_extensions", {}
            ).items():
                version = self.registry._state().get(
                    "extension-version:" + extension_id
                )
                if version is None or Version(version) not in SpecifierSet(specifier):
                    raise RuntimeError(
                        f"required extension unavailable: {extension_id} {specifier}"
                    )
            extension_id = str(manifest.get("id") or "")
            implementation_version = str(manifest.get("version") or "")
            if implementation_version:
                Version(implementation_version)
            if extension_id:
                key = "extension-version:" + extension_id
                if key in self.registry._state():
                    raise ValueError(f"extension already loaded: {extension_id}")
                self.registry._state(write=True)[key] = implementation_version
            entry = _find_entry_script(root)
            if entry is None and manifest.get("package_type") == "application":
                plugin = ManifestApplicationPlugin(root)
                if not plugin.plugin_id:
                    raise ValueError(
                        "manifest-only application plugin id must not be empty"
                    )
                if not plugin.frontend_contributions():
                    raise ValueError(
                        "manifest-only application plugin must declare at least one frontend"
                    )
                self.registry.register_application_plugin(plugin)
                await plugin.initialize(self.registry.config)
                registered = [plugin]
            else:
                module = self._import_module(root)
                registered = (
                    await module.register_extensions(self.registry)
                    if hasattr(module, "register_extensions")
                    else None
                )
                items = registered if isinstance(registered, list) else [registered]
                for ext in items:
                    transaction.own(ext)
                # Returned resources initialize in return order. Preserve that
                # order even if registration happened in a different order.
                returned = []
                for ext in items:
                    if any(ext is owned for owned in transaction.resources) and all(
                        ext is not old for old in returned
                    ):
                        returned.append(ext)
                transaction.resources = [
                    owned
                    for owned in transaction.resources
                    if all(owned is not ext for ext in returned)
                ] + returned
                for ext in items:
                    if hasattr(ext, "set_extension_dir"):
                        ext.set_extension_dir(root)
                    # Legacy extensions initialize themselves; do not double-initialize.
                    if isinstance(ext, ApplicationPluginExtension):
                        await ext.initialize(self.registry.config)
            transaction.commit()
            self._loads.append(transaction)
            return registered
        except BaseException:
            await transaction.close()
            raise
        finally:
            _ACTIVE_TRANSACTION.reset(token)
            self.registry._loading = None

    async def shutdown_loaded(self) -> None:
        """Release this loader's registrations/resources, never a borrowed registry."""
        self._check_lifecycle_idle()
        self.registry._closing = asyncio.current_task()
        try:
            errors: list[BaseException] = []
            while self._loads:
                errors.extend(await self._loads.pop().close())
            if errors:
                raise BaseExceptionGroup("extension shutdown failed", errors)
        finally:
            self.registry._closing = None

    @staticmethod
    def _check_compatibility(manifest: dict) -> None:
        minimum = manifest.get("min_jiuwenswarm_version")
        if minimum:
            installed = Version(__version__)
            required = Version(str(minimum))
            # Existing manifests describe release families (0.2.5 includes its
            # beta distribution); explicit prerelease floors remain precise.
            candidate = (
                installed if required.is_prerelease else Version(installed.base_version)
            )
            if candidate < required:
                raise RuntimeError(
                    f"extension requires jiuwenswarm >= {minimum}, found {installed}"
                )

    async def _install_dependencies(self, manifest: dict, root: Path) -> None:
        """安装扩展声明的依赖"""
        dependencies = manifest.get("dependencies", {})
        if not dependencies:
            return
        extension_name = _extension_display_name(manifest, root)

        import shutil
        import subprocess
        import sys

        uv_path = shutil.which("uv")
        use_uv = uv_path is not None

        for package, version_spec in dependencies.items():
            package_name = f"{package}{version_spec}" if version_spec else package
            try:
                installed = importlib.metadata.version(package)
                if version_spec and Version(installed) not in SpecifierSet(
                    version_spec
                ):
                    raise RuntimeError(
                        f"extension dependency incompatible: {package}{version_spec}, found {installed}"
                    )
                logger.info(
                    f"[ExtensionLoader] 扩展 {extension_name} 依赖 {package} 已安装"
                )
                continue
            except importlib.metadata.PackageNotFoundError:
                pass

            logger.info(
                f"[ExtensionLoader] 正在安装扩展 {extension_name} 的依赖: {package_name}"
            )
            try:
                if use_uv:
                    subprocess.check_call(
                        [uv_path, "pip", "install", package_name],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.PIPE,
                        timeout=120,
                    )
                else:
                    subprocess.check_call(
                        [sys.executable, "-m", "pip", "install", package_name],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.PIPE,
                        timeout=120,
                    )
                installed = importlib.metadata.version(package)
                if version_spec and Version(installed) not in SpecifierSet(
                    version_spec
                ):
                    raise RuntimeError(
                        f"extension dependency incompatible after install: {package_name}"
                    )
                logger.info(
                    f"[ExtensionLoader] 扩展 {extension_name} 依赖 {package} 安装成功"
                )
            except subprocess.TimeoutExpired:
                raise RuntimeError(
                    f"extension dependency installation timed out: {package_name}"
                ) from None
            except subprocess.CalledProcessError as e:
                raise RuntimeError(
                    f"extension dependency installation failed: {package_name}"
                ) from e

    @staticmethod
    def _import_module(root: Path) -> Any:
        entry = _find_entry_script(root)
        if entry is None:
            raise FileNotFoundError(
                f"扩展入口脚本不存在（期望 {ENTRY_FILENAME}）: {root}"
            )

        module_name = root.name
        spec = importlib.util.spec_from_file_location(
            f"jiuwenswarm.loaded_extension.{module_name}",
            entry,
        )
        if spec is None or spec.loader is None:
            raise ImportError(f"无法加载扩展: {module_name}")

        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module


def _load_manifest_dict(root: Path) -> dict:
    manifest_path = _find_manifest(root)
    if manifest_path is None:
        return {}
    try:
        import yaml

        manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
        if not isinstance(manifest, dict):
            raise ValueError("extension manifest must be a mapping")
        for field in ("requires_capabilities", "requires_extensions", "dependencies"):
            if field in manifest and not isinstance(manifest[field], dict):
                raise ValueError(f"extension manifest {field} must be a mapping")
        return manifest
    except ImportError:
        return {}
