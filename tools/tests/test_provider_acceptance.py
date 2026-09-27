"""Prevent silent loss of acceptance gates or false green JUnit accounting."""

from pathlib import Path
import importlib.util
import json
import sys
import os
import subprocess
import tempfile
import unittest

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))
SPEC = importlib.util.spec_from_file_location(
    "provider_acceptance_under_test", TOOLS / "run_provider_acceptance.py"
)
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


class AcceptanceAccounting(unittest.TestCase):
    def audit(self, xml, planned=("tests/a.py::TestA::test_one[x]",)):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "junit.xml"
            path.write_text(
                "<testsuites><testsuite>" + xml + "</testsuite></testsuites>"
            )
            return runner.reconcile(list(planned), path, len(planned))

    def test_exact_parameter_identity_passes(self):
        self.assertTrue(
            self.audit('<testcase classname="tests.a.TestA" name="test_one[x]"/>')[
                "closed"
            ]
        )

    def test_skip_failure_error_and_missing_result_are_not_green(self):
        for state in ("skipped", "failure", "error"):
            with self.subTest(state=state):
                self.assertFalse(
                    self.audit(
                        f'<testcase classname="tests.a.TestA" name="test_one[x]"><{state}/></testcase>'
                    )["closed"]
                )
        self.assertFalse(self.audit("")["closed"])

    def test_changed_parameter_and_duplicate_cannot_replace_missing_case(self):
        self.assertFalse(
            self.audit('<testcase classname="tests.a.TestA" name="test_one[y]"/>')[
                "closed"
            ]
        )
        xml = '<testcase classname="tests.a" name="test_one"/>' * 2
        self.assertFalse(
            self.audit(xml, ("tests/a.py::test_one", "tests/a.py::test_two"))["closed"]
        )

    def test_secrets_are_removed_before_json_encoding(self):
        secret = 'key-"back\\slash-中文'
        self.assertEqual(
            runner.redacted({"nested": [secret]}, secret), {"nested": ["[REDACTED]"]}
        )
        self.assertNotIn(
            secret,
            json.dumps(
                runner.redacted({"nested": [secret]}, secret), ensure_ascii=False
            ),
        )
        self.assertEqual(
            runner.redacted(json.dumps(secret)[1:-1], secret), "[REDACTED]"
        )

    @unittest.skipUnless(
        Path("/proc/self/stat").exists(), "Linux owned-process identity check"
    )
    def test_cleanup_reaps_only_its_exact_run_scope(self):
        with tempfile.TemporaryDirectory() as folder:
            scope = Path(folder)
            env = dict(os.environ, ACCEPTANCE_RUN_ID=str(scope))
            owned = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(60)"], env=env
            )
            other = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(60)"],
                env=dict(os.environ, ACCEPTANCE_RUN_ID=str(scope) + "-other"),
            )
            try:
                report = runner.cleanup(scope)
                self.assertIn(owned.pid, [pid for pid, _ in report["rescued"]])
                self.assertFalse(report["remaining"])
                self.assertIsNone(other.poll(), "Do not clean another run's process")
            finally:
                for proc in (owned, other):
                    if proc.poll() is None:
                        proc.kill()
                    proc.wait(timeout=5)

    def test_critical_goal_heartbeat_modules_are_discovered_and_executed(self):
        manifest = json.loads((TOOLS / "test-manifest.json").read_text())
        ids = manifest["profiles"]["pr-stable"]["suites"]
        suites = [
            suite
            for suite in manifest["suites"]
            if suite["id"] in ids and suite["runner"] == "pytest"
        ]
        critical = [
            "tests/unit_tests/runtime/harness",
        ]
        # Every test file in the runtime Goal/Heartbeat scope must be selected,
        # by a parent directory or the exact file, in discovery and execution.
        critical += [
            str(p.relative_to(TOOLS.parent))
            for p in (TOOLS.parent / "tests/unit_tests").rglob("test_*.py")
            if "goal" in p.name or "heartbeat" in p.name
        ]
        for suite in suites:
            self.assertFalse(
                any("tests/system_tests" in arg for arg in suite["command"])
            )
        for scope in critical:
            files = (
                list((TOOLS.parent / scope).rglob("test_*.py"))
                if (TOOLS.parent / scope).is_dir()
                else [TOOLS.parent / scope]
            )
            for path in files:
                relative = str(path.relative_to(TOOLS.parent))
                for phase in ("discover", "run"):
                    args = [
                        arg
                        for suite in suites
                        for arg in (
                            suite["discover"]["command"]
                            if phase == "discover"
                            else suite["command"]
                        )
                        if arg.startswith("tests/") and "::" not in arg
                    ]
                    self.assertTrue(
                        any(
                            relative == arg
                            or relative.startswith(arg.rstrip("/") + "/")
                            for arg in args
                        ),
                        (relative, phase),
                    )


if __name__ == "__main__":
    unittest.main()
