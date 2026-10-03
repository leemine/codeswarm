"""Task-local mandatory tool authority, owned by the submitting Runtime."""

from collections.abc import Awaitable, Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from types import MappingProxyType

from openjiuwen.harness_protocol import BeforeToolContext

ToolAuthorizer = Callable[[BeforeToolContext], Awaitable[bool | None]]
_AUTHORITY: ContextVar[Mapping[str, ToolAuthorizer] | None] = ContextVar(
    "tool_authority", default=None
)


_NATIVE_SOURCE: ContextVar[Callable[[], ToolAuthorizer | None] | None] = ContextVar(
    "native_tool_authority_source", default=None
)


@contextmanager
def native_authority_source_scope(source):
    """Private host selector inherited by the existing Native lifetime tasks."""
    if not callable(source):
        raise TypeError("Native authority source must be callable")
    token = _NATIVE_SOURCE.set(source)
    try:
        yield
    finally:
        _NATIVE_SOURCE.reset(token)


async def _deny_unknown_provider(_context: BeforeToolContext) -> bool:
    return False


def current_tool_authorizer(provider_id: str = "native") -> ToolAuthorizer | None:
    """Return the current task's authority; never cache it on a shared rail."""
    source = _NATIVE_SOURCE.get() if provider_id == "native" else None
    if source is not None:
        return source()
    return submitted_tool_authorizer(provider_id)


def submitted_tool_authorizer(provider_id: str = "native") -> ToolAuthorizer | None:
    """Capture the submitting Runtime scope, without a lifetime task selector."""
    authorities = _AUTHORITY.get()
    return (
        None
        if authorities is None
        else authorities.get(provider_id, _deny_unknown_provider)
    )


@contextmanager
def tool_authority_scope(
    callback: ToolAuthorizer | None,
    *,
    provider_authorizers: Mapping[str, ToolAuthorizer] | None = None,
) -> Iterator[None]:
    """Bind authority across owned execution; new asyncio tasks inherit it."""
    if callback is not None and not callable(callback):
        raise TypeError("tool authority must be callable or None")
    authorities = dict(provider_authorizers or {})
    if any(
        not isinstance(key, str) or not key or key.strip() != key or not callable(value)
        for key, value in authorities.items()
    ):
        raise TypeError("provider authorities must map normalized IDs to callbacks")
    if callback is not None:
        if "native" in authorities and authorities["native"] is not callback:
            raise ValueError("conflicting Native tool authorities")
        authorities["native"] = callback
    bound = (
        None
        if callback is None and provider_authorizers is None
        else MappingProxyType(authorities)
    )
    token = _AUTHORITY.set(bound)
    try:
        yield
    finally:
        _AUTHORITY.reset(token)
