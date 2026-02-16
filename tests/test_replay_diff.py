from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _fixture_dir() -> Path:
    return Path(__file__).resolve().parent / "fixtures" / "replay" / "v0.1"


def _run_tm(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "tm", *args],
        cwd=_repo_root(),
        capture_output=True,
        text=True,
    )


def test_replay_diff_policy_change_only_expected_row_and_rule(tmp_path: Path) -> None:
    fixtures = _fixture_dir()
    report_path = tmp_path / "diff_report.json"
    result = _run_tm(
        "replay",
        "diff",
        "--trace",
        str(fixtures / "trace_diff.jsonl"),
        "--policy-old",
        str(fixtures / "policy_old.json"),
        "--policy-new",
        str(fixtures / "policy_new.json"),
        "--json-report",
        str(report_path),
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(report_path.read_text(encoding="utf-8"))

    assert report["summary"]["changed_rows"] == 1
    changed_rows = [row for row in report["rows"] if row["status"] == "changed"]
    assert len(changed_rows) == 1
    assert changed_rows[0]["index"] == 0

    by_rule = {entry["rule_id"]: entry for entry in report["by_rule"]}
    assert set(by_rule.keys()) == {"r-high-temp"}
    assert by_rule["r-high-temp"]["changed_rows"] == 1
    assert by_rule["r-high-temp"]["action_modified"] == 1
