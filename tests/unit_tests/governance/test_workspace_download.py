"""Real organization principals, Session sidecar and Workspace download IO."""

import json
import os
import time
from dataclasses import FrozenInstanceError, replace
from types import SimpleNamespace

import pytest

from jiuwenswarm.governance.resources import ResourceDefinition
from jiuwenswarm.governance.workspace_download import (
    ARTIFACT_NAMESPACE,
    MAX_DOWNLOAD_CHUNK_BYTES,
    WorkspaceArtifactIssuer,
    WorkspaceDownloadDenied,
    WorkspaceDownloadPermit,
)
from jiuwenswarm.agents.harness.common.tools.web_file_download import (
    WebFileDownloadManager,
)
from jiuwenswarm.server.runtime.session import lifecycle, project_store
from jiuwenswarm.server.runtime.session.project_access import ProjectAccessStore
from jiuwenswarm.server.runtime.session.sharing_host import SharingHostService
from tests.unit_tests.governance.test_organization_auth import principal
from tests.unit_tests.governance import test_organization_auth as authentication_tests

credentials = authentication_tests.credentials


@pytest.fixture
def setup(credentials, tmp_path, monkeypatch):
    auth, tokens, config_path = credentials
    monkeypatch.setattr(project_store, "get_agent_root_dir", lambda: tmp_path)
    monkeypatch.setattr(
        lifecycle, "get_agent_sessions_dir", lambda: tmp_path / "sessions"
    )
    project_store.invalidate_cache()
    root = tmp_path / "workspace"
    root.mkdir()
    project = project_store.create_project("both owners", str(root))
    access = ProjectAccessStore()
    access.initialize(project.project_id, "admin")
    access.replace_acl(
        project.project_id,
        "admin",
        acl={actor: ["read", "execute", "admin"] for actor in ("alice", "bob")},
        expected_revision=1,
    )
    alice, bob = (principal(auth, tokens, actor) for actor in ("alice", "bob"))
    host = SharingHostService(
        auth.resolve_actor, known_actor=auth.known_actor, storage=access
    )
    for actor, person in [("alice", alice), ("bob", bob)]:
        path = tmp_path / "sessions" / (actor + "-session")
        path.mkdir(parents=True)
        (path / "metadata.json").write_text(
            json.dumps(
                {
                    "project_id": project.project_id,
                    "project_dir": str(root),
                    "channel_id": "web",
                    "mode": "agent.work.normal",
                    "work_mode": "normal",
                    "team_name": None,
                    "execution_profile_id": "native",
                    "execution_config_fingerprint": "fixture",
                }
            )
        )
        host.register_owner_and_source(
            actor + "-session", person.identity(), project.project_id
        )
    access.register_resource(
        project.project_id,
        ResourceDefinition("workspace", "workspace", str(root)),
        owner_subject_id="alice",
        actions=("read",),
        expected_revision=0,
        delegable=True,
    )
    access.grant_resource(
        project.project_id,
        alice.identity(),
        "workspace",
        subject_id="bob",
        actions=("read",),
        expected_revision=1,
    )
    file = root / "report.txt"
    file.write_bytes(b"fixture content" * 10000)
    current = [alice.identity]
    origin = [True]
    manager = WebFileDownloadManager(secret="synthetic-file-signature-key")

    def issuer(person=alice, sid="alice-session", paths=None):
        return WorkspaceArtifactIssuer.capture(
            host,
            person.identity,
            sid,
            channel_id="web",
            workspace=str(root),
            source_check=lambda: origin[0],
            actual_paths=paths or (str(file),),
        )

    def token(person=alice, sid="alice-session"):
        return manager.generate_token(
            str(file), sid, artifact_issuer=issuer(person, sid)
        )

    def capture(value=None, sid="alice-session"):
        return WorkspaceDownloadPermit.capture(
            host,
            lambda: current[0](),
            sid,
            value or token(),
            token_validator=manager.validate_token,
        )

    yield SimpleNamespace(**locals())
    project_store.invalidate_cache()


def test_two_independent_owners_and_shared_project_do_not_share_tokens(setup):
    s = setup
    token_a = s.token()
    a = s.capture(token_a)
    assert a.read(0, 7) == b"fixture"
    assert a.read(a.size, 10) == b""
    assert a.name == "report.txt"
    s.current[0] = s.bob.identity
    with pytest.raises(WorkspaceDownloadDenied):
        s.capture(token_a)
    with pytest.raises(WorkspaceDownloadDenied):
        s.capture(token_a, sid="bob-session")
    token_b = s.token(s.bob, "bob-session")
    b = s.capture(token_b, sid="bob-session")
    assert b.read(0, 7) == b"fixture"
    assert len(token_b) < 4096
    assert s.tokens["alice"] not in token_a
    assert s.tokens["bob"] not in token_b
    with pytest.raises(FrozenInstanceError):
        b._path = "/tmp/forged"


@pytest.mark.parametrize(
    "change",
    [
        "credential",
        "identity",
        "authority",
        "subject",
        "acl",
        "resource",
        "source",
        "binding",
        "retire",
        "file",
        "inode",
        "root",
        "unlink",
    ],
)
def test_original_permit_cannot_deliver_buffer_after_change(setup, change):
    s = setup
    p = s.capture()
    assert p.read(0, 3) == b"fix"
    if change == "credential":
        s.auth.revoke(s.alice)
    elif change in {"identity", "authority", "subject"}:
        identity = (
            s.bob.identity()
            if change == "identity"
            else replace(
                s.alice.identity(),
                **{"authority" if change == "authority" else "subject_id": "other"},
            )
        )
        s.current[0] = lambda: identity
    elif change == "acl":
        s.access.replace_acl(
            s.project.project_id,
            "admin",
            acl={"alice": ["read"], "bob": ["read", "execute"]},
            expected_revision=2,
        )
    elif change == "resource":
        s.access.revoke_resource(
            s.project.project_id,
            s.alice.identity(),
            "workspace",
            subject_id="alice",
            expected_revision=2,
        )
    elif change in {"source", "retire"}:
        s.host.compensate_owner_registration(
            "alice-session", s.alice.identity(), expected_revision=1, expected_epoch=1
        )
        if change == "source":
            s.host.register_owner_and_source(
                "alice-session",
                s.alice.identity(),
                s.project.project_id,
                expected_owner_revision=2,
            )
    elif change == "binding":
        path = s.tmp_path / "sessions/alice-session/metadata.json"
        data = json.loads(path.read_text())
        data["channel_id"] = "other"
        path.write_text(json.dumps(data))
    elif change == "file":
        s.file.write_bytes(b"x" * p.size)
    elif change == "inode":
        replacement = s.root / "other"
        replacement.write_bytes(s.file.read_bytes())
        replacement.replace(s.file)
    elif change == "root":
        s.root.rename(s.tmp_path / "old")
        s.root.mkdir()
        s.file.write_bytes(b"x" * p.size)
    else:
        s.file.unlink()
    with pytest.raises(WorkspaceDownloadDenied):
        p.check()
    with pytest.raises(WorkspaceDownloadDenied):
        p.read(3, 5)


def test_regrant_does_not_revive_original_buffer(setup):
    s = setup
    token = s.token()
    p = s.capture(token)
    s.access.revoke_resource(
        s.project.project_id,
        s.alice.identity(),
        "workspace",
        subject_id="bob",
        expected_revision=2,
    )
    s.access.grant_resource(
        s.project.project_id,
        s.alice.identity(),
        "workspace",
        subject_id="bob",
        actions=("read",),
        expected_revision=3,
    )
    with pytest.raises(WorkspaceDownloadDenied):
        p.check()
    # A fresh request uses current independent authorization, not old permit.
    assert s.capture(token).read(0, 3) == b"fix"


@pytest.mark.parametrize("stage", ["pread", "post_open"])
def test_revocation_at_actual_read_discards_buffer(setup, monkeypatch, stage):
    s = setup
    permit = s.capture()
    original = os.pread

    def read(fd, count, offset):
        if stage == "post_open":
            s.auth.revoke(s.alice)
        data = original(fd, count, offset)
        if stage == "pread":
            s.auth.revoke(s.alice)
        return data

    monkeypatch.setattr(os, "pread", read)
    with pytest.raises(WorkspaceDownloadDenied):
        permit.read(0, 10)


@pytest.mark.parametrize(
    "kind", ["final", "parent", "fifo", "directory", "outside", "traversal"]
)
def test_safe_descriptor_walk_never_reads_unscoped_or_special_file(setup, kind):
    s = setup
    if kind == "final":
        candidate = s.root / "link"
        candidate.symlink_to(s.file)
    elif kind == "parent":
        sub = s.root / "sub"
        sub.mkdir()
        (sub / "child").write_text("owned")
        link = s.root / "link"
        link.symlink_to(sub, target_is_directory=True)
        candidate = link / "child"
    elif kind == "fifo":
        candidate = s.root / "pipe"
        os.mkfifo(candidate)
    elif kind == "directory":
        candidate = s.root
    elif kind == "outside":
        candidate = s.tmp_path / "outside"
        candidate.write_text("not scoped")
    else:
        candidate = s.root / ".." / "workspace" / "report.txt"
    with pytest.raises(WorkspaceDownloadDenied):
        issuer = s.issuer(paths=(str(candidate),))
        s.manager.generate_token(
            str(candidate), "alice-session", artifact_issuer=issuer
        )


@pytest.mark.parametrize(
    "offset,limit",
    [(-1, 1), (True, 1), (0, 0), (0, True), (0, MAX_DOWNLOAD_CHUNK_BYTES + 1)],
)
def test_bounded_read_inputs(setup, offset, limit):
    with pytest.raises(WorkspaceDownloadDenied):
        setup.capture().read(offset, limit)


def test_origin_must_stay_current_and_exact_paths(setup):
    s = setup
    issuer = s.issuer()
    s.origin[0] = False
    with pytest.raises(WorkspaceDownloadDenied):
        s.manager.generate_token(str(s.file), "alice-session", artifact_issuer=issuer)
    s.origin[0] = True
    other = s.root / "other"
    other.write_text("owned")
    with pytest.raises(WorkspaceDownloadDenied):
        issuer.issue(str(other), "alice-session")


@pytest.mark.parametrize(
    "mutation",
    [
        "extra",
        "no_namespace",
        "identity",
        "bool_epoch",
        "binding",
        "file",
        "namespace_extra",
    ],
)
def test_even_valid_hmac_does_not_grant_malformed_or_changed_provenance(
    setup, mutation
):
    s = setup
    payload = s.manager.validate_token(s.token())
    proof = payload[ARTIFACT_NAMESPACE]
    if mutation == "extra":
        payload["resource_id"] = "workspace"
    elif mutation == "no_namespace":
        payload.pop(ARTIFACT_NAMESPACE)
    elif mutation == "identity":
        proof["source"]["identity"]["subject_id"] = "forged"
    elif mutation == "bool_epoch":
        proof["source"]["source_epoch"] = True
    elif mutation == "binding":
        proof["source"]["binding"].append(["unknown", "x"])
    elif mutation == "file":
        proof["file"]["inode"] += 1
    else:
        proof["new"] = "x"
    with pytest.raises(WorkspaceDownloadDenied):
        s.capture(s.manager._sign_payload(payload))


def test_organization_direct_signer_has_no_ambient_fallback_and_legacy_retained(
    setup, monkeypatch
):
    s = setup
    with pytest.raises(WorkspaceDownloadDenied):
        s.manager.generate_token(str(s.file), "alice-session")
    monkeypatch.delenv("JIUWENSWARM_ORGANIZATION_AUTH_FILE")
    old = s.manager.generate_token(str(s.file), "alice-session")
    assert set(s.manager.validate_token(old)) == {"path", "sid"}
    with pytest.raises(WorkspaceDownloadDenied):
        s.capture(old)


def test_expiry_and_unknown_old_owner_fail_closed(setup, monkeypatch):
    s = setup
    token = s.token()
    permit = s.capture(token)
    monkeypatch.setattr(time, "time", lambda: 2**40)
    with pytest.raises(WorkspaceDownloadDenied):
        permit.check()
    with pytest.raises(WorkspaceDownloadDenied):
        s.capture(token, sid="old-session")


def test_required_nofollow_platform_capability(setup, monkeypatch):
    s = setup
    token = s.token()
    monkeypatch.delattr(os, "O_NOFOLLOW")
    with pytest.raises(WorkspaceDownloadDenied):
        s.capture(token)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation", [None, "args", "targets", "toolkit", "method", "slice", "late"]
)
async def test_actual_core_localfunction_origin_and_explicit_issuer(
    setup, monkeypatch, mutation
):
    """Actual core method certificate; host slice/factory are synthetic ports.

    Runtime/NativeSession ownership integration is intentionally a parent test.
    """
    from openjiuwen.core.foundation.tool import invoke_tool_with_authority
    from openjiuwen.harness_protocol import BeforeToolContext
    from jiuwenswarm.governance import tool_context
    from jiuwenswarm.governance.workspace_artifact_origin import (
        validate_send_file_origin,
    )
    from jiuwenswarm.agents.harness.common.tools.send_file_to_user import (
        SendFileToolkit,
    )

    s = setup
    toolkit = SendFileToolkit(
        "request", "alice-session", "web", project_dir=str(s.root)
    )
    tool = toolkit.get_tools()[0]
    args = {"abs_file_path_list": str(s.file), "target_channels": ["web"]}
    seen = []

    # Local bounded fixture supplies the optional field before the parent
    # appends it to the public private-host dataclass in its separate commit.
    class Slice(tool_context.NativeExecutionSlice):
        pass

    bound = Slice(object(), None, None, None)

    def factory(**facts):
        if mutation == "args":
            facts["actual_paths"] = (str(s.root / "different"),)
        elif mutation == "targets":
            facts["target_channels"] = ("other",)
        elif mutation == "toolkit":
            facts["toolkit"] = SendFileToolkit(
                "r", "alice-session", "web", project_dir=str(s.root)
            )
        elif mutation == "method":
            facts["source_execution"].executor._func = lambda **_: None
        elif mutation == "slice":
            bound.active = False
        check = validate_send_file_origin(**facts)
        issuer = WorkspaceArtifactIssuer.capture(
            s.host,
            s.alice.identity,
            "alice-session",
            channel_id="web",
            workspace=str(s.root),
            source_check=check,
            actual_paths=facts["actual_paths"],
        )
        seen.append(issuer)
        return issuer

    bound.artifact_issuer_factory = factory
    token_scope = tool_context._NATIVE_SLICE.set(bound)

    async def consume(_self, envelope, **kwargs):
        if mutation == "late":
            bound.active = False
        info = _self._build_files_payload(
            envelope, valid_files=[str(s.file)], assets_by_path={}
        )
        seen.append(info)
        return "delivered"

    monkeypatch.setattr(SendFileToolkit, "_send_file_with_envelope", consume)
    monkeypatch.setattr(WebFileDownloadManager, "_instance", s.manager)

    async def authorize(_):
        return True

    try:
        result = await invoke_tool_with_authority(
            tool,
            args,
            operation=BeforeToolContext(
                "agent", "native-session", "turn", "call", "send_file_to_user", args
            ),
            authorizer=authorize,
            runtime_kwargs={},
            is_current=lambda: True,
            resolve_executor=lambda: tool,
        )
    finally:
        tool_context._NATIVE_SLICE.reset(token_scope)
    if mutation is None:
        assert result == "delivered"
        token = seen[-1][0]["download_token"]
        assert s.capture(token).read(0, 7) == b"fixture"
        with pytest.raises(WorkspaceDownloadDenied):
            seen[0].issue(str(s.file), "alice-session")
    else:
        assert "提交文件失败" in result
        assert not any(isinstance(row, list) for row in seen)


def test_url_size_limit_is_explicit(setup, monkeypatch):
    import jiuwenswarm.governance.workspace_download as module

    monkeypatch.setattr(module, "MAX_DOWNLOAD_TOKEN_BYTES", 20)
    with pytest.raises(WorkspaceDownloadDenied):
        setup.token()


def test_read_only_project_does_not_gain_workspace_download(setup):
    s = setup
    s.access.replace_acl(
        s.project.project_id, "admin", acl={"alice": ["read"]}, expected_revision=2
    )
    with pytest.raises(WorkspaceDownloadDenied):
        s.token()


@pytest.mark.parametrize("routing_user", ["", "legacy-routing-user"])
def test_built_owner_artifact_url_uses_exact_session_selector(setup, monkeypatch, routing_user):
    """Actual metadata production must match the owner UI/HTTP query contract."""
    from urllib.parse import parse_qs, urlsplit
    from jiuwenswarm.agents.harness.common.tools import web_file_download

    s = setup
    monkeypatch.setattr(WebFileDownloadManager, "_instance", s.manager)
    info = web_file_download.build_file_download_info(
        str(s.file), s.file.name, "alice-session", user_id=routing_user,
        artifact_issuer=s.issuer(),
    )
    url = urlsplit(info["download_url"])
    query = parse_qs(url.query, keep_blank_values=True)
    assert not url.scheme and not url.netloc and not url.fragment
    assert url.path == "/file-api/download"
    assert query == {
        "token": [info["download_token"]],
        "session_id": ["alice-session"],
    }
    signed = s.manager.validate_token(query["token"][0])
    assert ARTIFACT_NAMESPACE in signed
    assert signed["sid"] == query["session_id"][0]
    assert info["size"] == s.file.stat().st_size
    permit = s.capture(query["token"][0], sid=query["session_id"][0])
    assert permit.read(0, 7) == b"fixture"
