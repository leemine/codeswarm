"""Exercise real authority runner provenance and verifier report compatibility."""

import json
import subprocess
import sys

import pytest

from jiuwenswarm.extensions.evaluation.backend.adapters import independent
from openjiuwen.agent_evolving.evaluator.evaluator_pipeline import ExecResult
from test_independent import task


@pytest.mark.parametrize(
    ("source", "script", "code", "count", "kind"),
    [
        ("value=3", "from solution import value\nassert value==3", 0, 1, None),
        ("value=3", "from solution import value\nassert value==4", 1, 1, None),
        ("def run(): return None.items()", "from solution import run\nassert run()==3", 1, 1, "delivery_exception"),
        ("def run(): return 1+None", "from solution import run\nassert run()==3", 1, 1, "delivery_exception"),
        ("def run(): return 1/0", "from solution import run\nassert run()==3", 1, 1, "delivery_exception"),
        ("raise ValueError('import defect')", "import solution\nassert True", 1, 0, "delivery_exception"),
        ("def broken(", "import solution\nassert True", 1, 0, "delivery_exception"),
        ("value=3", "assert absent_name==3", 2, 1, None),
        ("value=3", "raise ValueError('authority defect')", 2, 0, None),
        ("value=3", "assert (", 2, 0, None),
        ("import absent_evaluation_dependency", "import solution\nassert True", 2, 0, None),
        ("raise SystemExit(0)", "import solution\nassert True", 2, 0, None),
        ("raise KeyboardInterrupt()", "import solution\nassert True", 2, 0, None),
        ("raise GeneratorExit()", "import solution\nassert True", 2, 0, None),
    ],
)
def test_real_runner_attributes_delivery_without_fabricating_assertions(
    tmp_path, source, script, code, count, kind
):
    work = tmp_path / "work"
    work.mkdir()
    (work / "solution.py").write_text(source)
    authority = tmp_path / "authority.py"
    authority.write_text(script)
    runner = independent.RUNNER.replace("/authority/test.py", str(authority)).replace(
        "/work", str(work)
    )
    result = subprocess.run(
        [sys.executable, "-I", "-c", runner],
        capture_output=True, text=True, timeout=10,
    )
    reports = [
        line[len(independent.REPORT):]
        for line in result.stdout.splitlines()
        if line.startswith(independent.REPORT)
    ]
    assert len(reports) == 1
    report = json.loads(reports[0])
    assert result.returncode == report["returncode"] == code
    assert report["assertions"] == count
    assert report.get("failure_kind") == kind
    if code:
        assert "Traceback" in result.stderr


@pytest.mark.parametrize(
    ("report", "rc", "outcome"),
    [
        ({"assertions": 1, "returncode": 0}, 0, "passed"),
        ({"assertions": 1, "returncode": 1}, 1, "test_failed"),
        ({"assertions": 0, "returncode": 1, "failure_kind": "delivery_exception"}, 1, "test_failed"),
        ({"assertions": 0, "returncode": 0}, 0, "environment_error"),
        ({"assertions": 1, "returncode": 0, "failure_kind": "delivery_exception"}, 0, "environment_error"),
        ({"assertions": True, "returncode": 0}, 0, "environment_error"),
        ({"assertions": 1, "returncode": False}, 0, "environment_error"),
        ({"assertions": -1, "returncode": 0}, 0, "environment_error"),
        ({"assertions": 1, "returncode": 0}, 1, "environment_error"),
        ([], 0, "environment_error"),
        (3, 0, "environment_error"),
    ],
)
@pytest.mark.asyncio
async def test_verifier_handles_legacy_and_malformed_reports(
    tmp_path, monkeypatch, report, rc, outcome
):
    output = independent.REPORT + json.dumps(report) + "\n"
    class Environment:
        owner = "a" * 32
        container_name = "evaluation-" + owner
        removed = False
        calls = 0
        def __init__(self, *args):
            pass
        async def start(self):
            pass
        async def exec(self, *args, **kwargs):
            self.calls += 1
            return ExecResult(stdout=output if self.calls > 1 else "", stderr="", returncode=rc if self.calls > 1 else 0)
        async def stop(self):
            self.removed = True
    async def snapshot():
        return {}
    monkeypatch.setattr(independent, "VerificationEnvironment", Environment)
    monkeypatch.setattr(independent, "environment_snapshot", snapshot)
    work = tmp_path / "work"
    work.mkdir()
    (work / "solution.py").write_text("x=1")
    verifier = independent.IndependentVerifier(tmp_path / "staging")
    result = await verifier.verify("attempt", work, task(), {})
    assert result["outcome"] == outcome
    assert result["test_output"] == output
    assert result["verifier_removed"] and result["exit_confirmed"]
    assert not verifier.environments
