from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _fixtures_dir() -> Path:
    return Path(__file__).resolve().parent / "fixtures" / "testsuites" / "v0.1"


def _run_tests_cli(*, suite: Path, policy: Path, report: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "tm",
            "tests",
            "run",
            "--suite",
            str(suite),
            "--policy",
            str(policy),
            "--json-report",
            str(report),
        ],
        cwd=_repo_root(),
        capture_output=True,
        text=True,
    )


def _load_json(path: Path) -> Mapping[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def _make_failing_suite(src: Path, dst: Path, *, replacement_value: str) -> Path:
    payload = dict(_load_json(src))
    spec = dict(payload["spec"])
    tests = list(spec["tests"])
    test0 = dict(tests[0])
    expected = dict(test0["expected"])
    action_log = dict(expected["action_log"])
    final_patch = dict(action_log["final_patch"])
    final_patch["cooler.mode"] = replacement_value
    action_log["final_patch"] = final_patch
    expected["action_log"] = action_log
    test0["expected"] = expected
    tests[0] = test0
    spec["tests"] = tests
    payload["spec"] = spec
    _write_json(dst, payload)
    return dst


def test_hard_fail_returns_exit_1(tmp_path: Path) -> None:
    fixtures = _fixtures_dir()
    suite_fail = _make_failing_suite(
        fixtures / "suite_hard.json",
        tmp_path / "suite_hard_fail.json",
        replacement_value="wrong",
    )
    report = tmp_path / "report_hard.json"
    result = _run_tests_cli(suite=suite_fail, policy=fixtures / "policy.json", report=report)
    assert result.returncode == 1
    payload = _load_json(report)
    assert payload["summary"]["failed"] == 1
    assert payload["summary"]["warnings"] == 0
    assert payload["results"][0]["status"] == "fail"


def test_compat_fail_returns_exit_1(tmp_path: Path) -> None:
    fixtures = _fixtures_dir()
    suite_fail = _make_failing_suite(
        fixtures / "suite_compat.json",
        tmp_path / "suite_compat_fail.json",
        replacement_value="wrong",
    )
    report = tmp_path / "report_compat.json"
    result = _run_tests_cli(suite=suite_fail, policy=fixtures / "policy.json", report=report)
    assert result.returncode == 1
    payload = _load_json(report)
    assert payload["summary"]["failed"] == 1
    assert payload["results"][0]["status"] == "fail"


def test_evolving_fail_warns_and_exit_0(tmp_path: Path) -> None:
    fixtures = _fixtures_dir()
    report = tmp_path / "report_evolving.json"
    result = _run_tests_cli(suite=fixtures / "suite_evolving.json", policy=fixtures / "policy.json", report=report)
    assert result.returncode == 0
    payload = _load_json(report)
    assert payload["summary"]["failed"] == 0
    assert payload["summary"]["warnings"] == 1
    assert payload["results"][0]["status"] == "warn"


def test_report_fields_are_stable(tmp_path: Path) -> None:
    fixtures = _fixtures_dir()
    report = tmp_path / "report_hard_pass.json"
    result = _run_tests_cli(suite=fixtures / "suite_hard.json", policy=fixtures / "policy.json", report=report)
    assert result.returncode == 0, result.stderr
    payload = _load_json(report)
    assert set(payload.keys()) == {"summary", "results"}
    assert set(payload["summary"].keys()) == {"total", "passed", "failed", "warnings"}
    result0 = payload["results"][0]
    assert {"test_id", "stability", "intent_refs", "status", "reason"} <= set(result0.keys())


def test_runner_determinism_report_bytes_identical(tmp_path: Path) -> None:
    fixtures = _fixtures_dir()
    report_a = tmp_path / "report_a.json"
    report_b = tmp_path / "report_b.json"
    result_a = _run_tests_cli(suite=fixtures / "suite_hard.json", policy=fixtures / "policy.json", report=report_a)
    result_b = _run_tests_cli(suite=fixtures / "suite_hard.json", policy=fixtures / "policy.json", report=report_b)
    assert result_a.returncode == 0
    assert result_b.returncode == 0
    assert report_a.read_bytes() == report_b.read_bytes()
