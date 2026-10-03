from __future__ import annotations

import asyncio
import inspect
from contextvars import ContextVar
from copy import deepcopy
from typing import TYPE_CHECKING, Any, Callable, Mapping

from packaging.specifiers import SpecifierSet
from packaging.version import Version

from openjiuwen.core.runner.callback.framework import AsyncCallbackFramework

from jiuwenswarm.common.security.base_crypto import CryptoProvider
from jiuwenswarm.common.utils import logger as default_logger
from jiuwenswarm.extensions.callback_compat import unregister_callback_sync
from jiuwenswarm.extensions.sdk.crypto_utility import CryptoUtility
from jiuwenswarm.extensions.types import ExtensionConfig

if TYPE_CHECKING:
    from jiuwenswarm.extensions.sdk.agent_server_client import (
        AgentServerClientExtension,
    )
    from jiuwenswarm.extensions.sdk.application_plugin import (
        ApplicationPluginExtension,
    )
    from jiuwenswarm.extensions.sdk.third_agent import ThirdAgentExtension
    from jiuwenswarm.gateway import AgentServerClient
    from jiuwenswarm.gateway.routing.third_agent import ThirdAgent
else:
    # Keep runtime type-hint introspection valid without importing Gateway and
    # transport adapters into a Runtime-direct process.
    AgentServerClientExtension = Any
    ApplicationPluginExtension = Any
    ThirdAgentExtension = Any
    AgentServerClient = Any
    ThirdAgent = Any


class ExtensionCapabilityError(RuntimeError):
    """A required instance capability is absent or incompatible."""


class _RegistrationTransaction:
    """Stage registry writes within the loading task, then publish atomically.

    A receipt only removes registrations it owns; borrowed registrations and
    later replacements survive cleanup. Arbitrary plugin side effects remain
    the plugin's responsibility in shutdown().
    """

    def __init__(self, registry: "ExtensionRegistry"):
        self.registry = registry
        self.before = dict(registry._values)
        self.task = asyncio.current_task()
        self.values = dict(self.before)
        self.callbacks: list[tuple[str, Callable, int, dict]] = []
        self.resources: list[Any] = []
        self.published = False
        self.closed = False

    def own(self, resource: Any) -> None:
        if resource is not None and all(resource is not old for old in self.resources):
            # A borrowed provider must never become owned simply by returning it.
            if all(resource is not old for old in self.before.values()):
                self.resources.append(resource)

    def commit(self) -> None:
        installed = []
        try:
            for event, handler, priority, kwargs in self.callbacks:
                self.registry.callback_framework.register_sync(
                    event,
                    handler,
                    priority=priority,
                    **kwargs,
                )
                installed.append((event, handler))
        except BaseException:
            for event, handler in reversed(installed):
                try:
                    unregister_callback_sync(
                        self.registry.callback_framework, event, handler
                    )
                except Exception as exc:
                    (self.registry.config.logger or default_logger).warning(
                        "extension callback cleanup failed: %s", exc
                    )
            raise
        self.registry._values = dict(self.values)
        self.registry._callback_receipts.append(self)
        self.published = True

    async def close(self) -> list[BaseException]:
        if self.closed:
            return []
        errors: list[BaseException] = []
        self.closed = True
        # Unpublish before shutdown so a failed close cannot leave a policy live.
        if self.published:
            for key, value in self.values.items():
                if self.before.get(key) is value:
                    continue
                if self.registry._values.get(key) is value:
                    if key in self.before:
                        self.registry._values[key] = self.before[key]
                    else:
                        self.registry._values.pop(key, None)
            for event, handler, _, _ in reversed(self.callbacks):
                try:
                    unregister_callback_sync(
                        self.registry.callback_framework, event, handler
                    )
                except Exception as exc:
                    errors.append(exc)
                    (self.registry.config.logger or default_logger).warning(
                        "extension callback cleanup failed: %s", exc
                    )
        for resource in reversed(self.resources):
            if hasattr(resource, "shutdown"):
                try:
                    await resource.shutdown()
                except BaseException as exc:
                    # The loader preserves initialization failures; normal close
                    # surfaces these errors after attempting every resource.
                    errors.append(exc)
                    (self.registry.config.logger or default_logger).warning(
                        "extension cleanup failed: %s", exc
                    )
        if self in self.registry._callback_receipts:
            self.registry._callback_receipts.remove(self)
        return errors


_ACTIVE_TRANSACTION: ContextVar[_RegistrationTransaction | None] = ContextVar(
    "extension_registration_transaction",
    default=None,
)


class _ApplicationPluginChannel:
    def __init__(self, channel: Any, plugin: ApplicationPluginExtension):
        self._channel = channel
        self._plugin = plugin

    def __getattr__(self, name: str) -> Any:
        return getattr(self._channel, name)

    def register_method(
        self,
        method: str,
        handler: Callable,
        *,
        local_only: bool = False,
        available_when_disabled: bool = False,
    ) -> None:
        async def enabled_handler(ws, req_id, params, session_id):  # noqa: ANN001
            if not available_when_disabled and not self._plugin.is_enabled():
                await self._channel.send_response(
                    ws,
                    req_id,
                    ok=False,
                    error=f"application plugin {self._plugin.plugin_id} is disabled",
                    code="APPLICATION_PLUGIN_DISABLED",
                )
                return
            await handler(ws, req_id, params, session_id)

        self._channel.register_method(method, enabled_handler, local_only=local_only)


class ExtensionRegistry:
    _instance: "ExtensionRegistry | None" = None

    def __init__(
        self,
        callback_framework: AsyncCallbackFramework,
        config: dict[str, Any],
        logger: Any,
    ):
        self._values: dict[str, Any] = {}
        self._loading: _RegistrationTransaction | None = None
        self._callback_receipts: list[_RegistrationTransaction] = []
        self.callback_framework = callback_framework
        self._config = ExtensionConfig(config=deepcopy(config), logger=logger)

    def _state(self, *, write: bool = False) -> dict[str, Any]:
        active = _ACTIVE_TRANSACTION.get()
        if (
            active is not None
            and active is self._loading
            and active.task is asyncio.current_task()
        ):
            return active.values
        if write and self._loading is not None:
            raise RuntimeError("extension registry is loading in another task")
        return self._values

    def _put(self, key: str, value: Any) -> None:
        self._state(write=True)[key] = value
        active = _ACTIVE_TRANSACTION.get()
        if (
            active is not None
            and active is self._loading
            and active.task is asyncio.current_task()
        ):
            active.own(value)

    def register_capability(
        self, name: str, provider: Any, *, version: str = "1"
    ) -> None:
        """Register an instance service; names and requirements belong to the host."""
        if not name or provider is None:
            raise ValueError("capability name and provider are required")
        Version(version)
        key = "capability:" + name
        if key in self._state():
            raise ValueError(f"capability already registered: {name}")
        self._put(key, provider)
        self._state(write=True)["capability-version:" + name] = version

    def get_capability(self, name: str) -> Any | None:
        return self._state().get("capability:" + name)

    def require_capabilities(self, requirements: Mapping[str, str]) -> None:
        for name, specifier in requirements.items():
            version = self._state().get("capability-version:" + name)
            if version is None or Version(version) not in SpecifierSet(specifier):
                raise ExtensionCapabilityError(
                    f"required extension capability unavailable: {name} {specifier}"
                )

    @classmethod
    def get_instance(cls) -> "ExtensionRegistry":
        if cls._instance is None:
            raise RuntimeError(
                "ExtensionRegistry 尚未初始化，请先调用 create_instance()"
            )
        return cls._instance

    @classmethod
    def create_instance(
        cls,
        callback_framework: AsyncCallbackFramework,
        config: dict[str, Any],
        logger: Any,
    ) -> "ExtensionRegistry":
        if cls._instance is not None:
            raise RuntimeError(
                "ExtensionRegistry 已初始化，请勿重复调用 create_instance()"
            )
        cls._instance = cls(
            callback_framework=callback_framework,
            config=config,
            logger=logger,
        )
        return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        cls._instance = None

    def register_agent_server_client(
        self, extension: AgentServerClientExtension
    ) -> None:
        self._put("agent_server_client", extension)

    def register_crypto_utility(self, extension: CryptoUtility) -> None:
        self._put("crypto_tool", extension)

    def register_third_agent(self, extension: ThirdAgentExtension) -> None:
        self._put("third_agent", extension)

    def register_application_plugin(
        self,
        extension: ApplicationPluginExtension,
    ) -> None:
        plugin_id = str(extension.plugin_id or "").strip()
        if not plugin_id:
            plugin_id = str(extension.metadata.id or "").strip()
        if not plugin_id:
            raise ValueError("application plugin id must not be empty")
        if "application:" + plugin_id in self._state():
            raise ValueError(f"application plugin already registered: {plugin_id}")
        self._put("application:" + plugin_id, extension)

    def get_application_plugins(self) -> tuple[ApplicationPluginExtension, ...]:
        return tuple(
            value
            for key, value in self._state().items()
            if key.startswith("application:")
        )

    def get_application_plugin(
        self,
        plugin_id: str,
    ) -> ApplicationPluginExtension | None:
        return self._state().get("application:" + plugin_id)

    def bind_application_plugins(
        self,
        channel: Any,
        *,
        agent_client: Any = None,
        media_attachment_normalizer: Callable[[dict[str, Any], str | None], None]
        | None = None,
    ) -> None:
        from jiuwenswarm.extensions.sdk.application_plugin import (
            ApplicationPluginServices,
        )

        services = ApplicationPluginServices(
            agent_client=agent_client,
            media_attachment_normalizer=media_attachment_normalizer,
        )
        for plugin in self.get_application_plugins():
            plugin.bind_web_channel(
                _ApplicationPluginChannel(channel, plugin), services
            )
        channel.application_plugin_registry = self

    def get_agent_server_client_extension(self) -> AgentServerClientExtension | None:
        return self._state().get("agent_server_client")

    def get_agent_server_client(self) -> AgentServerClient | None:
        ext = self._state().get("agent_server_client")
        return ext.get_client() if ext is not None else None

    def get_crypto_utility_extension(self) -> CryptoUtility | None:
        return self._state().get("crypto_tool")

    def get_crypto_provider(self) -> CryptoProvider | None:
        ext = self._state().get("crypto_tool")
        return ext.get_crypto() if ext is not None else None

    def get_third_agent_extension(self) -> ThirdAgentExtension | None:
        return self._state().get("third_agent")

    def get_third_agent(self) -> ThirdAgent | None:
        """Return registered ThirdAgent, or None when no extension registered."""
        ext = self._state().get("third_agent")
        return ext.get_third_agent() if ext is not None else None

    def register(
        self,
        event: str,
        handler: Callable,
        priority: int = 100,
        **kwargs,
    ) -> None:
        self._state(write=True)
        active = _ACTIVE_TRANSACTION.get()
        if (
            active is not None
            and active is self._loading
            and active.task is asyncio.current_task()
        ):
            # Unique wrapper makes unloading safe even if a borrowed callback
            # uses the exact same callable.
            async def owned_handler(*args, **call_kwargs):
                result = handler(*args, **call_kwargs)
                return await result if inspect.isawaitable(result) else result

            owned_handler.__signature__ = inspect.signature(handler)
            owned_handler.__extension_handler__ = handler
            active.callbacks.append((event, owned_handler, priority, kwargs))
        else:
            self.callback_framework.register_sync(
                event, handler, priority=priority, **kwargs
            )

    def unregister(self, event: str, handler: Callable | None = None) -> None:
        self._state(write=True)
        active = _ACTIVE_TRANSACTION.get()
        receipts = list(self._callback_receipts)
        if active is not None and active is self._loading:
            receipts.append(active)
        matched = False
        for receipt in receipts:
            for registration in list(receipt.callbacks):
                registered_event, wrapper, _, _ = registration
                if (
                    registered_event == event
                    and getattr(wrapper, "__extension_handler__", None) == handler
                ):
                    matched = True
                    if receipt.published:
                        unregister_callback_sync(
                            self.callback_framework, event, wrapper
                        )
                    elif not receipt.closed:
                        receipt.callbacks.remove(registration)
        if not matched:
            unregister_callback_sync(self.callback_framework, event, handler)

    async def trigger(
        self, event: str, context: Any | None = None, **kwargs: Any
    ) -> None:
        """触发事件。约定由调用方传入的 context 承载回调副作用"""
        if context is None and not kwargs:
            await self.callback_framework.trigger(event)
        elif context is not None:
            await self.callback_framework.trigger(event, context, **kwargs)
        else:
            await self.callback_framework.trigger(event, **kwargs)

    @property
    def config(self) -> ExtensionConfig:
        return self._config
