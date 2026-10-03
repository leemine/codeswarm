from pathlib import Path
from typing import Any, Mapping
from copy import deepcopy

from jiuwenswarm.common.config import get_config
from jiuwenswarm.extensions.loader import ExtensionLoader
from jiuwenswarm.extensions.registry import ExtensionRegistry
from jiuwenswarm.common.utils import get_root_dir, logger


_DEFAULT_PACKAGE_EXTENSION_DIR = ("jiuwenswarm", "extensions")
_DEFAULT_EXTENSION_DIR = "/".join(_DEFAULT_PACKAGE_EXTENSION_DIR)
_USER_APPLICATION_PLUGIN_DIR = "application_plugins"


def _is_default_package_extension_dir(path: Path) -> bool:
    parts = tuple(part.lower() for part in path.parts if part not in ("", "."))
    return parts == _DEFAULT_PACKAGE_EXTENSION_DIR


def _extension_search_path_candidates(path_value: str) -> list[Path]:
    path = Path(path_value)
    if path.is_absolute():
        return [path]

    candidates = [path.resolve()]
    if _is_default_package_extension_dir(path):
        candidates.append(Path(__file__).resolve().parent)
    return candidates


def _split_extension_dirs(value: str) -> list[str]:
    # 按需求使用 ';' 分割
    return [p.strip() for p in value.split(";") if p.strip()]


def _dedupe_extension_dirs(paths: list[str]) -> list[str]:
    seen: set[tuple[str, ...]] = set()
    result: list[str] = []
    for path in paths:
        key = tuple(part.lower() for part in Path(path).parts if part not in ("", "."))
        if key in seen:
            continue
        seen.add(key)
        result.append(path)
    return result


def _extension_dir_paths_from_config(cfg: dict) -> list[str]:
    """读取 ``extensions.extension_dirs``（扩展包搜索目录：仅支持字符串，用 ';' 分割）。"""
    ext = cfg.get("extensions")
    dirs = ext.get("extension_dirs") if isinstance(ext, dict) else None
    paths = _split_extension_dirs(dirs) if isinstance(dirs, str) else []
    paths.append(_DEFAULT_EXTENSION_DIR)
    return _dedupe_extension_dirs(paths)


class ExtensionManager:
    def __init__(
        self,
        registry: ExtensionRegistry,
        *,
        config: dict[str, Any] | None = None,
        root_dir: Path | None = None,
        required_capabilities: Mapping[str, str] | None = None,
    ):
        if config is not None and config != registry.config.config:
            raise ValueError("manager config must match registry instance config")
        self._registry = registry
        self._config = deepcopy(config) if config is not None else get_config()
        self._root_dir = Path(root_dir) if root_dir is not None else get_root_dir()
        self.required_capabilities = dict(required_capabilities or {})
        self.diagnostics: list[dict[str, str]] = []
        self.loader = ExtensionLoader(registry)
        self._loaded_extensions: list[Any] = []
        self._setup_search_paths()

    @property
    def registry(self) -> ExtensionRegistry:
        return self._registry

    def _setup_search_paths(self) -> None:
        seen: set[str] = set()
        user_plugin_dir = self._root_dir / _USER_APPLICATION_PLUGIN_DIR
        user_plugin_dir.mkdir(parents=True, exist_ok=True)
        extension_dirs = _extension_dir_paths_from_config(self._config)
        for path in [str(user_plugin_dir), *extension_dirs]:
            for p in _extension_search_path_candidates(path):
                if not p.exists():
                    continue
                key = str(p.resolve()).lower()
                if key in seen:
                    continue
                seen.add(key)
                self.loader.add_search_path(p)

    async def load_all_extensions(
        self,
        *,
        include_transport_extensions: bool = True,
    ) -> None:
        self.diagnostics = []
        roots = self.loader.discover_extension_roots()
        logger.info("[ExtensionManager] 发现扩展路径: %s", roots)
        # Resolve only the explicit new extension dependency field. Legacy
        # `dependencies` continues to mean Python distributions to the loader.
        pending = list(roots)
        manifests = {}
        for path in roots:
            try:
                manifests[path] = self.loader.load_manifest(path)
            except Exception as exc:
                self.diagnostics.append({"path": str(path), "error": str(exc)})
                pending.remove(path)
        ordered = []
        while pending:
            pending_ids = {str(manifests[path].get("id") or "") for path in pending}
            ready = [
                path
                for path in pending
                if not (
                    set(manifests[path].get("requires_extensions", {})) & pending_ids
                )
            ]
            if not ready:
                for path in pending:
                    self.diagnostics.append(
                        {"path": str(path), "error": "cyclic extension dependencies"}
                    )
                break
            ordered.extend(ready)
            pending = [path for path in pending if path not in ready]
        for path in ordered:
            try:
                manifest = manifests[path]
                if (
                    not include_transport_extensions
                    and manifest.get("requires_transport") is True
                ):
                    logger.info(
                        "[ExtensionManager] Runtime 直连跳过 transport 扩展: %s",
                        path,
                    )
                    continue
                loaded = await self.loader.load_extension(path, manifest=manifest)
                if loaded:
                    logger.info("[ExtensionManager] 加载 %s", loaded)
                    if isinstance(loaded, list):
                        self._loaded_extensions.extend(loaded)
                    else:
                        self._loaded_extensions.append(loaded)
            except Exception as e:
                self.diagnostics.append({"path": str(path), "error": str(e)})
                logger.error("[ExtensionManager] 加载扩展 %s 失败: %s", path, e)
            except BaseException:
                await self._rollback_loaded()
                raise
        try:
            self.registry.require_capabilities(self.required_capabilities)
        except BaseException:
            await self._rollback_loaded()
            raise

    async def _rollback_loaded(self) -> None:
        try:
            await self.shutdown_all_extensions()
        except BaseException as exc:
            self.diagnostics.append({"path": "<rollback>", "error": str(exc)})
            logger.warning("[ExtensionManager] rollback cleanup failed: %s", exc)

    async def shutdown_all_extensions(self) -> None:
        try:
            await self.loader.shutdown_loaded()
        finally:
            self._loaded_extensions.clear()

    def list_extensions(self) -> list[dict]:
        return [
            {
                "id": p.metadata.id,
                "name": p.metadata.name,
                "version": p.metadata.version,
            }
            for p in self._loaded_extensions
            if hasattr(p, "metadata")
        ]
