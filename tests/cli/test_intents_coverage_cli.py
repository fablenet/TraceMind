from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _fixture(*parts: str) -> Path:
    return Path(__file__).resolve().parents[1] / "fixtures" / "intents" / "coverage" / "v0.1" / Path(*parts)


def _run_coverage(*, intents: Path, tests: Path, policy: Path | None = None) -> subprocess.CompletedProcess[bytes]:
    cmd = [
        sys.executable,
        "-m",
        "tm",
        "intents",
        "coverage",
        "--intents",
        str(intents),
        "--tests",
        str(tests),
    ]
    if policy is not None:
        cmd.extend(["--policy", str(policy)])
    return subprocess.run(
        cmd,
        cwd=_repo_root(),
        capture_output=True,
    )


def _load_report(result: subprocess.CompletedProcess[bytes]) -> Mapping[str, Any]:
    return json.loads(result.stdout.decode("utf-8"))


def test_intents_coverage_uncovered_leaf_exit_1() -> None:
    result = _run_coverage(
        intents=_fixture("intent_tree.json"),
        tests=_fixture("suite_uncovered_leaf.json"),
    )
    assert result.returncode == 1, result.stderr.decode("utf-8")
    report = _load_report(result)
    assert report["uncovered_leaf_intents"] == ["TM-INT-LEAF-B"]
    assert report["summary"]["uncovered_leaf_intents"] == 1


def test_intents_coverage_full_exit_0() -> None:
    result = _run_coverage(
        intents=_fixture("intent_tree.json"),
        tests=_fixture("suite_full_coverage.json"),
    )
    assert result.returncode == 0, result.stderr.decode("utf-8")
    report = _load_report(result)
    assert report["uncovered_leaf_intents"] == []
    assert report["summary"]["uncovered_leaf_intents"] == 0
    assert report["summary"]["warnings"] == 0


def test_intents_coverage_output_is_deterministic_bytes() -> None:
    result_a = _run_coverage(
        intents=_fixture("intent_tree.json"),
        tests=_fixture("suite_full_coverage.json"),
        policy=_fixture("policy_with_intent_refs.json"),
    )
    result_b = _run_coverage(
        intents=_fixture("intent_tree.json"),
        tests=_fixture("suite_full_coverage.json"),
        policy=_fixture("policy_with_intent_refs.json"),
    )
    assert result_a.returncode == 0, result_a.stderr.decode("utf-8")
    assert result_b.returncode == 0, result_b.stderr.decode("utf-8")
    assert result_a.stdout == result_b.stdout
    report = _load_report(result_a)
    assert "summary" in report
    assert {"uncovered_leaf_intents", "intents_without_tests", "tests_without_intents"} <= set(report.keys())
