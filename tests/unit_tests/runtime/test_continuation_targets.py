"""Static B3 selection uses real project/resource persistence and no Model."""

from __future__ import annotations

import copy
import json
from dataclasses import FrozenInstanceError, replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from jiuwenswarm.governance.continuation import ContinuationInput
from jiuwenswarm.governance.contracts import TrustedIdentity
from jiuwenswarm.governance.preparation import SubmissionGuard
from jiuwenswarm.governance.resources import ResourceAccessDenied, ResourceDefinition
from jiuwenswarm.runtime.continuation_targets import ContinuationTargets
from jiuwenswarm.server.runtime.session import project_store
from jiuwenswarm.server.runtime.session.project_access import ProjectAccessStore


@pytest.fixture
def case(tmp_path, monkeypatch):
    monkeypatch.setattr(project_store, "get_agent_root_dir", lambda: tmp_path)
    project_store.invalidate_cache()
    root = tmp_path / "workspace"
    root.mkdir()
    project = project_store.create_project("Target", str(root), work_mode="work")
    store = ProjectAccessStore()
    store.initialize(project.project_id, "alice")
    store.replace_acl(
        project.project_id,
        "alice",
        acl={"bob": ["read", "execute"], "charlie": ["read", "execute"]},
        expected_revision=1,
    )
    for revision, (rid, kind, ref, actions) in enumerate(
        (
            ("workspace", "workspace", str(root), ("read",)),
            ("model-bob", "credential", "model-account:bob", ("use",)),
        )
    ):
        store.register_resource(
            project.project_id,
            ResourceDefinition(rid, kind, ref),
            owner_subject_id="bob",
            actions=actions,
            expected_revision=revision,
        )
    identity = TrustedIdentity("bob", "bob", "test-auth")
    runtime = SimpleNamespace(
        _governance_identity=Mock(return_value=identity),
        _submission_guard=SubmissionGuard(store),
        _resource_authorizer=store,
    )
    config = {
        "permissions": {"enabled": True},
        "execution": {
            "default_profile_id": "native",
            "profiles": {
                "native": {
                    "provider_id": "native",
                    "config_revision": "r1",
                    "requested_mode": "normal",
                    "provider_config": {},
                }
            },
        },
    }
    entries = [
        {
            "model_client_config": {
                "model_name": "same-model",
                "client_provider": "OpenAI",
                "api_base": "https://models.example/v1",
                "credential_reference": "model-account:" + name,
                "credential_encoding": "plain",
                "api_key": "MODEL_REQUEST_AUTHORITY",
            },
            "model_config_obj": {"model_name": "same-model", "temperature": 0},
        }
        for name in ("alice", "bob")
    ]
    selector = ContinuationTargets(
        runtime,
        catalog_source=lambda: config,
        model_entries_source=lambda: entries,
        project_store=project_store,
    )
    request = ContinuationInput(
        "alice-source-session",
        "alice-share",
        1,
        "private-create-token",
        project.project_id,
        "native",
        model_name="same-model#1",
    )
    yield SimpleNamespace(
        root=root,
        project=project,
        store=store,
        identity=identity,
        runtime=runtime,
        config=config,
        entries=entries,
        selector=selector,
        request=request,
    )
    project_store.invalidate_cache()


def test_select_freezes_private_facts_without_copying_source_session(case):
    target = case.selector.select(case.request)
    assert case.selector.revalidate(target) is target
    p = target.provision_input
    assert (p.channel_id, p.persist_session, p.persist_session_supplied) == (
        "web",
        True,
        True,
    )
    assert (p.mode, p.project_id, p.project_dir, p.cwd) == (
        "agent.work.normal",
        case.project.project_id,
        str(case.root),
        str(case.root),
    )
    assert (
        p.previous_session_id,
        p.requested_session_id,
        p.user_id,
        p.is_swarm,
        p.team_hint,
    ) == ("", None, "", False, False)
    assert p.model_name == "same-model#1" and p.execution_profile_id == "native"
    assert target.identity == case.identity
    assert target.model_binding.reference == "model-account:bob"
    assert [d.resource_revision for d in target.resource_decisions] == [2, 2]
    assert target.project_acl_revision == 2
    assert "private-create-token" not in repr(target)
    with pytest.raises(FrozenInstanceError):
        target.execution_revision = "change"
    with pytest.raises(FrozenInstanceError):
        p.model_name = "other"


@pytest.mark.parametrize(
    "model_name",
    [
        "",
        "same-model",
        "same-model#0",
        "unknown#1",
        "same-model#01",
        "same-model#bad",
        "same-model#20",
    ],
)
def test_exact_model_key_and_selected_reference_required(case, model_name):
    with pytest.raises(ResourceAccessDenied):
        case.selector.select(replace(case.request, model_name=model_name))


@pytest.mark.parametrize(
    "change",
    [
        "reorder",
        "reference",
        "endpoint",
        "request",
        "request_name",
        "request_model",
        "profile_revision",
        "profile_mode",
        "resource_revision",
        "identity_authority",
    ],
)
def test_revalidate_rejects_same_name_drift_without_replacing_original(case, change):
    original = case.selector.select(case.request)
    if change == "reorder":
        case.entries.reverse()
    elif change == "reference":
        case.entries[1]["model_client_config"]["credential_reference"] = (
            "model-account:other"
        )
    elif change == "endpoint":
        case.entries[1]["model_client_config"]["api_base"] = "https://other.example/v1"
    elif change == "request":
        case.entries[1]["model_config_obj"]["temperature"] = 0.8
    elif change in ("request_name", "request_model"):
        case.entries[1]["model_config_obj"][
            "model_name" if change == "request_name" else "model"
        ] = "wrong-model"
    elif change == "profile_revision":
        case.config["execution"]["profiles"]["native"]["config_revision"] = "r2"
    elif change == "profile_mode":
        case.config["execution"]["profiles"]["native"]["requested_mode"] = None
    elif change == "resource_revision":
        case.store.register_resource(
            case.project.project_id,
            ResourceDefinition("other", "tool", "unrelated-tool"),
            owner_subject_id="bob",
            actions=("invoke",),
            expected_revision=2,
        )
    else:
        case.runtime._governance_identity.return_value = TrustedIdentity(
            "bob", "bob", "new-auth"
        )
    with pytest.raises(ResourceAccessDenied):
        case.selector.revalidate(original)
    assert original.model_binding.reference == "model-account:bob"
    assert original.execution_revision == "r1"
    assert original.resource_revision == 2


@pytest.mark.parametrize(
    "identity",
    [
        None,
        "bob",
        TrustedIdentity("bob", "charlie", "test-auth"),
        TrustedIdentity("charlie", "bob", "test-auth"),
    ],
)
def test_full_trusted_identity_and_actor_subject_permissions(case, identity):
    case.runtime._governance_identity.return_value = identity
    # The store checks both actor and subject grants; neither identity component
    # may borrow the other's resource permission.
    with pytest.raises(ResourceAccessDenied):
        case.selector.select(case.request)


@pytest.mark.parametrize("action", ["read", "execute"])
def test_acl_revocation_and_missing_managed_revision_deny(case, action):
    target = case.selector.select(case.request)
    case.store.replace_acl(
        case.project.project_id,
        "alice",
        acl={"bob": [a for a in ("read", "execute") if a != action]},
        expected_revision=2,
    )
    with pytest.raises(ResourceAccessDenied):
        case.selector.revalidate(target)
    case.store.path.unlink()
    with pytest.raises(ResourceAccessDenied):
        case.selector.select(case.request)


@pytest.mark.parametrize("rid", ["workspace", "model-bob"])
def test_resource_revocation_does_not_fall_back_to_project_execute(case, rid):
    target = case.selector.select(case.request)
    case.store.revoke_resource(
        case.project.project_id,
        case.identity,
        rid,
        subject_id="bob",
        expected_revision=2,
    )
    with pytest.raises(ResourceAccessDenied):
        case.selector.revalidate(target)


@pytest.mark.parametrize("kind", ["workspace", "credential"])
def test_duplicate_required_resource_denied(case, kind):
    ref, action = (
        (str(case.root), "read")
        if kind == "workspace"
        else ("model-account:bob", "use")
    )
    case.store.register_resource(
        case.project.project_id,
        ResourceDefinition("duplicate", kind, ref),
        owner_subject_id="bob",
        actions=(action,),
        expected_revision=2,
    )
    with pytest.raises(ResourceAccessDenied):
        case.selector.select(case.request)


def test_duplicate_catalog_binding_denied(case):
    case.entries.append(copy.deepcopy(case.entries[1]))
    with pytest.raises(ResourceAccessDenied):
        case.selector.select(case.request)


@pytest.mark.parametrize(
    "profile",
    [
        {"provider_id": "opencode"},
        {"provider_id": "codex"},
        {"provider_config": {"language": "en"}},
        {"provider_config": {"deep_agent": {"model": {"api_key": "SYNTHETIC-SECRET"}}}},
        {"provider_config": {"env": {"TOKEN": "${SECRET}"}}},
        {"requested_mode": "plan"},
    ],
)
def test_only_explicit_empty_config_native_normal_is_open(case, profile):
    case.config["execution"]["profiles"]["native"].update(profile)
    with pytest.raises(ResourceAccessDenied) as exc:
        case.selector.select(case.request)
    assert "SYNTHETIC-SECRET" not in str(exc.value) and "${SECRET}" not in str(
        exc.value
    )


def test_no_catalog_defaults_or_full_access(case):
    with pytest.raises(ResourceAccessDenied):
        case.selector.select(replace(case.request, execution_profile_id="unknown"))
    case.config["permissions"]["enabled"] = False
    with pytest.raises(ResourceAccessDenied):
        case.selector.select(case.request)
    case.config.clear()
    with pytest.raises(ResourceAccessDenied):
        case.selector.select(case.request)


def test_project_read_bypasses_cache_and_detects_directory_and_work_mode_drift(case):
    target = case.selector.select(case.request)
    path = project_store._projects_file()
    data = json.loads(path.read_text())
    record = next(
        p for p in data["projects"] if p["project_id"] == case.project.project_id
    )
    record["work_mode"] = "code"
    path.write_text(json.dumps(data))
    with pytest.raises(ResourceAccessDenied):
        case.selector.revalidate(target)
    code = case.selector.select(replace(case.request, mode="agent.code.normal"))
    assert code.provision_input.work_mode == "code"
    record["project_dir"] = str(case.root.parent)
    path.write_text(json.dumps(data))
    with pytest.raises(ResourceAccessDenied):
        case.selector.revalidate(code)


def test_bad_model_transport_environment_and_secret_errors_do_not_leak(case):
    secret = "SYNTHETIC-SECRET"
    for name, value in [
        ("api_mode", "responses"),
        ("client_provider", "Anthropic"),
        ("credential_encoding", None),
        ("api_base", "${SECRET}"),
        ("custom_headers", {"Authorization": secret}),
    ]:
        old = dict(case.entries[1]["model_client_config"])
        case.entries[1]["model_client_config"][name] = value
        with pytest.raises(ResourceAccessDenied) as exc:
            case.selector.select(case.request)
        assert secret not in str(exc.value)
        case.entries[1]["model_client_config"] = old
    case.selector._model_entries_source = Mock(side_effect=RuntimeError(secret))
    with pytest.raises(ResourceAccessDenied) as exc:
        case.selector.select(case.request)
    assert (
        str(exc.value) == "continuation target unavailable"
        and exc.value.__suppress_context__
    )


def test_target_does_not_consume_key_or_accept_foreign_selector(case):
    class Secret:
        def __str__(self):
            raise AssertionError("secret consumed")

        def __repr__(self):
            raise AssertionError("secret logged")

    case.entries[1]["model_client_config"]["api_key"] = Secret()
    target = case.selector.select(case.request)
    other = ContinuationTargets(
        case.runtime,
        catalog_source=lambda: case.config,
        model_entries_source=lambda: case.entries,
        project_store=project_store,
    )
    with pytest.raises(ResourceAccessDenied):
        other.revalidate(target)
    assert case.selector.revalidate(target) is target


def test_default_sources_use_raw_metadata_not_decrypt_env_or_model(monkeypatch, case):
    from jiuwenswarm.common import config
    from jiuwenswarm.governance import model_credentials

    raw = copy.deepcopy(case.config)
    raw["models"] = {"defaults": copy.deepcopy(case.entries)}
    raw["models"]["defaults"][1]["model_client_config"]["api_key"] = (
        "${SYNTHETIC_SECRET}"
    )
    monkeypatch.setattr(config, "get_config_raw", lambda: raw)
    monkeypatch.setattr(
        config,
        "get_config",
        Mock(side_effect=AssertionError("resolved config forbidden")),
    )
    monkeypatch.setattr(
        config,
        "get_default_models",
        Mock(side_effect=AssertionError("default models forbidden")),
    )
    selector = ContinuationTargets(case.runtime)
    target = selector.select(case.request)
    assert target.model_binding.reference == "model-account:bob"
    assert "${SYNTHETIC_SECRET}" not in repr(target)
    # Static metadata selection intentionally does not establish usable secrets.
    assert (
        model_credentials.configured_model_metadata()[1]["model_client_config"][
            "api_key"
        ]
        == "MODEL_REQUEST_AUTHORITY"
    )


def test_resource_expiry_after_selection_denies(case, monkeypatch):
    import time
    from jiuwenswarm.server.runtime.session import project_access

    expiry = time.time() + 3600
    case.store.register_resource(
        case.project.project_id,
        ResourceDefinition("model-bob", "credential", "model-account:bob"),
        owner_subject_id="bob",
        actions=("use",),
        expires_at=expiry,
        expected_revision=2,
    )
    target = case.selector.select(case.request)
    assert target.resource_decisions[1].expires_at == expiry
    monkeypatch.setattr(project_access.time, "time", lambda: expiry + 1)
    with pytest.raises(ResourceAccessDenied):
        case.selector.revalidate(target)


def test_identity_changed_during_selection_is_not_replaced(case):
    case.runtime._governance_identity.side_effect = [
        case.identity,
        TrustedIdentity("bob", "bob", "new-auth"),
    ]
    with pytest.raises(ResourceAccessDenied):
        case.selector.select(case.request)


def test_workspace_subtree_is_not_project_root_permission(case):
    case.store.revoke_resource(
        case.project.project_id,
        case.identity,
        "workspace",
        subject_id="bob",
        expected_revision=2,
    )
    case.store.register_resource(
        case.project.project_id,
        ResourceDefinition("scoped-workspace", "workspace", str(case.root)),
        owner_subject_id="alice",
        actions=("read",),
        expected_revision=3,
        delegable=True,
    )
    child = case.root / "child"
    child.mkdir()
    case.store.grant_resource(
        case.project.project_id,
        TrustedIdentity("alice", "alice", "test-auth"),
        "scoped-workspace",
        subject_id="bob",
        actions=("read",),
        scope=str(child),
        expected_revision=4,
    )
    with pytest.raises(ResourceAccessDenied):
        case.selector.select(case.request)


def test_model_request_secret_metadata_denied_without_leak(case):
    case.entries[1]["model_config_obj"]["headers"] = {
        "Authorization": "SYNTHETIC-SECRET"
    }
    with pytest.raises(ResourceAccessDenied) as exc:
        case.selector.select(case.request)
    assert "SYNTHETIC-SECRET" not in str(exc.value)
