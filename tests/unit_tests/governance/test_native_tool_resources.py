"""Real Native executor/ACL resource mapping, including denied side effects."""

import json
from types import SimpleNamespace

import pytest
from openjiuwen.core.foundation.llm import ToolCall
from openjiuwen.core.session import _current_session
from openjiuwen.core.single_agent.ability_manager import AbilityManager
from openjiuwen.core.single_agent.rail.base import (
    AgentCallbackContext,
    AgentCallbackEvent,
)
from openjiuwen.core.sys_operation.config import LocalWorkConfig
from openjiuwen.core.sys_operation.cwd import _cwd_state, init_cwd
from openjiuwen.core.sys_operation.sys_operation import SysOperation, SysOperationCard
from openjiuwen.harness.tools import BashTool
from openjiuwen.harness.tools.filesystem import (
    ReadFileTool,
    WriteFileTool,
    EditFileTool,
)
from openjiuwen.harness_protocol import BeforeToolContext

from jiuwenswarm.agents.harness.common.rails.permissions.resource_authority_rail import (
    NativeResourceAuthorityRail,
)
from jiuwenswarm.governance.contracts import TrustedIdentity
from jiuwenswarm.governance.native_tool_resources import NativeToolResourceResolver
from jiuwenswarm.governance.resources import ResourceDefinition
from jiuwenswarm.governance.tool_context import tool_authority_scope
from jiuwenswarm.governance.tool_resources import (
    BoundToolResourceAuthority,
    ResourceExecutionContext,
)
from jiuwenswarm.server.runtime.session import project_store
from jiuwenswarm.server.runtime.session.project_access import ProjectAccessStore


@pytest.fixture
def native(tmp_path, monkeypatch):
    monkeypatch.setenv("JIUWENSWARM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(
        project_store, "get_agent_root_dir", lambda: tmp_path / "projects"
    )
    project_store.invalidate_cache()
    work = tmp_path / "work"
    work.mkdir()
    (work / "source.txt").write_text("RESOURCE-MARKER")
    pid = project_store.create_project("Fixture", str(work)).project_id
    store = ProjectAccessStore()
    store.initialize(pid, "owner")
    identity = TrustedIdentity("owner", "owner", "fixture")
    revision = 0

    def grant(kind, reference, actions):
        nonlocal revision
        rid = f"resource-{revision}"
        revision = store.register_resource(
            pid,
            ResourceDefinition(rid, kind, reference),
            owner_subject_id="owner",
            actions=actions,
            expected_revision=revision,
        )
        return rid

    workspace_resource = grant("workspace", str(work), ("read", "write"))
    for name in ("read_file", "write_file", "edit_file", "bash"):
        grant("tool", "native:" + name, ("invoke",))
    operation = SysOperation(
        SysOperationCard(
            id=f"resource-{tmp_path.name}",
            work_config=LocalWorkConfig(sandbox_root=[str(tmp_path)]),
        )
    )
    manager = AbilityManager(owner_id=f"resource-{tmp_path.name}")
    tools = {
        name: cls(operation, agent_id=tmp_path.name)
        for name, cls in (
            ("read_file", ReadFileTool),
            ("write_file", WriteFileTool),
            ("edit_file", EditFileTool),
            ("bash", BashTool),
        )
    }
    for tool in tools.values():
        manager.add_ability(tool.card, tool)
    session = SimpleNamespace(
        get_session_id=lambda: "root",
        get_agent_id=lambda: "root-agent",
        get_state=lambda *_args, **_kwargs: None,
    )
    rail = NativeResourceAuthorityRail()

    class Callbacks:
        async def execute(self, event, ctx):
            if event is AgentCallbackEvent.BEFORE_TOOL_CALL:
                await rail.before_tool_call(ctx)

    agent = SimpleNamespace(
        card=SimpleNamespace(id="root-agent", name="root-agent"),
        ability_manager=manager,
        agent_callback_manager=Callbacks(),
    )
    execution = ResourceExecutionContext(pid, identity, "root", str(work), "native")

    def policy():
        return BoundToolResourceAuthority(
            execution,
            authorizer=store,
            resolver=NativeToolResourceResolver(
                store.resource_grants(pid, identity),
                owns_session=lambda _execution, actual_agent, actual_session: (
                    actual_agent is agent and actual_session is session
                ),
            ),
            current_identity=lambda: identity,
            is_current_execution=lambda: True,
        )

    async def invoke(name, args, *, authority=None, actual_session=None):
        with tool_authority_scope(authority or policy()):
            return await manager.execute(
                AgentCallbackContext(agent=agent),
                ToolCall(
                    id="call", type="function", name=name, arguments=json.dumps(args)
                ),
                session=actual_session or session,
            )

    token = _cwd_state.set(None)
    init_cwd(str(work), workspace=str(work), project_root=str(work))
    try:
        yield SimpleNamespace(
            work=work,
            tmp=tmp_path,
            store=store,
            pid=pid,
            identity=identity,
            workspace_resource=workspace_resource,
            grant=grant,
            policy=policy,
            invoke=invoke,
            session=session,
            agent=agent,
            manager=manager,
            operation=operation,
            tools=tools,
        )
    finally:
        manager.teardown_tools()
        _cwd_state.reset(token)
        project_store.invalidate_cache()


@pytest.mark.asyncio
async def test_real_read_then_revocation_rechecks_fixed_catalog(native):
    policy = native.policy()
    result = await native.invoke(
        "read_file", {"file_path": "source.txt"}, authority=policy
    )
    assert "RESOURCE-MARKER" in str(result)
    revision = native.store.resource_grants(native.pid, native.identity)[
        "resource_revision"
    ]
    native.store.revoke_resource(
        native.pid,
        native.identity,
        native.workspace_resource,
        subject_id="owner",
        expected_revision=revision,
    )
    denied = await native.invoke(
        "read_file", {"file_path": "source.txt"}, authority=policy
    )
    assert "RESOURCE-MARKER" not in str(denied) and "PERMISSION_DENIED" in str(denied)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind", ["outside", "symlink", "nested_payload", "unknown_field"]
)
async def test_file_paths_and_schema_fail_closed(native, kind):
    outside = native.tmp / "private.txt"
    outside.write_text("OUTSIDE-SECRET")
    (native.work / "link").symlink_to(outside)
    args = {
        "file_path": {
            "outside": str(outside),
            "symlink": "link",
            "nested_payload": '{"file_path":"source.txt"}',
            "unknown_field": "source.txt",
        }[kind]
    }
    if kind == "unknown_field":
        args["redirect"] = str(outside)
    result = await native.invoke("read_file", args)
    assert "PERMISSION_DENIED" in str(result)
    assert "OUTSIDE-SECRET" not in str(result)


@pytest.mark.asyncio
async def test_same_name_subclass_is_not_a_native_executor(native):
    class Impostor(ReadFileTool):
        pass

    other = Impostor(native.operation, agent_id="impostor")
    native.manager.remove_ability("read_file")
    native.manager.add_ability(other.card, other)
    assert "PERMISSION_DENIED" in str(
        await native.invoke("read_file", {"file_path": "source.txt"})
    )


@pytest.mark.asyncio
async def test_child_or_other_agent_cannot_consume_root_subject(native):
    child = SimpleNamespace(
        get_session_id=lambda: "root:child", get_state=lambda *_args: None
    )
    assert "PERMISSION_DENIED" in str(
        await native.invoke(
            "read_file", {"file_path": "source.txt"}, actual_session=child
        )
    )
    same_id_other_session = SimpleNamespace(
        get_session_id=lambda: "root", get_state=lambda *_args: None
    )
    assert "PERMISSION_DENIED" in str(
        await native.invoke(
            "read_file",
            {"file_path": "source.txt"},
            actual_session=same_id_other_session,
        )
    )


@pytest.mark.asyncio
async def test_missing_executor_scope_cannot_forge_native_name(native):
    tool = BeforeToolContext(
        "root-agent",
        "root",
        None,
        "call",
        "read_file",
        {"file_path": str(native.work / "source.txt")},
    )
    assert await native.policy()(tool) is False


@pytest.mark.asyncio
async def test_write_requires_explicit_audit_resource(native):
    target = native.work / "new.txt"
    token = _current_session.set(native.session)
    try:
        denied = await native.invoke(
            "write_file", {"file_path": str(target), "content": "NEW"}
        )
        assert "PERMISSION_DENIED" in str(denied) and not target.exists()
        history = native.tmp / "data" / ".agent_history"
        native.grant("workspace", str(history), ("read", "write"))
        result = await native.invoke(
            "write_file", {"file_path": str(target), "content": "NEW"}
        )
        assert target.read_text() == "NEW", result
        assert (history / "file_ops_root-agent_root.json").is_file()
    finally:
        _current_session.reset(token)


@pytest.mark.asyncio
async def test_shell_requires_process_grant_and_does_not_claim_path_sandbox(native):
    outside = native.tmp / "shell-secret.txt"
    outside.write_text("PROCESS-SCOPE-MARKER")
    args = {"command": f"cat '{outside}'", "timeout": 5}
    assert "PERMISSION_DENIED" in str(await native.invoke("bash", args))
    native.grant("process", "native:local-process", ("execute",))
    assert "PROCESS-SCOPE-MARKER" in str(await native.invoke("bash", args))
    assert "PERMISSION_DENIED" in str(
        await native.invoke("bash", {**args, "run_in_background": True})
    )


@pytest.mark.asyncio
async def test_edit_requires_audit_and_preserves_old_history(native):
    history = native.tmp / "data" / ".agent_history"
    native.grant("workspace", str(history), ("read", "write"))
    token = _current_session.set(native.session)
    try:
        await native.invoke("read_file", {"file_path": "source.txt"})
        result = await native.invoke(
            "edit_file",
            {
                "file_path": "source.txt",
                "old_string": "RESOURCE",
                "new_string": "EDITED",
            },
        )
        assert (native.work / "source.txt").read_text() == "EDITED-MARKER", result
        assert (history / "file_ops_root-agent_root.json").is_file()
    finally:
        _current_session.reset(token)


@pytest.mark.asyncio
async def test_post_rail_redirect_is_rechecked_and_legacy_still_transforms(native):
    from openjiuwen.core.runner import Runner
    from openjiuwen.core.runner.callback.events import ToolCallEvents

    outside = native.tmp / "transform-secret.txt"
    outside.write_text("TRANSFORM-SECRET")
    transformed = []

    async def redirect(*args, **kwargs):
        transformed.append(True)
        return ((), {**kwargs, "inputs": {"file_path": str(outside)}})

    framework = Runner.callback_framework
    await framework.register(
        ToolCallEvents.TOOL_INVOKE_INPUT, redirect, callback_type="transform"
    )
    try:
        denied = await native.invoke("read_file", {"file_path": "source.txt"})
        assert "PERMISSION_DENIED" in str(denied) and transformed
        assert "TRANSFORM-SECRET" not in str(denied)
        with tool_authority_scope(None):
            legacy = await native.manager.execute(
                AgentCallbackContext(agent=native.agent),
                ToolCall(
                    id="legacy",
                    type="function",
                    name="read_file",
                    arguments=json.dumps({"file_path": "source.txt"}),
                ),
                session=native.session,
            )
        assert "TRANSFORM-SECRET" in str(legacy) and transformed
    finally:
        await framework.unregister(ToolCallEvents.TOOL_INVOKE_INPUT, redirect)


@pytest.mark.asyncio
async def test_same_class_replaced_invoke_is_not_proven(native):
    from functools import wraps

    original = native.tools["read_file"].invoke
    called = []

    @wraps(original)
    async def replacement(*args, **kwargs):
        called.append(True)
        return await original(*args, **kwargs)

    native.tools["read_file"].invoke = replacement
    result = await native.invoke("read_file", {"file_path": "source.txt"})
    assert "PERMISSION_DENIED" in str(result) and not called


@pytest.mark.asyncio
async def test_executor_mutation_while_authorizing_rejected(native):
    policy = native.policy()
    original = native.tools["read_file"].invoke

    async def changing(tool):
        assert await policy(tool) is True

        async def replacement(*args, **kwargs):
            return await original(*args, **kwargs)

        native.tools["read_file"].invoke = replacement
        return True

    result = await native.invoke(
        "read_file", {"file_path": "source.txt"}, authority=changing
    )
    assert "PERMISSION_DENIED" in str(result)


@pytest.mark.asyncio
async def test_executor_proof_expires_even_in_copied_context(native):
    from contextvars import copy_context
    from jiuwenswarm.governance.native_executor import require_native_executor

    saved = []
    policy = native.policy()

    async def capture(tool):
        assert await policy(tool) is True
        saved.append((copy_context(), tool))
        return True

    result = await native.invoke(
        "read_file", {"file_path": "source.txt"}, authority=capture
    )
    assert "RESOURCE-MARKER" in str(result)
    context, tool = saved[0]
    with pytest.raises(ValueError, match="proof is required"):
        context.run(require_native_executor, tool)


@pytest.mark.asyncio
async def test_backend_mode_change_during_authorization_is_denied(native):
    from openjiuwen.core.sys_operation import OperationMode

    policy = native.policy()

    async def changing(tool):
        assert await policy(tool) is True
        native.operation.mode = OperationMode.SANDBOX
        return True

    result = await native.invoke(
        "read_file", {"file_path": "source.txt"}, authority=changing
    )
    assert "PERMISSION_DENIED" in str(result)


@pytest.mark.asyncio
async def test_legal_transform_uses_actual_final_operation_and_executor(native):
    from openjiuwen.core.foundation.tool import current_tool_invocation
    from openjiuwen.core.runner import Runner
    from openjiuwen.core.runner.callback.events import ToolCallEvents

    target = native.work / "transformed.txt"
    target.write_text("LEGAL-TRANSFORM-MARKER")
    seen = []
    policy = native.policy()

    async def transform(*args, **kwargs):
        return (), {**kwargs, "inputs": {"file_path": str(target)}}

    async def authority(tool):
        actual = current_tool_invocation()
        seen.append((actual is not None, tool.arguments["file_path"]))
        if actual is not None:
            assert tool is actual.operation
            assert actual.executor is native.tools["read_file"]
            assert actual.original_invoke.__func__ is ReadFileTool.invoke
        return await policy(tool)

    framework = Runner.callback_framework
    await framework.register(
        ToolCallEvents.TOOL_INVOKE_INPUT, transform, callback_type="transform"
    )
    try:
        result = await native.invoke(
            "read_file", {"file_path": "source.txt"}, authority=authority
        )
        assert "LEGAL-TRANSFORM-MARKER" in str(result)
        assert seen == [
            (False, "source.txt"),
            (False, "source.txt"),
            (True, str(target)),
        ]
    finally:
        await framework.unregister(ToolCallEvents.TOOL_INVOKE_INPUT, transform)


@pytest.mark.asyncio
async def test_final_bridge_cannot_fall_back_to_early_proof(native, monkeypatch):
    from jiuwenswarm.agents.harness.common.rails.permissions import (
        resource_authority_rail,
    )

    monkeypatch.setattr(
        resource_authority_rail, "current_tool_invocation", lambda: None
    )
    result = await native.invoke("read_file", {"file_path": "source.txt"})
    assert "PERMISSION_DENIED" in str(result) and "RESOURCE-MARKER" not in str(result)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change", ["cwd", "workspace", "backend", "mode", "ambient_session", "owner"]
)
async def test_final_policy_context_mutation_denies_before_side_effect(
    native, monkeypatch, change
):
    from openjiuwen.core.foundation.tool import current_tool_invocation
    from openjiuwen.core.sys_operation import OperationMode
    from openjiuwen.core.sys_operation.cwd import set_cwd, set_workspace

    policy = native.policy()
    authorize = native.store.authorize_resource
    mutated = []

    def changing(project_id, identity, request):
        decision = authorize(project_id, identity, request)
        if current_tool_invocation() is not None and not mutated:
            mutated.append(True)
            if change == "cwd":
                set_cwd(str(native.tmp))
            elif change == "workspace":
                set_workspace(str(native.tmp))
            elif change == "backend":
                native.tools["read_file"].operation = object()
            elif change == "mode":
                native.operation.mode = OperationMode.SANDBOX
            elif change == "ambient_session":
                _current_session.set(
                    SimpleNamespace(get_session_id=lambda: "different")
                )
            else:
                policy._resolver._owns_session = lambda *_: False
        return decision

    monkeypatch.setattr(native.store, "authorize_resource", changing)
    result = await native.invoke(
        "read_file", {"file_path": "source.txt"}, authority=policy
    )
    assert mutated and "PERMISSION_DENIED" in str(result)
    assert "RESOURCE-MARKER" not in str(result)


@pytest.mark.asyncio
async def test_final_call_cannot_borrow_new_request_authority_after_transform(native):
    from openjiuwen.core.runner import Runner
    from openjiuwen.core.runner.callback.events import ToolCallEvents
    from jiuwenswarm.governance.tool_context import native_authority_source_scope

    newer_calls = []
    async def newer(_):
        newer_calls.append(True)
        return True
    selected = [native.policy()]
    async def change_request(*args, **kwargs):
        selected[0] = newer
        return args, kwargs
    framework = Runner.callback_framework
    await framework.register(ToolCallEvents.TOOL_INVOKE_INPUT, change_request, callback_type="transform")
    try:
        with native_authority_source_scope(lambda: selected[0]):
            result = await native.invoke("read_file", {"file_path": "source.txt"})
        assert "PERMISSION_DENIED" in str(result)
        assert "RESOURCE-MARKER" not in str(result)
        assert not newer_calls
    finally:
        await framework.unregister(ToolCallEvents.TOOL_INVOKE_INPUT, change_request)
