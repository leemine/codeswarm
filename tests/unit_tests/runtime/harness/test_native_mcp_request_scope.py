"""Original HostRequest propagation only; no MCP client or Provider acceptance."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from openjiuwen.harness.schema.interaction import SendInputRequest
from jiuwenswarm.governance.resources import ResourceAccessDenied
from jiuwenswarm.governance.tool_context import (
    ExecutionResourceAuthorities,
    current_native_execution_slice,
    native_authority_source_scope,
    submitted_mcp_authorizer,
    tool_authority_scope,
)
from tests.unit_tests.runtime.harness.test_native_session import _setup, _request_slice


def test_mcp_authority_scope_is_private_and_restores_original():
    first, second = Mock(), Mock()
    assert submitted_mcp_authorizer() is None
    with tool_authority_scope(
        None,
        provider_authorizers=ExecutionResourceAuthorities({}, mcp_authorizer=first),
    ):
        assert submitted_mcp_authorizer() is first
        with tool_authority_scope(
            None,
            provider_authorizers=ExecutionResourceAuthorities(
                {}, mcp_authorizer=second
            ),
        ):
            assert submitted_mcp_authorizer() is second
        assert submitted_mcp_authorizer() is first
    assert submitted_mcp_authorizer() is None
    with pytest.raises(TypeError):
        ExecutionResourceAuthorities({}, mcp_authorizer=object())


@pytest.mark.asyncio
async def test_saved_factory_keeps_original_request_and_live_slice(tmp_path):
    execution, agent, session, _, _, _ = _setup(tmp_path, [])
    observed = []

    def authority(binding, **kwargs):
        observed.append(kwargs)
        assert kwargs["native_session"] is execution
        assert kwargs["is_current_host_request"]()
        return SimpleNamespace(current=kwargs["is_current_host_request"])

    with native_authority_source_scope(
        execution._current_resource_authority,
        slice_source=execution._execution_slice_for,
    ):
        for label in ["first", "second"]:
            with tool_authority_scope(
                None,
                provider_authorizers=ExecutionResourceAuthorities(
                    {}, mcp_authorizer=authority
                ),
            ):
                token = execution._register_host_request(
                    request=SendInputRequest(request_id=label, inputs={"query": label})
                )
            execution._native._active_turn = SimpleNamespace(
                content=SimpleNamespace(metadata={"native.host_request": token}),
                abort_requested=False,
            )
            with _request_slice(execution, token, agent, session):
                bound = current_native_execution_slice()
                result = bound.mcp_authorizer(
                    object(),
                    executor_binding=object(),
                    actual_operation=object(),
                    source_execution=object(),
                    execution_slice=bound,
                    native_session=execution,
                )
                assert result.current()
                if label == "first":
                    saved, saved_slice, saved_result = (
                        bound.mcp_authorizer,
                        bound,
                        result,
                    )
                else:
                    with pytest.raises(ResourceAccessDenied):
                        saved(
                            object(),
                            executor_binding=object(),
                            actual_operation=object(),
                            source_execution=object(),
                            execution_slice=saved_slice,
                            native_session=execution,
                        )
                    assert not saved_result.current()
        assert not result.current()
    assert len(observed) == 2


@pytest.mark.asyncio
async def test_mcp_factory_cannot_survive_request_removal_or_abort_during_bind(
    tmp_path,
):
    execution, agent, session, _, _, _ = _setup(tmp_path, [])

    def authority(*_, **kwargs):
        assert kwargs["is_current_host_request"]()
        execution._native.active_turn.abort_requested = True
        return object()

    with native_authority_source_scope(
        execution._current_resource_authority,
        slice_source=execution._execution_slice_for,
    ):
        with tool_authority_scope(
            None,
            provider_authorizers=ExecutionResourceAuthorities(
                {}, mcp_authorizer=authority
            ),
        ):
            token = execution._register_host_request(
                request=SendInputRequest(request_id="original", inputs={"query": "x"})
            )
        execution._native._active_turn = SimpleNamespace(
            content=SimpleNamespace(metadata={"native.host_request": token}),
            abort_requested=False,
        )
        with _request_slice(execution, token, agent, session):
            bound = current_native_execution_slice()
            with pytest.raises(ResourceAccessDenied):
                bound.mcp_authorizer(
                    object(),
                    executor_binding=object(),
                    actual_operation=object(),
                    source_execution=object(),
                    execution_slice=bound,
                    native_session=execution,
                )
            execution._native.active_turn.abort_requested = False
            execution._requests.pop(token)
            with pytest.raises(ResourceAccessDenied):
                bound.mcp_authorizer(
                    object(),
                    executor_binding=object(),
                    actual_operation=object(),
                    source_execution=object(),
                    execution_slice=bound,
                    native_session=execution,
                )
