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
    core_tool: bool = False
    scope: Any = None

    def __post_init__(self):
        import openjiuwen.core.foundation.tool as core_tools
        method = getattr(self.executor, 'invoke', None)
        self.core_tool = isinstance(self.executor, core_tools.Tool)
        if self.core_tool:
            # Never open the earlier input callbacks on an old core that has no
            # actual final-boundary host entry. Unknown wrappers fail in core.
            if not callable(getattr(core_tools, 'invoke_tool_with_authority', None)):
                self.active = False
        elif (not inspect.ismethod(method) or method.__self__ is not self.executor
                or getattr(type(self.executor), 'invoke', None) is not method.__func__
                or hasattr(method, '__wrapped__')):
            self.active = False
        self.invoke = method
        self.kwargs = MappingProxyType(dict(self.gateway._invoke_kwargs))
        self.task = asyncio.current_task()
        self.scope = self.gateway.scope

    def is_current(self):
        method = getattr(self.executor, 'invoke', None)
        kwargs = self.gateway._invoke_kwargs
        same_method = method is self.invoke if self.core_tool else (
            inspect.ismethod(method) and inspect.ismethod(self.invoke)
            and method.__self__ is self.invoke.__self__
            and method.__func__ is self.invoke.__func__
        )
        return (self.active and self.task is not None and self.task is asyncio.current_task()
                and not self.task.done() and not self.task.cancelling() and same_method
                and self.gateway.scope is self.scope
                and kwargs.keys() == self.kwargs.keys()
                and all(value is self.kwargs[key] for key, value in kwargs.items())
                and self.current() is True)

    def matches_subject(self):
        from openjiuwen.harness.execution_subject import current_execution_subject
        subject = current_execution_subject()
        return (subject is not None and subject.kind == 'agent'
                and subject.subject_id == self.scope.subject_id
                and subject.session_id == self.scope.host_session_id)

    async def authorize_final(self, operation):
        """Authorize transformed args only with the live core and source proof."""
        from openjiuwen.core.foundation.tool import current_tool_invocation
        invocation = current_tool_invocation()
        if (not self.core_tool or not self.entered or not self.is_current() or not self.matches_subject()
                or invocation is None or invocation.executor is not self.executor
                or invocation.source_operation is not self.operation or invocation.operation is not operation):
            return False
        try:
            allowed = await self.authorize(operation)
        except Exception:
            return False
        return (allowed is True and self.is_current() and self.matches_subject()
                and current_tool_invocation() is invocation and invocation.is_current())


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
    """Require actual final Tool proof, or the unchanged plain call proof.

    For core Tools, ``operation`` is the transformed final operation; the
    returned proof.operation remains the original ticket identity. Resource
    mappers must inspect the actual executor and all required resources, never
    infer delegation or credential rights from a product tool name.
    """
    proof = current_product_executor()
    if proof is None:
        raise ValueError('current product executor proof is required')
    if proof.core_tool:
        from openjiuwen.core.foundation.tool import current_tool_invocation
        final = current_tool_invocation()
        if (not proof.entered or not proof.matches_subject() or final is None
                or final.executor is not proof.executor or final.source_operation is not proof.operation
                or final.operation is not operation):
            raise ValueError('current final product executor proof is required')
    elif proof.operation is not operation:
        raise ValueError('current product executor proof is required')
    return proof
