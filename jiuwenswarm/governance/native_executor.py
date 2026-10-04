"""Private in-process proof of the Native executor under authorization.

This object is never protocol data. The Native rail creates an early or final scope;
resource resolvers must match the exact immutable operation object in that scope.
"""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from types import CodeType
from typing import Any

from openjiuwen.core.foundation.tool import current_tool_invocation
from openjiuwen.core.foundation.tool.base import _ToolMeta
from openjiuwen.core.session import get_current_session
from openjiuwen.core.runner.callback.decorator import (
    create_emit_before_decorator,
    create_emit_after_decorator,
    _make_transform_io_decorator,
)

from openjiuwen.core.runner import Runner
from openjiuwen.core.single_agent.ability_manager import AbilityManager
from openjiuwen.core.sys_operation.cwd import get_cwd, get_workspace
from openjiuwen.harness_protocol import BeforeToolContext


def _nested_codes(code):
    yield code
    for value in code.co_consts:
        if isinstance(value, CodeType):
            yield from _nested_codes(value)


# Tool construction installs these four core wrappers around the bound method.
# Arbitrary plugin wrappers need their own trusted resource resolver.
_WRAPPER_CODES = frozenset(
    code
    for factory in (
        _ToolMeta.__call__,
        create_emit_before_decorator,
        create_emit_after_decorator,
        _make_transform_io_decorator,
    )
    for code in _nested_codes(factory.__code__)
)


def _original_method(executor):
    method = executor.invoke
    for _ in range(4):
        if getattr(method, "__code__", None) not in _WRAPPER_CODES:
            return None
        method = getattr(method, "__wrapped__", None)
    return method


def has_native_invoke(executor, expected) -> bool:
    proof = _PROOF.get()
    if proof is None or proof.executor is not executor or not proof.is_current():
        return False
    method = proof.original_invoke
    return (
        getattr(method, "__self__", None) is executor
        and getattr(method, "__func__", None) is expected.invoke
    )


def _lookup(ctx, tool):
    manager = getattr(getattr(ctx, "agent", None), "ability_manager", None)
    if type(manager) is not AbilityManager:
        raise ValueError("Native ability manager is unavailable")
    card = manager.get(tool.tool_name)
    if card is None or card.name != tool.tool_name:
        raise ValueError("Native tool card is unavailable")
    executor = Runner.resource_mgr.get_tool(card.id or card.name, session=None)
    if executor is None or executor.card is not card:
        raise ValueError("Native executor does not own the installed card")
    return manager, card, executor


@dataclass
class _ProofLifetime:
    active: bool = True


@dataclass(frozen=True)
class NativeExecutorProof:
    operation: BeforeToolContext
    ctx: Any
    manager: Any
    card: Any
    executor: Any
    agent: Any
    session: Any
    agent_id: str
    backend: Any
    backend_mode: Any
    invoke: Any
    cwd: str
    workspace: str
    ambient_session: Any
    original_invoke: Any
    final_invocation: Any
    lifetime: _ProofLifetime = field(default_factory=_ProofLifetime)
    goal_check: Any = field(default=None, repr=False, compare=False)

    def is_current(self) -> bool:
        try:
            manager, card, executor = _lookup(self.ctx, self.operation)
            return (
                self.lifetime.active
                and manager is self.manager
                and card is self.card
                and executor is self.executor
                and get_cwd() == self.cwd
                and get_workspace() == self.workspace
                and get_current_session() is self.ambient_session
                and (
                    self.final_invocation is None
                    or (
                        current_tool_invocation() is self.final_invocation
                        and self.final_invocation.is_current()
                        and self.final_invocation.operation is self.operation
                        and self.final_invocation.original_invoke
                        is self.original_invoke
                    )
                )
                and self.ctx.agent is self.agent
                and self.ctx.session is self.session
                and self.agent.card.id == self.agent_id
                and getattr(
                    executor, "operation", getattr(executor, "_operation", None)
                )
                is self.backend
                and getattr(self.backend, "mode", None) == self.backend_mode
                and executor.invoke == self.invoke
                and (self.goal_check is None or self.goal_check() is None)
            )
        except Exception:
            return False


_PROOF: ContextVar[NativeExecutorProof | None] = ContextVar(
    "native_executor_proof", default=None
)


@contextmanager
def native_executor_scope(ctx, operation: BeforeToolContext):
    """Bind resource evidence around one host check.

    Early approval uses the registered executor. Final checks must use core's
    actual invocation proof; importing its API requires the supporting core.
    """
    try:
        manager, card, executor = _lookup(ctx, operation)
        final = current_tool_invocation()
        if final is not None and (
            final.operation is not operation
            or final.executor is not executor
            or final.agent_context is not ctx
        ):
            raise ValueError(
                "final Native invocation does not match the resource operation"
            )
        original = (
            final.original_invoke if final is not None else _original_method(executor)
        )
        proof = NativeExecutorProof(
            operation,
            ctx,
            manager,
            card,
            executor,
            ctx.agent,
            ctx.session,
            ctx.agent.card.id,
            getattr(executor, "operation", getattr(executor, "_operation", None)),
            getattr(
                getattr(executor, "operation", getattr(executor, "_operation", None)),
                "mode",
                None,
            ),
            executor.invoke,
            get_cwd(),
            get_workspace(),
            get_current_session(),
            original,
            final,
        )
        if operation.tool_name == "submit_goal_report":
            from .native_goal_tools import capture_goal_report
            object.__setattr__(proof, "goal_check", capture_goal_report(proof))
    except Exception:
        proof = None
    token = _PROOF.set(proof)
    try:
        yield proof
    finally:
        if proof is not None:
            proof.lifetime.active = False
        _PROOF.reset(token)


def require_native_executor(operation: BeforeToolContext) -> NativeExecutorProof:
    proof = _PROOF.get()
    if proof is None or proof.operation is not operation or not proof.is_current():
        raise ValueError("current Native executor proof is required")
    return proof
