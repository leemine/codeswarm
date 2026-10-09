from __future__ import annotations

import importlib.util
import http.client
import json
import os
import signal
import subprocess
import sys
import threading
import time
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
    def test_isolated_git_fixture_does_not_inherit_host_repository(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            temp = root / "tmp"
            work = temp / "fixture"
            work.mkdir(parents=True)
            inherited = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                                       cwd=work, capture_output=True)
            self.assertEqual(inherited.returncode, 0)
            env = testctl.hermetic_env({"TMPDIR": str(temp)})
            isolated = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                                      cwd=work, env=env, capture_output=True)
            self.assertNotEqual(isolated.returncode, 0)
            subprocess.run(["git", "init", "-q", str(work)], env=env, check=True)
            own = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                                 cwd=work, env=env, capture_output=True, text=True)
            self.assertEqual(own.stdout.strip(), str(work))

    def test_privileged_bwrap_prefix_is_explicit(self) -> None:
        with patch.dict(testctl.os.environ, {"TESTCTL_BWRAP_SUDO": "1"}):
            with patch.object(testctl.shutil, "which", side_effect=lambda name: f"/usr/bin/{name}"):
                self.assertEqual(testctl.bwrap_prefix(), ["/usr/bin/sudo", "-n", "-E", "/usr/bin/bwrap"])

    def test_strict_wrapper_writes_only_owned_external_sandbox(self) -> None:
        owned = Path("/var/tmp/eval-tests/testctl-owned")
        with patch.dict(os.environ, {"TESTCTL_NETWORK_MODE": "strict"}), patch.object(
            testctl, "bwrap_prefix", return_value=["bwrap"]
        ):
            command = testctl.isolated_command(
                {"id": "test"}, Path("/work"), ["python"], sandbox_root=owned,
            )
        binds = [command[index + 1:index + 3]
                 for index, value in enumerate(command) if value == "--bind"]
        self.assertIn([str(owned), str(owned)], binds)
        self.assertNotIn([str(owned.parent), str(owned.parent)], binds)
        self.assertIn("--unshare-net", command)
        self.assertEqual(command[command.index("--ro-bind") + 1:command.index("--ro-bind") + 3], ["/", "/"])

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


class TestProcessDeadline(unittest.TestCase):
    def test_output_is_flushed_while_running_and_not_duplicated(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log, release = root / "live.log", root / "release"
            code = (
                "import pathlib,time; print('first 中文',flush=True); "
                f"p=pathlib.Path({str(release)!r}); "
                "exec('while not p.exists(): time.sleep(.01)'); print('last',flush=True)"
            )
            result = []
            worker = threading.Thread(target=lambda: result.append(testctl.run_process(
                [sys.executable, "-c", code], root, os.environ.copy(), 5, log_path=log,
            )))
            worker.start()
            try:
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    if log.exists() and b"first" in log.read_bytes():
                        break
                    time.sleep(.02)
                self.assertTrue(worker.is_alive())
                self.assertEqual(log.read_text(), "first 中文\n")
            finally:
                release.touch()
                worker.join(timeout=8)
            self.assertFalse(worker.is_alive())
            self.assertEqual(result[0]["output"], "first 中文\nlast\n")
            self.assertEqual(log.read_text(), result[0]["output"])
            self.assertFalse(result[0]["timed_out"])
            self.assertFalse(result[0]["cleanup_incomplete"])

    @unittest.skipUnless(os.name == "posix", "POSIX process-group regression")
    def test_escaped_descendant_pipe_cannot_block_timeout_drain(self):
        for parent_wait in (True, False):
            with self.subTest(parent_wait=parent_wait):
                self._check_escaped_pipe(parent_wait)

    def _check_escaped_pipe(self, parent_wait):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pidfile, log = root / "owned-child.pid", root / "timeout.log"
            # Safety cap makes even the old unbounded implementation terminate.
            child = (
                "import os,time; from pathlib import Path; "
                f"Path({str(pidfile)!r}).write_text(str(os.getpid())); "
                "print('escaped child output',flush=True); time.sleep(8)"
            )
            parent = (
                "import subprocess,sys,time; "
                f"subprocess.Popen([sys.executable,'-c',{child!r}],start_new_session=True); time.sleep({10 if parent_wait else 0})"
            )
            try:
                result = testctl.run_process([sys.executable, "-c", parent], root, os.environ.copy(), .3,
                                             log_path=log)
                self.assertTrue(result["timed_out"])
                self.assertLess(result["duration_seconds"], 6)
                self.assertEqual(result["cleanup_errors"], ["output_pipe_still_open"])
                self.assertTrue(result["cleanup_incomplete"])
                self.assertIn("escaped child output", log.read_text())
                self.assertIn("[testctl] shard timeout", log.read_text())
                self.assertIsNotNone(result["returncode"])
            finally:
                if pidfile.exists():
                    try:
                        os.kill(int(pidfile.read_text()), signal.SIGKILL)
                    except ProcessLookupError:
                        pass

    @unittest.skipUnless(os.name == "posix", "POSIX signal error accounting")
    def test_signal_permission_error_is_reported_without_unbounded_cleanup(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(
            testctl.os, "killpg", side_effect=PermissionError(1, "synthetic signal denial")
        ):
            result = testctl.run_process(
                [sys.executable, "-c", "import time; time.sleep(.3)"],
                Path(directory), os.environ.copy(), .1,
            )
        self.assertTrue(result["timed_out"])
        self.assertTrue(result["cleanup_incomplete"])
        self.assertEqual(result["cleanup_errors"], ["kill_failed:1"])
        self.assertIsNotNone(result["returncode"])

    def test_timeout_stays_failed_even_with_passed_junit_and_persists_live_log(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            code = (
                "import pathlib,sys,time; "
                "pathlib.Path(sys.argv[1]).write_text('<testsuite><testcase name=\"passed-before-hang\"/></testsuite>'); "
                "print('last visible test',flush=True); time.sleep(10)"
            )
            suite = {"id": "owned.fixture", "required": True, "runner": "pytest", "workdir": ".",
                     "command": [sys.executable, "-c", code, "{junit}"], "shard_timeout_seconds": 1}
            with patch.object(testctl, "REPO_ROOT", root), patch.dict(os.environ, {"TESTCTL_NETWORK_MODE": "audit"}):
                result = testctl.execute_suite(suite, "synthetic", root)
            try:
                self.assertEqual(result["status"], "timeout")
                self.assertEqual(result["counts"]["passed"], 1)
                self.assertEqual(result["counts"]["timeout"], 1)
                self.assertIn("last visible test", (root / result["log"]).read_text())
                summary = testctl.aggregate("synthetic", "test", [result], None, 1)
                self.assertNotEqual(summary["status"], "passed")
            finally:
                testctl.shutil.rmtree(result["sandbox"])

    def test_strict_command_keeps_network_boundary_and_contains_owned_pid_tree(self):
        with patch.dict(os.environ, {"TESTCTL_NETWORK_MODE": "strict"}), patch.object(
            testctl, "bwrap_prefix", return_value=["bwrap"]
        ):
            command = testctl.isolated_command({"id": "test"}, Path("/work"), ["python", "fixture.py"])
        self.assertIn("--unshare-net", command)
        self.assertIn("--unshare-pid", command)
        self.assertIn("--die-with-parent", command)
        self.assertNotIn("--share-net", command)
        self.assertIn("--tmpfs", command)
        self.assertNotIn(["--bind", "/tmp", "/tmp"], [command[i:i+3] for i in range(len(command))])
        self.assertEqual(command[-2:], ["python", "fixture.py"])


if __name__ == "__main__":
    unittest.main()
