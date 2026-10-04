"""Task-local mandatory resource authority, owned by the submitting Runtime."""

from collections.abc import Awaitable, Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
import asyncio
from dataclasses import dataclass, field
from types import MappingProxyType

from openjiuwen.harness_protocol import BeforeToolContext

ToolAuthorizer = Callable[[BeforeToolContext], Awaitable[bool | None]]
_AUTHORITY: ContextVar[Mapping[str, ToolAuthorizer] | None] = ContextVar(
    "tool_authority", default=None
)


_NATIVE_SOURCE: ContextVar[Callable[[], ToolAuthorizer | None] | None] = ContextVar(
    "native_tool_authority_source", default=None
)


@dataclass(frozen=True)
class ExecutionResourceAuthorities(Mapping):
    providers: Mapping
    model_authorizer: Callable | None = None
    mcp_authorizer: Callable | None = None
    artifact_issuer_factory: Callable | None = None

    def __post_init__(self):
        object.__setattr__(self, 'providers', MappingProxyType(dict(self.providers)))
        if self.model_authorizer is not None and not callable(self.model_authorizer):
            raise TypeError('model authority must be callable')
        if self.mcp_authorizer is not None and not callable(self.mcp_authorizer):
            raise TypeError('MCP authority must be callable')
        if self.artifact_issuer_factory is not None and not callable(self.artifact_issuer_factory):
            raise TypeError('artifact issuer factory must be callable')

    def __getitem__(self, key):
        return self.providers[key]

    def __iter__(self):
        return iter(self.providers)

    def __len__(self):
        return len(self.providers)


_MODEL_AUTHORITY: ContextVar[Callable | None] = ContextVar('model_resource_authority', default=None)
_MCP_AUTHORITY: ContextVar[Callable | None] = ContextVar('mcp_resource_authority', default=None)
_ARTIFACT_AUTHORITY: ContextVar[Callable | None] = ContextVar('artifact_issuer_factory', default=None)
_NATIVE_MODEL_SOURCE: ContextVar[Callable | None] = ContextVar('native_model_authority_source', default=None)
_NATIVE_SLICE_SOURCE: ContextVar[Callable | None] = ContextVar('native_execution_slice_source', default=None)
_NATIVE_SLICE: ContextVar[object | None] = ContextVar('native_execution_slice', default=None)


@dataclass(slots=True)
class NativeExecutionSlice:
    owner: object
    tool_authorizer: Callable | None
    model_authorizer: Callable | None
    subject: object
    active: bool = True
    _task: asyncio.Task | None = field(default=None, repr=False, compare=False)
    _task_done_callback: Callable | None = field(default=None, repr=False, compare=False)
    mcp_authorizer: Callable | None = field(default=None, repr=False)
    artifact_issuer_factory: Callable | None = field(default=None, repr=False)


def current_native_execution_slice():
    bound = _NATIVE_SLICE.get()
    if bound is not None and bound._task is not None:
        # Task completion callbacks run on a later event-loop tick. Check here
        # too so inherited work cannot use that interval or pending cancellation.
        if bound._task.done() or bound._task.cancelling():
            bound.active = False
    return bound


def deny_native_execution_slice():
    """Fail closed in this task without restoring an inherited parent scope."""
    denied = NativeExecutionSlice(None, None, None, None, active=False)
    _NATIVE_SLICE.set(denied)
    return denied


def begin_native_execution_slice(ctx):
    source = _NATIVE_SLICE_SOURCE.get()
    if source is None:
        return None
    previous = _NATIVE_SLICE.get()
    denied = deny_native_execution_slice()
    # A failed child/owner admission must never retain an inherited valid slice.
    bound = source(ctx)
    if bound is None:
        return denied, previous, asyncio.current_task()
    if type(bound) is not NativeExecutionSlice or bound is previous or bound._task is not None:
        raise TypeError('Native execution admission must produce a fresh slice')
    task = asyncio.current_task()
    if task is None:
        raise RuntimeError('Native execution slice requires its owning task')
    bound._task = task

    def invalidate(_completed):
        bound.active = False
        bound._task = None
        bound._task_done_callback = None

    bound._task_done_callback = invalidate
    task.add_done_callback(invalidate)
    _NATIVE_SLICE.set(bound)
    return bound, previous, task


def end_native_execution_slice(handle, *, restore=True):
    if handle is None:
        return
    bound, previous, task = handle
    bound.active = False
    if bound._task_done_callback is not None:
        task.remove_done_callback(bound._task_done_callback)
        bound._task_done_callback = None
    bound._task = None
    # Never restore another task's ContextVar or a replacement execution slice.
    if restore and asyncio.current_task() is task and _NATIVE_SLICE.get() is bound:
        _NATIVE_SLICE.set(previous)


def submitted_model_authorizer():
    return _MODEL_AUTHORITY.get()


def submitted_mcp_authorizer():
    return _MCP_AUTHORITY.get()


def submitted_artifact_issuer_factory():
    return _ARTIFACT_AUTHORITY.get()


def current_model_authorizer():
    source = _NATIVE_MODEL_SOURCE.get()
    return source() if source is not None else submitted_model_authorizer()


async def deny_model_consumption(*_):
    from .resources import ResourceAccessDenied
    raise ResourceAccessDenied('model execution authority unavailable')


@contextmanager
def native_authority_source_scope(source, *, model_source=None, slice_source=None):
    """Private host selector inherited by the existing Native lifetime tasks."""
    if not callable(source):
        raise TypeError("Native authority source must be callable")
    if model_source is not None and not callable(model_source):
        raise TypeError("Native model source must be callable")
    if slice_source is not None and not callable(slice_source):
        raise TypeError("Native execution slice source must be callable")
    token = _NATIVE_SOURCE.set(source)
    model_token = _NATIVE_MODEL_SOURCE.set(model_source)
    slice_token = _NATIVE_SLICE_SOURCE.set(slice_source)
    try:
        yield
    finally:
        _NATIVE_SLICE_SOURCE.reset(slice_token)
        _NATIVE_MODEL_SOURCE.reset(model_token)
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
    model_token = _MODEL_AUTHORITY.set(getattr(provider_authorizers, "model_authorizer", None))
    mcp_token = _MCP_AUTHORITY.set(getattr(provider_authorizers, "mcp_authorizer", None))
    artifact_token = _ARTIFACT_AUTHORITY.set(getattr(provider_authorizers, "artifact_issuer_factory", None))
    try:
        yield
    finally:
        _ARTIFACT_AUTHORITY.reset(artifact_token)
        _MCP_AUTHORITY.reset(mcp_token)
        _MODEL_AUTHORITY.reset(model_token)
        _AUTHORITY.reset(token)
