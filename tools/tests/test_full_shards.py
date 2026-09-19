from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tools.run_full_shards import junit_counts, parse_collect, reconcile_ids, run_exit_code
from tools.analyze_full_shards import classify


class TestFullShardAccounting(unittest.TestCase):
    def test_collection_skip_is_not_a_case(self) -> None:
        xml = """<testsuite>
          <testcase classname="tests.a" name="passes"/>
          <testcase classname="tests.a" name="fails"><failure message="bad"/></testcase>
          <testcase classname="tests.a" name="case_skip"><skipped message="skip"/></testcase>
          <testcase classname="tests.desktop" name=""><skipped message="collection skipped">missing webview</skipped></testcase>
        </testsuite>"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "junit.xml"
            path.write_text(xml, encoding="utf-8")
            counts, collection_skips = junit_counts(path)
        self.assertEqual(counts, {"passed": 1, "failed": 1, "error": 0, "skipped": 1})
        self.assertEqual(collection_skips, [{"module": "tests.desktop", "reason": "missing webview"}])

    def test_canonical_collect_filters_noise(self) -> None:
        output = "tests/a.py::test_one\ntests/a.py::test_one\n2 tests collected\n"
        self.assertEqual(parse_collect(output), {"tests/a.py::test_one"})

    def test_triage_keeps_timeout_and_async_failure_separate(self) -> None:
        self.assertEqual(classify("Failed: Timeout (>30.0s) from pytest-timeout."), "single_case_timeout")
        self.assertEqual(classify("Failed: async def functions are not natively supported."),
                         "async_test_configuration")

    def test_changed_parameter_id_is_not_auto_reconciled(self) -> None:
        missing, extra, pairs = reconcile_ids(
            ["tests/a.py::test_zip[clock=1]", "tests/b.py::test_missing"],
            ["tests/a.py::test_zip[clock=2]"])
        self.assertEqual(missing, ["tests/a.py::test_zip[clock=1]", "tests/b.py::test_missing"])
        self.assertEqual(extra, ["tests/a.py::test_zip[clock=2]"])
        self.assertEqual(pairs, [])

    def test_incomplete_or_not_run_archive_fails_even_when_pytest_exits_zero(self) -> None:
        summary = {"closed": True, "not_run": 0,
                   "results": {"shard-001": {"returncode": 0, "junit_parse_error": None}}}
        self.assertEqual(run_exit_code(summary), 0)
        summary["closed"] = False
        self.assertEqual(run_exit_code(summary), 1)
        summary["closed"] = True
        summary["not_run"] = 1
        self.assertEqual(run_exit_code(summary), 1)


if __name__ == "__main__":
    unittest.main()
