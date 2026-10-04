"""Original OpenCode pre-I/O inputs and live resource decisions."""

from dataclasses import replace
from types import SimpleNamespace

import pytest
from openjiuwen.harness_protocol import BeforeToolContext

from jiuwenswarm.governance.contracts import TrustedIdentity
from jiuwenswarm.governance.opencode_tool_resources import OpenCodeToolResourceResolver
from jiuwenswarm.governance.resources import ResourceDefinition
from jiuwenswarm.governance.tool_resources import (
    BoundToolResourceAuthority,
    ResourceExecutionContext,
)
from jiuwenswarm.server.runtime.session import project_store
from jiuwenswarm.server.runtime.session.project_access import ProjectAccessStore


def operation(**changes):
    return replace(
        BeforeToolContext(
            "agent",
            "ses-owned",
            "turn",
            "call",
            "bash",
            {"command": "printf fixture", "description": "fixture"},
        ),
        **changes,
    )


@pytest.fixture
def host(tmp_path, monkeypatch):
    monkeypatch.setenv("JIUWENSWARM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(
        project_store, "get_agent_root_dir", lambda: tmp_path / "projects"
    )
    project_store.invalidate_cache()
    work = tmp_path / "work"
    work.mkdir()
    pid = project_store.create_project("OpenCode fixture", str(work)).project_id
    store = ProjectAccessStore()
    store.initialize(pid, "owner")
    identity = TrustedIdentity("owner", "owner", "fixture")
    for revision, (rid, kind, reference, actions) in enumerate(
        (
            ("tool", "tool", "opencode:bash", ("invoke",)),
            ("process", "process", "opencode:local-process", ("execute",)),
        )
    ):
        store.register_resource(
            pid,
            ResourceDefinition(rid, kind, reference),
            owner_subject_id="owner",
            actions=actions,
            expected_revision=revision,
        )
    execution = ResourceExecutionContext(
        pid, identity, "host-root", str(work), "opencode"
    )
    catalog = store.resource_grants(pid, identity)
    current = {"live": True, "identity": identity}
    resolver = OpenCodeToolResourceResolver(
        catalog,
        owns_session=lambda actual, sid: (
            actual is execution and sid == "ses-owned" and current["live"]
        ),
    )
    authority = BoundToolResourceAuthority(
        execution,
        authorizer=store,
        resolver=resolver,
        current_identity=lambda: current["identity"],
        is_current_execution=lambda: current["live"],
    )
    yield SimpleNamespace(**locals())
    project_store.invalidate_cache()


def test_process_grant_is_explicit_and_not_a_workspace_sandbox(host):
    uses = host.resolver.resources_for_tool(
        host.execution,
        operation(
            arguments={
                "command": "cat /outside/private; curl https://example.invalid",
                "description": "explicit whole process grant",
            }
        ),
    )
    assert [(use.reference, use.request.action, use.request.path) for use in uses] == [
        ("opencode:bash", "invoke", None),
        ("opencode:local-process", "execute", None),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("rid", ["tool", "process"])
async def test_current_store_revocation_denies_with_fixed_catalog(host, rid):
    assert await host.authority(operation()) is True
    host.store.revoke_resource(
        host.pid, host.identity, rid, subject_id="owner", expected_revision=2
    )
    assert await host.authority(operation()) is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name,metadata,patterns",
    [
        ("read", {}, ["tmp/work/file"]),
        ("edit", {"filepath": "/tmp/work/file", "diff": "a diff"}, ["tmp/work/file"]),
        ("write", {"filePath": "/tmp/work/file", "content": "data"}, ["tmp/work/file"]),
        ("glob", {"pattern": "*", "path": "/tmp/work"}, ["*"]),
        ("grep", {"pattern": "secret", "path": "/tmp/work"}, ["secret"]),
        (
            "webfetch",
            {"url": "https://example.invalid", "format": "text"},
            ["https://example.invalid"],
        ),
        ("external_directory", {}, ["/tmp/outside/*"]),
        ("task", {}, ["general"]),
        ("unknown", {}, ["*"]),
    ],
)
async def test_unproven_operations_do_not_infer_resources(
    host, name, metadata, patterns
):
    assert (
        await host.authority(
            operation(
                tool_name=name,
                arguments={
                    "metadata": metadata,
                    "patterns": patterns,
                },
            )
        )
        is False
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "args",
    [
        {},
        {"metadata": {"command": "true"}},
        {"patterns": ["true"]},
        {"metadata": {"command": "true", "workdir": "/tmp"}, "patterns": ["true"]},
        {"metadata": {"command": "true"}, "patterns": ["true"], "executor": "bash"},
        {"metadata": {"command": ""}, "patterns": ["true"]},
        {"metadata": {"command": "a\x00b"}, "patterns": ["true"]},
        {"metadata": {"command": True}, "patterns": ["true"]},
        {"metadata": {"command": "true"}, "patterns": []},
        {"metadata": {"command": "true"}, "patterns": [None]},
        {"metadata": {"command": "true"}, "patterns": "true"},
    ],
)
async def test_unknown_or_empty_schema_denies(host, args):
    assert await host.authority(operation(arguments=args)) is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [
        ("provider_session_id", "host-root"),
        ("provider_session_id", "ses-owned-child"),
        ("provider_session_id", None),
        ("turn_id", None),
        ("call_id", ""),
    ],
)
async def test_exact_provider_session_and_call_required(host, field, value):
    assert await host.authority(operation(**{field: value})) is False


def test_catalog_is_copied_and_missing_process_never_implies_grant(host):
    host.catalog["resources"].clear()
    assert len(host.resolver.resources_for_tool(host.execution, operation())) == 2
    resolver = OpenCodeToolResourceResolver(host.catalog, owns_session=lambda *_: True)
    with pytest.raises(ValueError, match="unavailable"):
        resolver.resources_for_tool(host.execution, operation())


@pytest.mark.asyncio
async def test_current_identity_and_owner_are_rechecked(host):
    host.current["identity"] = TrustedIdentity("other", "other", "fixture")
    assert await host.authority(operation()) is False
    host.current["identity"] = host.identity
    host.current["live"] = False
    assert await host.authority(operation()) is False


def test_owner_must_return_exact_true_and_survive_mapping(host):
    answers = iter([True, False])
    for owns in (lambda *_: 1, lambda *_: next(answers)):
        resolver = OpenCodeToolResourceResolver(host.catalog, owns_session=owns)
        with pytest.raises(ValueError, match="Session"):
            resolver.resources_for_tool(host.execution, operation())


def test_ambiguous_reference_fails_construction():
    with pytest.raises(ValueError, match="ambiguous"):
        OpenCodeToolResourceResolver(
            {
                "resources": [
                    {"resource_id": rid, "kind": "tool", "reference": "opencode:bash"}
                    for rid in ("a", "b")
                ]
            },
            owns_session=lambda *_: True,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("tool,actions", [("read", ("read",)), ("write", ("read", "write")), ("edit", ("read", "write"))])
async def test_original_file_inputs_require_current_tool_and_workspace_grants(host, tool, actions):
    host.store.register_resource(host.pid, ResourceDefinition("file-tool", "tool", "opencode:" + tool),
                                 owner_subject_id="owner", actions=("invoke",), expected_revision=2)
    host.store.register_resource(host.pid, ResourceDefinition("workspace", "workspace", str(host.work)),
                                 owner_subject_id="owner", actions=("read", "write"), expected_revision=3)
    host.resolver = OpenCodeToolResourceResolver(host.store.resource_grants(host.pid, host.identity),
                                               owns_session=lambda *_: True)
    file = host.work / "fixture.txt"
    args = {"filePath": str(file)}
    if tool == "write":
        args["content"] = "new"
    if tool == "edit":
        args.update(oldString="old", newString="new")
    call = operation(tool_name=tool, arguments=args)
    uses = host.resolver.resources_for_tool(host.execution, call)
    assert [(u.request.action, u.request.path) for u in uses] == [("invoke", None), *[(a, str(file)) for a in actions]]
    authority = BoundToolResourceAuthority(host.execution, authorizer=host.store, resolver=host.resolver,
                                          current_identity=lambda: host.identity, is_current_execution=lambda: True)
    assert await authority(call) is True
    host.store.revoke_resource(host.pid, host.identity, "workspace", subject_id="owner", expected_revision=4)
    assert await authority(call) is False
    outside = host.tmp_path / "outside"
    outside.mkdir()
    link = host.work / "link"
    link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="outside"):
        host.resolver.resources_for_tool(host.execution, operation(tool_name=tool, arguments={**args, "filePath": str(link / "file")}))


@pytest.mark.asyncio
@pytest.mark.parametrize("args", [
    {"command": "true", "description": "ok", "timeout": True},
    {"command": "true", "description": "ok", "extra": "forged"},
    {"command": "", "description": "ok"},
    {"command": "true", "description": "bad\\x00value".replace("\\x00", "\x00")},
])
async def test_invalid_original_schema_denies(host, args):
    assert await host.authority(operation(arguments=args)) is False
