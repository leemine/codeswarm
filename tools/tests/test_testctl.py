from __future__ import annotations

import importlib.util
import http.client
import json
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch


MODULE_PATH = Path(__file__).resolve().parents[1] / "testctl.py"
SPEC = importlib.util.spec_from_file_location("testctl_under_test", MODULE_PATH)
assert SPEC and SPEC.loader
testctl = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(testctl)
SHARD_SPEC = importlib.util.spec_from_file_location("shardplan_under_test", MODULE_PATH.with_name("shardplan.py"))
assert SHARD_SPEC and SHARD_SPEC.loader
shardplan = importlib.util.module_from_spec(SHARD_SPEC)
SHARD_SPEC.loader.exec_module(shardplan)


class TestProtocol(unittest.TestCase):
    def test_privileged_bwrap_prefix_is_explicit(self) -> None:
        with patch.dict(testctl.os.environ, {"TESTCTL_BWRAP_SUDO": "1"}):
            with patch.object(testctl.shutil, "which", side_effect=lambda name: f"/usr/bin/{name}"):
                self.assertEqual(testctl.bwrap_prefix(), ["/usr/bin/sudo", "-n", "-E", "/usr/bin/bwrap"])

    def test_manifest_rejects_unknown_schema(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            path.write_text(json.dumps({"schema_version": 999, "suites": [], "profiles": {}}))
            with self.assertRaises(testctl.TestCtlError):
                testctl.load_manifest(path)

    def test_pytest_collection_extracts_node_ids(self) -> None:
        output = "tests/a.py::test_one\ntests/a.py::TestThing::test_two\n2 tests collected\n"
        self.assertEqual(
            testctl.parse_pytest_collect(output),
            ["tests/a.py::TestThing::test_two", "tests/a.py::test_one"],
        )

    def test_shard_plan_preserves_file_boundary_and_count(self) -> None:
        collected = ["tests/a.py::test_one", "tests/a.py::test_two", "tests/b.py::test_three"]
        result = shardplan.plan(collected, target_cases=2)
        self.assertEqual(result["collected_cases"], 3)
        self.assertEqual(result["shard_count"], 2)
        self.assertEqual(sum(item["case_count"] for item in result["shards"]), 3)

    def test_junit_normalizes_common_and_node_dialects(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "result.xml"
            path.write_text(
                """<?xml version='1.0'?>
                <testsuites>
                  <testsuite name='pytest'>
                    <testcase classname='a' name='ok' time='0.1'/>
                    <testcase classname='a' name='bad'><failure message='boom'/></testcase>
                  </testsuite>
                  <testcase classname='node' name='direct' time='0.2'/>
                </testsuites>""",
                encoding="utf-8",
            )
            cases = testctl.parse_junit(path)
        self.assertEqual([case["state"] for case in cases], ["passed", "failed", "passed"])
        self.assertEqual(cases[-1]["id"], "node::direct")

    def test_pytest_timeout_is_not_product_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "timeout.xml"
            path.write_text(
                "<testsuite><testcase name='hung'><failure message='Failed: Timeout (>1.0s)'/></testcase>"
                "<testcase name='next'/></testsuite>",
                encoding="utf-8",
            )
            cases = testctl.parse_junit(path)
        self.assertEqual([case["state"] for case in cases], ["timeout", "passed"])

    def test_summary_counts_are_closed(self) -> None:
        counts = testctl.empty_counts()
        counts.update({"passed": 2, "skipped": 1})
        summary = testctl.aggregate(
            "run-1",
            "mvp",
            [{"suite_id": "sample", "required": True, "status": "passed", "counts": counts}],
            None,
            30,
        )
        self.assertEqual(summary["status"], "passed")
        self.assertEqual(summary["planned"], 3)
        self.assertTrue(summary["closed"])

    def test_service_lifecycle_uses_dynamic_port_and_cleans_up(self) -> None:
        server = str(Path(__file__).with_name("fixture_http_server.py"))
        with tempfile.TemporaryDirectory() as directory:
            suite = {
                "services": [{"id": "fixture", "command": [testctl.choose_python(), server]}]
            }
            processes, injected, reason = testctl.start_services(
                suite, Path(directory), testctl.hermetic_env({"HOME": directory}), Path(directory)
            )
            try:
                self.assertIsNone(reason)
                port = int(injected["TESTCTL_FIXTURE_PORT"])
                connection = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
                connection.request("GET", "/health")
                self.assertEqual(connection.getresponse().status, 200)
                connection.close()
            finally:
                testctl.stop_services(processes)
            self.assertIsNotNone(processes[0].poll())
            with self.assertRaises(OSError):
                socket.create_connection(("127.0.0.1", port), timeout=0.5)


if __name__ == "__main__":
    unittest.main()
