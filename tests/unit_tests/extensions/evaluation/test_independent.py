"""Policy boundaries and adverse delivery cases; real Docker is separately probed."""

import hashlib
import json
import os
import subprocess
import sys

import pytest

from jiuwenswarm.extensions.evaluation.backend.adapters.deliverables import capture
from jiuwenswarm.extensions.evaluation.backend.adapters.independent import (
    IndependentVerifier,
    RUNNER,
    dependency_materials,
)
from jiuwenswarm.extensions.evaluation.backend.adapters.store import CatalogError
from jiuwenswarm.extensions.evaluation.backend.models import TaskDraft, decode


def task(**changes):
    return decode(
        TaskDraft,
        {
            "task_id": "check",
            "name": "Check",
            "instruction": "Implement solution",
            "files": [
                {"path": "solution.py", "content": "x=0\n"},
                {"path": "tests.py", "content": "assert False\n"},
            ],
            "deliverables": ["solution.py"],
            "acceptance": {
                "kind": "python",
                "script": "from solution import x\nassert x == 1",
            },
            **changes,
        },
    )


def test_only_declared_deliveries_apply_and_fixed_tests_survive(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    (work / "solution.py").write_text("x=1\n")
    (work / "tests.py").write_text("assert True\n")
    (work / "secret.txt").write_text("not declared")
    result = capture(work, task(), tmp_path / "fresh")
    assert (tmp_path / "fresh/solution.py").read_text() == "x=1\n"
    assert (tmp_path / "fresh/tests.py").read_text() == "assert False\n"
    assert not (tmp_path / "fresh/secret.txt").exists()
    assert [f["path"] for f in result["files"]] == ["solution.py"]
    assert result["files"][0]["sha256"] == hashlib.sha256(b"x=1\n").hexdigest()


@pytest.mark.parametrize(
    "kind",
    ["symlink", "parent-link", "hardlink", "fifo", "directory", "oversized", "missing"],
)
def test_unsafe_or_missing_delivery_is_an_artifact_error(tmp_path, kind):
    work = tmp_path / "work"
    work.mkdir()
    outside = tmp_path / "outside"
    outside.write_text("private")
    name = "new.txt"
    if kind == "symlink":
        (work / name).symlink_to(outside)
    elif kind == "parent-link":
        (work / "nested").symlink_to(tmp_path, target_is_directory=True)
        name = "nested/outside"
    elif kind == "hardlink":
        os.link(outside, work / name)
    elif kind == "fifo":
        os.mkfifo(work / name)
    elif kind == "directory":
        (work / name).mkdir()
    elif kind == "oversized":
        (work / name).write_bytes(b"a" * (1048576 + 1))
    with pytest.raises(CatalogError):
        capture(work, task(deliverables=[name]), tmp_path / "fresh")


def test_deletion_and_executable_mode_are_in_delivery_manifest(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    (work / "run.sh").write_text("echo ok\n")
    (work / "run.sh").chmod(0o755)
    result = capture(
        work, task(deliverables=["solution.py", "run.sh"]), tmp_path / "fresh"
    )
    assert result["files"][0]["status"] == "deleted"
    assert result["files"][1]["mode"] == 0o755
    assert not (tmp_path / "fresh/solution.py").exists()
    assert (tmp_path / "fresh/run.sh").stat().st_mode & 0o777 == 0o755


def test_offline_dependency_lock_requires_known_hash_and_no_install_options(
    tmp_path, monkeypatch
):
    wheel = tmp_path / "wheelhouse"
    wheel.mkdir()
    (wheel / "demo.whl").write_bytes(b"wheel fixture")
    sha = hashlib.sha256(b"wheel fixture").hexdigest()
    snapshot = {"wheels": [{"name": "demo.whl", "sha256": sha}]}
    monkeypatch.setenv("JIUWENSWARM_EVALUATION_WHEELHOUSE", str(wheel))
    work = tmp_path / "work"
    work.mkdir()
    dest = tmp_path / "copy"
    dest.mkdir()
    t = task(
        deliverables=["requirements.lock"],
        acceptance={
            "kind": "python",
            "script": "assert True",
            "dependency_lock": "requirements.lock",
        },
    )
    for value in [
        "--index-url https://invalid.invalid",
        "demo==1.0",
        "demo @ file:///secret",
        "demo==1.0 --hash=sha256:" + "0" * 64,
    ]:
        (work / "requirements.lock").write_text(value)
        with pytest.raises(CatalogError):
            dependency_materials(t, work, dest, snapshot)
    (work / "requirements.lock").write_text("demo==1.0 --hash=sha256:" + sha + "\n")
    result = dependency_materials(t, work, dest, snapshot)
    assert (
        result["locked_requirements"] == 1
        and (dest / "demo.whl").read_bytes() == b"wheel fixture"
    )


@pytest.mark.parametrize(
    ("script", "code", "count"),
    [
        ("assert True", 0, 1),
        ("assert False", 1, 1),
        ('print("no tests")', 2, 0),
        ("raise SystemExit(0)", 2, 0),
        ("import absent_evaluation_module", 2, 0),
        ("def check():\n assert 1==1\ncheck()", 0, 1),
    ],
)
def test_authoritative_runner_does_not_treat_empty_or_early_exit_as_pass(
    tmp_path, script, code, count
):
    test = tmp_path / "test.py"
    test.write_text(script)
    runner = RUNNER.replace("'/authority/test.py'", repr(str(test)))
    result = subprocess.run(
        [sys.executable, "-I", "-c", runner], capture_output=True, text=True, timeout=10
    )
    assert result.returncode == code
    report = json.loads(result.stdout.split("EVALUATION_AUTHORITY_REPORT=")[-1])
    assert report["assertions"] == count


@pytest.mark.asyncio
async def test_environment_change_fails_before_container_or_delivery(
    tmp_path, monkeypatch
):
    from jiuwenswarm.extensions.evaluation.backend.adapters import independent

    async def changed():
        return {"image_id": "new"}

    monkeypatch.setattr(independent, "environment_snapshot", changed)
    verifier = IndependentVerifier(tmp_path / "staging")
    with pytest.raises(CatalogError, match="VERIFIER_ENVIRONMENT_CHANGED"):
        await verifier.verify("attempt", tmp_path, task(), {"image_id": "old"})
    assert not verifier.environments and not (tmp_path / "staging").exists()


@pytest.mark.asyncio
async def test_cancel_before_verifier_launch_never_creates_container(
    tmp_path, monkeypatch
):
    from jiuwenswarm.extensions.evaluation.backend.adapters import independent

    async def snapshot():
        return {}

    monkeypatch.setattr(independent, "environment_snapshot", snapshot)
    verifier = IndependentVerifier(tmp_path / "stage")
    await verifier.cancel("attempt")
    work = tmp_path / "work"
    work.mkdir()
    (work / "solution.py").write_text("x=1")
    result = await verifier.verify("attempt", work, task(), {})
    assert result["outcome"] == "cancelled" and result["exit_confirmed"]
    assert not verifier.environments and not list((tmp_path / "stage").iterdir())


@pytest.mark.asyncio
async def test_recovery_cleanup_requires_valid_durable_ownership(tmp_path):
    verifier = IndependentVerifier(tmp_path)
    with pytest.raises(CatalogError, match="EXIT_NOT_CONFIRMED"):
        await verifier.cancel(
            "attempt", {"name": "someone-elses-container", "owner": "invalid"}
        )
    assert not verifier.environments


@pytest.mark.asyncio
async def test_cancel_after_restart_settles_only_confirmed_verifier_exit(
    tmp_path, monkeypatch
):
    from test_trials import setup, ACTOR

    trials, experiment = setup(tmp_path, monkeypatch)
    attempt = experiment["trials"][0]["attempts"][0]
    # Simulate persisted state after original Runtime exit and before verifier exit.
    trials._patch(
        ACTOR, experiment["id"], attempt["id"], "submitting", session_id="original"
    )
    trials._patch(
        ACTOR, experiment["id"], attempt["id"], "observing", execution_finished_at=1
    )
    ownership = {"name": "evaluation-" + "a" * 32, "owner": "a" * 32}
    trials._patch(
        ACTOR,
        experiment["id"],
        attempt["id"],
        "verifying",
        verifier_ownership=ownership,
    )
    calls = []

    async def cleanup(attempt_id, recovery):
        calls.append((attempt_id, recovery))

    monkeypatch.setattr(trials.independent, "cancel", cleanup)
    await trials.cancel(ACTOR, experiment["id"])
    result = trials.get(ACTOR, experiment["id"])["trials"][0]["attempts"][0]
    assert result["phase"] == "settled" and result["body"]["outcome"] == "cancelled"
    assert result["body"]["verifier_removed"] and result["body"]["exit_confirmed"]
    assert calls == [(attempt["id"], ownership)]
    await trials.close()
    trials.store.close()
