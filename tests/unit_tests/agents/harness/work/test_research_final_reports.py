# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Preserve the last live failures; execution completion is not quality acceptance."""

import hashlib
import json
from pathlib import Path

import pytest

from tests.system_tests.test_work_research_remote import _check_report, _write_sources


@pytest.mark.parametrize("provider, digest", [
    ("native", "922d4cab54903bab556749f4aecf695b3dd15b3b8ec4604f34ff4e021f13c944"),
    ("codex", "bce221cbd6e79df5a7374233fa4b7a55812607d8e7d397ec85242830493d8b36"),
    ("opencode", "114b9606cb482a475c86cf8cc66aaa3eed36e7b6f6f233a17225a908ca7bf122"),
])
def test_last_real_reports_still_fail_despite_correct_ledger(tmp_path, provider, digest):
    root = tmp_path / "workspace"
    _write_sources(root)
    report = (Path(__file__).parent / "fixtures" / f"{provider}-final-policy-report.md").read_bytes()
    assert hashlib.sha256(report).hexdigest() == digest
    (root / "research-report.md").write_bytes(report)
    (root / "research-evidence.json").write_text(json.dumps({"sources": {
        "source-a.md": {"network_requirement": "unknown", "line_number": 4,
                        "exact_quote": "No network requirement was tested."},
        "source-b.md": {"network_requirement": "required_in_observed_run", "line_number": 4,
                        "exact_quote": "A network connection was required."},
    }}))
    with pytest.raises(AssertionError):
        _check_report(root)
    assert (root / "research-report.md").read_bytes() == report
