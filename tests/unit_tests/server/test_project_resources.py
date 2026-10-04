"""Resource grants use the existing sidecar and real temporary-file operations."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

from jiuwenswarm.governance.contracts import TrustedIdentity
from jiuwenswarm.governance.resources import (
    ResourceAccessDenied,
    ResourceDecision,
    ResourceDefinition,
    ResourceGuard,
    ResourceRequest,
)
from jiuwenswarm.server.runtime.session import project_store
from jiuwenswarm.server.runtime.session.project_access import (
    ProjectAccessDenied,
    ProjectAccessStore,
    ProjectRevisionConflict,
)


def identity(subject):
    return TrustedIdentity(subject, subject, "test-authentication")


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(project_store, "get_agent_root_dir", lambda: tmp_path)
    project_store.invalidate_cache()
    root = tmp_path / "workspace"
    root.mkdir()
    item = project_store.create_project("Private", str(root))
    store = ProjectAccessStore()
    store.initialize(item.project_id, "owner")
    store.replace_acl(
        item.project_id,
        "owner",
        acl={
            "bob": ["read", "execute"],
            "charlie": ["read", "execute"],
            "viewer": ["read"],
            "admin": ["admin", "read", "execute"],
        },
        expected_revision=1,
    )
    yield store, item.project_id, root
    project_store.invalidate_cache()


def install(store, pid, root, *, expires_at=None):
    return store.register_resource(
        pid,
        ResourceDefinition("workspace", "workspace", str(root)),
        owner_subject_id="owner",
        actions=("read", "write"),
        delegable=True,
        expires_at=expires_at,
        expected_revision=0,
    )


def test_execute_and_project_owner_do_not_imply_resources(setup):
    store, pid, root = setup
    request = ResourceRequest("workspace", "read", str(root / "secret.txt"))
    for who in ("owner", "bob", "admin"):
        assert store.authorize(pid, who, "execute").allowed
        assert not store.authorize_resource(pid, identity(who), request).allowed
    revision = install(store, pid, root)
    assert revision == 1
    assert store.authorize_resource(pid, identity("owner"), request).allowed
    assert not store.authorize_resource(pid, identity("bob"), request).allowed
    assert not store.authorize_resource(pid, identity("admin"), request).allowed
    with pytest.raises(ResourceAccessDenied):
        store.grant_resource(
            pid,
            identity("admin"),
            "workspace",
            subject_id="bob",
            actions=("read",),
            expected_revision=1,
        )


def test_path_scope_delegation_expiry_and_revision(setup, monkeypatch):
    store, pid, root = setup
    import jiuwenswarm.server.runtime.session.project_access as implementation

    monkeypatch.setattr(implementation.time, "time", lambda: 100.0)
    child = root / "child"
    child.mkdir()
    install(store, pid, root, expires_at=200.0)
    revision = store.grant_resource(
        pid,
        identity("owner"),
        "workspace",
        subject_id="bob",
        actions=("read",),
        scope=str(child),
        delegable=True,
        expected_revision=1,
    )
    assert revision == 2
    allowed = ResourceRequest("workspace", "read", str(child / "file"))
    assert store.authorize_resource(pid, identity("bob"), allowed).expires_at == 200.0
    for request in (
        ResourceRequest("workspace", "write", str(child / "file")),
        ResourceRequest("workspace", "read", str(root / "outside")),
        ResourceRequest("workspace", "read", str(child / ".." / "outside")),
        ResourceRequest("workspace", "read"),
    ):
        assert not store.authorize_resource(pid, identity("bob"), request).allowed
    outside = root.parent / "private"
    outside.mkdir()
    (child / "symlink").symlink_to(outside, target_is_directory=True)
    assert not store.authorize_resource(
        pid,
        identity("bob"),
        ResourceRequest("workspace", "read", str(child / "symlink" / "secret")),
    ).allowed
    with pytest.raises(ResourceAccessDenied):
        store.grant_resource(
            pid,
            identity("bob"),
            "workspace",
            subject_id="charlie",
            actions=("write",),
            expected_revision=2,
        )
    with pytest.raises(ResourceAccessDenied):
        store.grant_resource(
            pid,
            identity("bob"),
            "workspace",
            subject_id="charlie",
            actions=("read",),
            scope=str(root),
            expected_revision=2,
        )
    with pytest.raises(ResourceAccessDenied):
        store.grant_resource(
            pid,
            identity("bob"),
            "workspace",
            subject_id="charlie",
            actions=("read",),
            expires_at=201,
            expected_revision=2,
        )
    store.grant_resource(
        pid,
        identity("bob"),
        "workspace",
        subject_id="charlie",
        actions=("read",),
        expected_revision=2,
    )
    assert store.authorize_resource(pid, identity("charlie"), allowed).allowed
    with pytest.raises(ProjectRevisionConflict):
        store.revoke_resource(
            pid, identity("owner"), "workspace", subject_id="bob", expected_revision=2
        )
    monkeypatch.setattr(implementation.time, "time", lambda: 200.0)
    assert not store.authorize_resource(pid, identity("charlie"), allowed).allowed


def test_revoke_and_regrant_do_not_resurrect_children(setup):
    store, pid, root = setup
    install(store, pid, root)
    store.grant_resource(
        pid,
        identity("owner"),
        "workspace",
        subject_id="bob",
        actions=("read",),
        delegable=True,
        expected_revision=1,
    )
    store.grant_resource(
        pid,
        identity("bob"),
        "workspace",
        subject_id="charlie",
        actions=("read",),
        expected_revision=2,
    )
    request = ResourceRequest("workspace", "read", str(root / "file"))
    assert store.authorize_resource(pid, identity("charlie"), request).allowed
    store.revoke_resource(
        pid, identity("owner"), "workspace", subject_id="bob", expected_revision=3
    )
    assert not store.authorize_resource(pid, identity("charlie"), request).allowed
    store.grant_resource(
        pid,
        identity("owner"),
        "workspace",
        subject_id="bob",
        actions=("read",),
        delegable=True,
        expected_revision=4,
    )
    assert store.authorize_resource(pid, identity("bob"), request).allowed
    assert not store.authorize_resource(pid, identity("charlie"), request).allowed


def test_actor_and_execution_subject_both_need_resource_grants(setup):
    store, pid, root = setup
    install(store, pid, root)
    request = ResourceRequest("workspace", "read", str(root))
    assert not store.authorize_resource(
        pid, TrustedIdentity("owner", "bob", "host"), request
    ).allowed
    assert not store.authorize_resource(
        pid, TrustedIdentity("bob", "owner", "host"), request
    ).allowed
    store.grant_resource(
        pid,
        identity("owner"),
        "workspace",
        subject_id="bob",
        actions=("read",),
        expected_revision=1,
    )
    assert store.authorize_resource(
        pid, TrustedIdentity("bob", "owner", "host"), request
    ).allowed
    with pytest.raises(ResourceAccessDenied):
        store.grant_resource(
            pid,
            TrustedIdentity("bob", "owner", "host"),
            "workspace",
            subject_id="charlie",
            actions=("read",),
            expected_revision=2,
        )


def test_tool_credential_and_process_are_explicit_separate_resources(setup):
    store, pid, root = setup
    definitions = [
        ("tool", "invoke", "product.search"),
        ("credential", "use", "vault:owner/model"),
        ("process", "execute", "local-host"),
    ]
    revision = 0
    for kind, action, reference in definitions:
        revision = store.register_resource(
            pid,
            ResourceDefinition(kind, kind, reference),
            owner_subject_id="owner",
            actions=(action,),
            delegable=True,
            expected_revision=revision,
        )
    store.grant_resource(
        pid,
        identity("owner"),
        "tool",
        subject_id="bob",
        actions=("invoke",),
        expected_revision=revision,
    )
    assert store.authorize_resource(
        pid, identity("bob"), ResourceRequest("tool", "invoke")
    ).allowed
    assert not store.authorize_resource(
        pid, identity("bob"), ResourceRequest("credential", "use")
    ).allowed
    assert not store.authorize_resource(
        pid, identity("bob"), ResourceRequest("process", "execute")
    ).allowed
    assert not store.authorize_resource(
        pid, identity("owner"), ResourceRequest("process", "execute", str(root))
    ).allowed
    assert not store.authorize_resource(
        pid, identity("owner"), ResourceRequest("credential", "read")
    ).allowed
    assert not store.authorize_resource(
        pid, identity("owner"), ResourceRequest("unknown", "invoke")
    ).allowed
    assert "resource_access" not in store.get(pid, "viewer")
    assert "resource_access" not in store.get(pid, "admin")
    assert "vault:owner/model" not in json.dumps(
        store.resource_grants(pid, identity("bob"))
    )
    with pytest.raises(ProjectAccessDenied):
        store.resource_grants(pid, identity("viewer"))
    # User-editable extension data cannot overwrite the separate authority.
    store.update(pid, "owner", goal="", extensions={"resource_access": {"allow": "*"}})
    assert not store.authorize_resource(
        pid, identity("bob"), ResourceRequest("credential", "use")
    ).allowed


def test_revoke_changes_actual_preoperation_gate(setup):
    store, pid, root = setup
    install(store, pid, root)
    store.grant_resource(
        pid,
        identity("owner"),
        "workspace",
        subject_id="bob",
        actions=("write",),
        expected_revision=1,
    )
    path = root / "effects.txt"
    request = ResourceRequest("workspace", "write", str(path))
    with store.guard_resource(pid, identity("bob"), request):
        path.write_text("first")
    store.revoke_resource(
        pid, identity("owner"), "workspace", subject_id="bob", expected_revision=2
    )
    with pytest.raises(ResourceAccessDenied):
        with ProjectAccessStore().guard_resource(pid, identity("bob"), request):
            path.write_text("second")
    assert path.read_text() == "first"


def test_cross_process_revoke_is_immediate(setup):
    store, pid, root = setup
    install(store, pid, root)
    store.grant_resource(
        pid,
        identity("owner"),
        "workspace",
        subject_id="bob",
        actions=("read",),
        expected_revision=1,
    )
    script = """
import sys
from pathlib import Path
from jiuwenswarm.governance.contracts import TrustedIdentity
from jiuwenswarm.server.runtime.session import project_store
from jiuwenswarm.server.runtime.session.project_access import ProjectAccessStore
project_store.get_agent_root_dir = lambda: Path(sys.argv[1])
ProjectAccessStore().revoke_resource(sys.argv[2], TrustedIdentity('owner','owner','host'), 'workspace', subject_id='bob', expected_revision=2)
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(store.path.parent), pid],
        capture_output=True,
        text=True,
        timeout=30,
        env=os.environ.copy(),
    )
    assert result.returncode == 0, result.stderr
    decision = store.authorize_resource(
        pid, identity("bob"), ResourceRequest("workspace", "read", str(root))
    )
    assert not decision.allowed and decision.resource_revision == 3


@pytest.mark.parametrize(
    "damage",
    ["missing", "schema", "cycle", "parent", "revision", "nan", "bool_parent_revision"],
)
def test_corrupt_authority_and_deleted_projects_fail_closed(setup, damage):
    store, pid, root = setup
    install(store, pid, root)
    store.grant_resource(
        pid,
        identity("owner"),
        "workspace",
        subject_id="bob",
        actions=("read",),
        expected_revision=1,
    )
    data = store._load()
    state = data["projects"][pid]["resource_access"]
    bob = state["grants"]["workspace"]["bob"]
    if damage == "missing":
        data["projects"][pid].pop("resource_access")
    elif damage == "schema":
        state["schema_version"] = 99
    elif damage == "cycle":
        bob["parent"] = "bob"
    elif damage == "parent":
        bob["parent"] = "unknown"
    elif damage == "revision":
        state["revision"] = True
    elif damage == "nan":
        bob["expires_at"] = float("nan")
    else:
        bob["parent_revision"] = True
    store.path.write_text(json.dumps(data))
    assert not store.authorize_resource(
        pid, identity("bob"), ResourceRequest("workspace", "read", str(root))
    ).allowed


def test_delegation_cycles_non_delegable_and_deleted_project(setup):
    store, pid, root = setup
    install(store, pid, root)
    store.grant_resource(
        pid,
        identity("owner"),
        "workspace",
        subject_id="bob",
        actions=("read",),
        delegable=True,
        expected_revision=1,
    )
    with pytest.raises(ResourceAccessDenied):
        store.grant_resource(
            pid,
            identity("bob"),
            "workspace",
            subject_id="owner",
            actions=("read",),
            expected_revision=2,
        )
    store.grant_resource(
        pid,
        identity("bob"),
        "workspace",
        subject_id="charlie",
        actions=("read",),
        expected_revision=2,
    )
    with pytest.raises(ResourceAccessDenied):
        store.grant_resource(
            pid,
            identity("charlie"),
            "workspace",
            subject_id="viewer",
            actions=("read",),
            expected_revision=3,
        )
    project_store.delete_project(pid)
    assert not store.authorize_resource(
        pid, identity("owner"), ResourceRequest("workspace", "read", str(root))
    ).allowed


def test_alternative_authorizer_is_called_each_time_and_cannot_confuse_binding():
    request = ResourceRequest("search", "invoke")
    who = identity("bob")

    class Policy:
        allowed = True
        calls = 0

        def authorize_resource(self, project_id, identity, resource_request):
            self.calls += 1
            return ResourceDecision(
                self.allowed,
                project_id,
                identity.actor_id,
                identity.subject_id,
                resource_request,
                1,
                self.calls,
                reference="product.search",
            )

    policy = Policy()
    guard = ResourceGuard(policy)
    assert guard.check("project", who, request).resource_revision == 1
    policy.allowed = False
    with pytest.raises(ResourceAccessDenied):
        guard.check("project", who, request)
    assert policy.calls == 2
    mismatched = ResourceDecision(True, "other-project", "bob", "bob", request, 1, 1)
    with pytest.raises(ResourceAccessDenied):
        ResourceGuard(
            SimpleNamespace(authorize_resource=lambda *args: mismatched)
        ).check("project", who, request)


@pytest.mark.parametrize(
    "field,value",
    [
        ("allowed", "yes"),
        ("actor_id", "owner"),
        ("subject_id", "owner"),
        ("acl_revision", True),
        ("resource_revision", -1),
        ("request", ResourceRequest("another", "invoke")),
    ],
)
def test_alternative_authorizer_malformed_decisions_fail_closed(field, value):
    request = ResourceRequest("search", "invoke")
    decision = ResourceDecision(True, "project", "bob", "bob", request, 1, 1)
    object.__setattr__(decision, field, value)
    policy = SimpleNamespace(authorize_resource=lambda *args: decision)
    with pytest.raises(ResourceAccessDenied):
        ResourceGuard(policy).check("project", identity("bob"), request)


def test_alternative_authorizer_error_and_expiry_fail_closed():
    def broken(*args):
        raise RuntimeError("backend down")

    request = ResourceRequest("search", "invoke")
    with pytest.raises(ResourceAccessDenied):
        ResourceGuard(SimpleNamespace(authorize_resource=broken)).check(
            "project", identity("bob"), request
        )
    expired = ResourceDecision(
        True, "project", "bob", "bob", request, 1, 1, expires_at=10
    )
    with pytest.raises(ResourceAccessDenied):
        ResourceGuard(
            SimpleNamespace(authorize_resource=lambda *args: expired), clock=lambda: 10
        ).check("project", identity("bob"), request)


def test_definition_and_lifetime_validation(setup):
    store, pid, root = setup
    with pytest.raises(ValueError):
        ResourceDefinition("credential", "credential", "Authorization: Bearer secret")
    with pytest.raises(ValueError):
        ResourceDefinition("workspace", "workspace", "relative/path")
    with pytest.raises(ValueError):
        store.register_resource(
            pid,
            ResourceDefinition("tool", "tool", "product.search"),
            owner_subject_id="owner",
            actions=("invoke",),
            expected_revision=0,
            expires_at=float("inf"),
        )


@pytest.mark.parametrize(
    "overrides", [{"reference": None}, {"scope": "/private"}, {"resource_revision": 0}]
)
def test_alternative_authorizer_cannot_allow_missing_reference_or_wrong_scope(
    overrides,
):
    request = ResourceRequest("workspace", "read", "/shared/file")
    data = dict(
        allowed=True,
        project_id="project",
        actor_id="bob",
        subject_id="bob",
        request=request,
        acl_revision=1,
        resource_revision=1,
        reference="/shared",
        scope="/shared",
    )
    data.update(overrides)
    decision = ResourceDecision(**data)
    with pytest.raises(ResourceAccessDenied):
        ResourceGuard(SimpleNamespace(authorize_resource=lambda *args: decision)).check(
            "project", identity("bob"), request
        )


def test_real_store_guard_rechecks_project_execute_revoke(setup):
    store, pid, root = setup
    install(store, pid, root)
    store.grant_resource(
        pid,
        identity("owner"),
        "workspace",
        subject_id="bob",
        actions=("write",),
        expected_revision=1,
    )
    guard = ResourceGuard(store)
    request = ResourceRequest("workspace", "write", str(root / "file"))
    assert guard.check(pid, identity("bob"), request).allowed
    store.replace_acl(pid, "owner", acl={"bob": ["read"]}, expected_revision=2)
    with pytest.raises(ResourceAccessDenied):
        guard.check(pid, identity("bob"), request)


def test_root_reissue_never_resurrects_old_delegation(setup):
    store, pid, root = setup
    install(store, pid, root)
    store.grant_resource(
        pid,
        identity("owner"),
        "workspace",
        subject_id="bob",
        actions=("read",),
        expected_revision=1,
    )
    store.revoke_resource(
        pid, identity("owner"), "workspace", subject_id="owner", expected_revision=2
    )
    store.register_resource(
        pid,
        ResourceDefinition("workspace", "workspace", str(root)),
        owner_subject_id="owner",
        actions=("read", "write"),
        delegable=True,
        expected_revision=3,
    )
    assert store.authorize_resource(
        pid, identity("owner"), ResourceRequest("workspace", "read", str(root))
    ).allowed
    assert not store.authorize_resource(
        pid, identity("bob"), ResourceRequest("workspace", "read", str(root))
    ).allowed
