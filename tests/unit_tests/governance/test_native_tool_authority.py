"""Mandatory Native authority crosses optional permission and task boundaries."""

import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest
from openjiuwen.harness.rails.security.tool_security_rail import PermissionInterruptRail
from openjiuwen.harness.security import ToolPermissionHost

from jiuwenswarm.agents.harness.common.rails.permissions.resource_authority_rail import (
    NativeResourceAuthorityRail,
    ensure_native_tool_authority,
)
from jiuwenswarm.governance.tool_context import (
    current_tool_authorizer,
    tool_authority_scope,
)


def context():
    call = SimpleNamespace(
        name="read_file", id="call", arguments={"file_path": "/task/marker"}
    )
    return SimpleNamespace(
        inputs=SimpleNamespace(
            tool_call=call, tool_name=call.name, tool_args=deepcopy(call.arguments)
        ),
        extra={},
        session=None,
        agent=None,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", [True, False, None, 1, "true", "error"])
async def test_current_authority_is_strict_and_errors_deny(decision):
    async def authorize(operation):
        assert operation.arguments["file_path"] == "/task/marker"
        if decision == "error":
            raise RuntimeError("unavailable")
        return decision

    ctx = context()
    with tool_authority_scope(authorize):
        await NativeResourceAuthorityRail().before_tool_call(ctx)
    assert ctx.extra.get("_skip_tool", False) is (decision is not True)


@pytest.mark.asyncio
async def test_unbound_context_leaves_legacy_inputs_and_optional_decision_alone(monkeypatch):
    ctx = context()
    ctx.inputs.tool_args = "invalid"
    rail = NativeResourceAuthorityRail()
    def forbidden_allocation(**kwargs):
        pytest.fail("legacy execution must not allocate a mandatory permission rail")
    monkeypatch.setattr("jiuwenswarm.agents.harness.common.rails.permissions.resource_authority_rail.PermissionInterruptRail", forbidden_allocation)
    await rail.before_tool_call(ctx)
    assert not ctx.extra


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change", ["args", "name", "invalid", "boolean", "await_mutation"]
)
async def test_actual_execution_arguments_must_match_and_stay_fixed(change):
    ctx = context()
    if change == "args":
        ctx.inputs.tool_args["file_path"] = "/elsewhere"
    elif change == "name":
        ctx.inputs.tool_name = "write_file"
    elif change == "invalid":
        ctx.inputs.tool_args = "not-json"
    elif change == "boolean":
        ctx.inputs.tool_args = {"n": True}
        ctx.inputs.tool_call.arguments = {"n": 1}

    async def authorize(operation):
        if change == "await_mutation":
            await asyncio.sleep(0)
            ctx.inputs.tool_args["file_path"] = "/changed"
            ctx.inputs.tool_call.arguments["file_path"] = "/changed"
        return True

    with tool_authority_scope(authorize):
        await NativeResourceAuthorityRail().before_tool_call(ctx)
    assert ctx.extra["_skip_tool"] is True


@pytest.mark.asyncio
async def test_optional_approval_wait_cannot_authorize_revoked_call():
    permitted = True

    async def authorize(_):
        return permitted

    async def optional(_):
        nonlocal permitted
        await asyncio.sleep(0)
        permitted = False
        return ("approve",)

    ordinary = PermissionInterruptRail(
        host=ToolPermissionHost(permission_scene_hook=optional)
    )
    final = NativeResourceAuthorityRail()
    assert final.priority < ordinary.priority
    ctx = context()
    with tool_authority_scope(authorize):
        await ordinary.before_tool_call(ctx)
        await final.before_tool_call(ctx)
    assert ctx.extra["_skip_tool"] is True


@pytest.mark.asyncio
async def test_shared_rail_does_not_capture_first_tasks_identity():
    rail = NativeResourceAuthorityRail()
    ready = asyncio.Event()

    async def run(allowed):
        async def authorize(_):
            await ready.wait()
            return allowed

        with tool_authority_scope(authorize):
            child = asyncio.create_task(execute())
        return await child

    async def execute():
        ctx = context()
        assert current_tool_authorizer() is not None
        await rail.before_tool_call(ctx)
        return ctx.extra.get("_skip_tool", False)

    tasks = [asyncio.create_task(run(value)) for value in (True, False)]
    ready.set()
    assert await asyncio.gather(*tasks) == [False, True]
    assert current_tool_authorizer() is None


def test_scope_resets_after_error_and_rejects_noncallable():
    async def outer(_):
        return True

    with tool_authority_scope(outer):
        with pytest.raises(ValueError):
            with tool_authority_scope(None):
                assert current_tool_authorizer() is None
                raise ValueError("fixture")
        assert current_tool_authorizer() is outer
    assert current_tool_authorizer() is None
    with pytest.raises(TypeError):
        with tool_authority_scope(True):
            pass


def test_assembly_is_idempotent_and_independent_from_optional_group():
    from jiuwenswarm.server.runtime.agent_adapter.permission_rail_group import (
        PERMISSION_GROUP_TYPES,
    )

    rails = ensure_native_tool_authority([])
    assert ensure_native_tool_authority(rails) is rails
    assert len(rails) == 1 and not isinstance(rails[0], PERMISSION_GROUP_TYPES)
    with pytest.raises(ValueError):
        ensure_native_tool_authority([*rails, NativeResourceAuthorityRail()])


def test_team_profiles_include_authority_even_when_optional_permission_is_filtered():
    from jiuwenswarm.agents.swarm import config_specs, registry

    assert config_specs._COMMON_RAIL_NAMES.count(registry.RESOURCE_AUTHORITY) == 1
    assert config_specs._CODE_RAIL_NAMES.count(registry.RESOURCE_AUTHORITY) == 1
    from jiuwenswarm.agents.swarm.providers.builtin_rails import (
        NativeResourceAuthorityRail as registered,
    )

    assert registered is NativeResourceAuthorityRail


def test_core_general_purpose_clone_preserves_mandatory_rail():
    from openjiuwen.harness.factory import _inject_general_purpose_subagent

    rail = NativeResourceAuthorityRail()
    children = _inject_general_purpose_subagent(
        [],
        add_general_purpose_agent=True,
        resolved_language="en",
        rails=[rail],
        system_prompt="fixture",
        tools=[],
        mcps=[],
        model=None,
        skills=[],
    )
    assert rail in children[0].rails


@pytest.mark.parametrize("mode", ["team", "code.team", "team.plan.code"])
@pytest.mark.parametrize("role", ["leader", "teammate"])
@pytest.mark.parametrize("enabled", [True, False])
def test_compiled_team_spec_retains_final_authority(mode, role, enabled):
    from openjiuwen.agent_teams.schema.deep_agent_spec import DeepAgentSpec
    from jiuwenswarm.agents.swarm.config_specs import build_member_deep_agent_spec
    from jiuwenswarm.agents.swarm.registry import RESOURCE_AUTHORITY

    spec = build_member_deep_agent_spec(
        {"permissions": {"enabled": enabled}},
        mode,
        role,
        DeepAgentSpec(),
        enable_permissions=enabled,
    )
    assert [rail.type for rail in spec.rails].count(RESOURCE_AUTHORITY) == 1


@pytest.mark.asyncio
async def test_provider_authorities_are_copied_and_unknown_provider_denies():
    async def native(_):
        return True

    async def opencode(_):
        return True

    callbacks = {"opencode": opencode}
    with tool_authority_scope(native, provider_authorizers=callbacks):
        callbacks.clear()
        assert current_tool_authorizer() is native
        assert current_tool_authorizer("opencode") is opencode
        assert await current_tool_authorizer("codex")(None) is False
    assert current_tool_authorizer("codex") is None
    with tool_authority_scope(None, provider_authorizers={}):
        assert await current_tool_authorizer()(None) is False


def test_provider_authority_mapping_rejects_conflicting_or_invalid_values():
    async def allow(_):
        return True

    async def deny(_):
        return False

    with pytest.raises(ValueError):
        with tool_authority_scope(allow, provider_authorizers={"native": deny}):
            pass
    with pytest.raises(TypeError):
        with tool_authority_scope(None, provider_authorizers={"codex": None}):
            pass
