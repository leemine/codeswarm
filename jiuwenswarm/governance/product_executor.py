"""Private proof linking one MCP invocation to its original Provider call.

The proof is created only after the Provider consumes its one-use ticket. It
does not grant access; the original Runtime authority still checks each use.
"""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from collections.abc import Callable
from typing import Any
from types import MappingProxyType
import inspect
import asyncio

from openjiuwen.harness_protocol import BeforeToolContext, ToolInvocation


@dataclass(slots=True, repr=False)
class ProductExecutorProof:
    owner: Any
    gateway: Any
    executor: Any
    invocation: ToolInvocation
    operation: BeforeToolContext
    current: Callable[[], bool]
    authorize: Callable
    active: bool = True
    invoke: Any = None
    kwargs: Any = None
    task: Any = None
    entered: bool = False

    def __post_init__(self):
        from openjiuwen.core.foundation.tool import Tool
        method = getattr(self.executor, 'invoke', None)
        # Framework Tool callbacks/transforms need the existing core final
        # invocation boundary. An early host callback cannot protect them.
        if (isinstance(self.executor, Tool) or not inspect.ismethod(method)
                or method.__self__ is not self.executor
                or getattr(type(self.executor), 'invoke', None) is not method.__func__
                or hasattr(method, '__wrapped__')):
            self.active = False
        self.invoke = method
        self.kwargs = MappingProxyType(dict(self.gateway._invoke_kwargs))
        self.task = asyncio.current_task()

    def is_current(self):
        method = getattr(self.executor, 'invoke', None)
        kwargs = self.gateway._invoke_kwargs
        return (self.active and self.task is not None and self.task is asyncio.current_task()
                and not self.task.done() and not self.task.cancelling() and inspect.ismethod(method)
                and method.__self__ is self.invoke.__self__
                and method.__func__ is self.invoke.__func__
                and kwargs.keys() == self.kwargs.keys()
                and all(value is self.kwargs[key] for key, value in kwargs.items())
                and self.current() is True)


_PROOF: ContextVar[ProductExecutorProof | None] = ContextVar('product_executor', default=None)


@contextmanager
def product_executor_scope(proof: ProductExecutorProof):
    token = _PROOF.set(proof)
    try:
        yield proof
    finally:
        proof.active = False
        proof.task = None
        _PROOF.reset(token)


def current_product_executor():
    proof = _PROOF.get()
    return proof if proof is not None and proof.is_current() else None


def require_product_executor(operation):
    proof = current_product_executor()
    if proof is None or proof.operation is not operation:
        raise ValueError('current product executor proof is required')
    return proof
