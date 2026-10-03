# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
from dataclasses import FrozenInstanceError, replace

import pytest

from jiuwenswarm.governance.contracts import AuthorizationDecision, TrustedIdentity
from jiuwenswarm.governance.preparation import AlreadySubmitted, GovernanceError, SubmissionGuard, compensate_owned


class Authority:
    allowed = True
    revision = 1
    def authorize(self, project_id, actor_id, action):
        return AuthorizationDecision(self.allowed and actor_id == "alice", project_id, actor_id,
                                     action, self.revision, "ACL")


def prepare(guard, **kwargs):
    return guard.prepare(**dict(request_id="r", identity=TrustedIdentity("alice", "worker", "host"),
                                project_id="p", action="execute", session_id="s", generation=3,
                                inputs={"query": ["original"]}, **kwargs))


def test_snapshot_and_decision_are_fixed_while_current_revision_changes():
    authority = Authority()
    guard = SubmissionGuard(authority)
    inputs = {"query": ["original"]}
    item = guard.prepare(request_id="r", identity=TrustedIdentity("alice", "worker", "host"),
                         project_id="p", action="execute", session_id="s", generation=3, inputs=inputs)
    inputs["query"].append("mutated")
    authority.revision = 2
    guard.begin_submission(item, generation=3)
    assert "mutated" not in item.input_snapshot
    assert item.authorization.revision == 1
    with pytest.raises(FrozenInstanceError):
        item.identity.actor_id = "mallory"


@pytest.mark.parametrize("kind", ["revoke", "generation"])
def test_commit_rejects_revocation_or_generation_change(kind):
    authority = Authority()
    guard = SubmissionGuard(authority)
    item = prepare(guard)
    if kind == "revoke":
        authority.allowed = False
    with pytest.raises(GovernanceError):
        guard.begin_submission(item, generation=4 if kind == "generation" else 3)
    assert guard.outcome(item) == "rejected"


def test_unknown_and_accepted_are_never_resubmitted():
    guard = SubmissionGuard(Authority())
    item = prepare(guard)
    guard.begin_submission(item, generation=3)
    assert guard.outcome(item) == "unknown"
    with pytest.raises(AlreadySubmitted, match="unknown"):
        guard.begin_submission(item, generation=3)
    guard.accepted(item)
    with pytest.raises(AlreadySubmitted, match="accepted"):
        prepare(guard)


def test_request_id_cannot_be_reused_for_other_input():
    guard = SubmissionGuard(Authority())
    prepare(guard)
    with pytest.raises(GovernanceError, match="different input"):
        guard.prepare(request_id="r", identity=TrustedIdentity("alice", "worker", "host"),
                      project_id="p", action="execute", session_id="s", generation=3, inputs={})


def test_untrusted_transport_fields_do_not_prove_identity():
    guard = SubmissionGuard(Authority())
    with pytest.raises(GovernanceError, match="denied"):
        guard.prepare(request_id="r", identity=None, project_id="p", action="execute",
                      session_id="s", generation=3, inputs={"user_id": "alice", "metadata": {"actor_id": "alice"}})


def test_unknown_receipts_survive_capacity_pressure():
    guard = SubmissionGuard(Authority(), capacity=1)
    item = prepare(guard)
    guard.begin_submission(item, generation=3)
    with pytest.raises(GovernanceError, match="capacity"):
        guard.prepare(request_id="r2", identity=item.identity, project_id="p", action="execute",
                      session_id="s", generation=3, inputs={})
    with pytest.raises(AlreadySubmitted):
        prepare(guard)


def test_prepared_capability_cannot_be_copied_to_other_guard_or_forged():
    guard = SubmissionGuard(Authority())
    item = prepare(guard)
    with pytest.raises(GovernanceError, match="another"):
        SubmissionGuard(Authority()).begin_submission(item, generation=3)
    with pytest.raises(GovernanceError, match="another"):
        guard.begin_submission(replace(item, project_id="other"), generation=3)


@pytest.mark.asyncio
async def test_compensation_is_reverse_owned_and_continues_after_failure():
    releases = []
    async def first(): releases.append("first")
    async def second():
        releases.append("second")
        raise OSError("cleanup")
    failures = await compensate_owned((first, second))
    assert releases == ["second", "first"]
    assert len(failures) == 1
