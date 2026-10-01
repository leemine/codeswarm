# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Expose original Team tools through the existing product MCP gateway."""

from __future__ import annotations

from typing import TYPE_CHECKING

from openjiuwen.agent_teams.schema.team import TeamRole
from openjiuwen.agent_teams.tools.tool_factory import create_team_tools

from jiuwenswarm.runtime.harness.tool_gateway import (
    ProductToolGateway,
    ProductToolScope,
    ToolAdmission,
)

if TYPE_CHECKING:
    from openjiuwen.agent_teams import TeamMemberRuntimeBuild


def build_team_tool_gateway(
    request: TeamMemberRuntimeBuild,
    *,
    scope: ProductToolScope,
    admit: ToolAdmission,
) -> ProductToolGateway:
    """Bind a role-filtered catalog to the actual member's TeamBackend.

    The host supplies the admitted member scope and a per-call authorization
    callback. This does not perform Session admission or construct an execution.
    """
    if not callable(admit):
        raise ValueError("Team tool gateway requires runtime admission")
    return ProductToolGateway(
        team_product_tools(request), scope=scope,
        invoke_kwargs={"member_name": request.context.member_name, "display_name": request.context.member_name},
        admit=admit,
    )


def team_product_tools(request: TeamMemberRuntimeBuild):
    """Original role-filtered tool instances for a member composition root."""
    backend = request.team_backend
    context = request.context
    spec = request.spec
    if context.role not in (TeamRole.LEADER, TeamRole.TEAMMATE):
        raise ValueError("External Team tool gateway requires an execution member")
    if (
        not context.member_name
        or backend.member_name != context.member_name
        or backend.team_name != spec.team_name
        or backend.is_leader != (context.role is TeamRole.LEADER)
    ):
        raise ValueError("Team tool backend does not match the member identity")
    return create_team_tools(
        role=context.role.value,
        agent_team=backend,
        teammate_mode=str(spec.teammate_mode),
        dispatch_mode=spec.dispatch_mode,
        lifecycle=spec.lifecycle,
        team_mode=request.team_mode,
        lang=request.language,
        model_config_allocator=request.model_allocator.allocate if request.model_allocator else None,
        messager=request.messager,
        team_name=spec.team_name,
        team_permissions_enabled=spec.enable_permissions,
        # These tools operate on Native-only harness resources. Team Task,
        # Review, messages and ordinary member spawn retain their original code.
        exclude_tools={
            "checkpoint", "list_checkpoints", "swarmflow",
            "async_tasks_list", "async_task_output", "async_task_cancel",
            "spawn_external_cli", "spawn_bridge_agent", "spawn_human_agent",
            "spawn_passive_human",
        },
    )
