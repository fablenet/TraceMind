from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping


def _repo_root() -> Path:
    return Path(__file__).resolve().parent


def _fixture(*parts: str) -> Path:
    return _repo_root() / "fixtures" / "proposals" / "v0.1" / Path(*parts)


def _run_gate(
    *,
    proposal: str,
    policy: str = "policy_pass.json",
    registry: str | None = None,
    trace: str | None = None,
    json_report: Path | None = None,
) -> subprocess.CompletedProcess[bytes]:
    cmd = [
        sys.executable,
        "-m",
        "tm",
        "gate",
        "proposal",
        "--proposal",
        str(_fixture(proposal)),
        "--intents",
        str(_fixture("intents.json")),
        "--policy",
        str(_fixture(policy)),
    ]
    if registry is not None:
        cmd.extend(["--registry", str(_fixture(registry))])
    if trace is not None:
        cmd.extend(["--trace", str(_fixture(trace))])
    if json_report is not None:
        cmd.extend(["--json-report", str(json_report)])
    return subprocess.run(cmd, cwd=_repo_root(), capture_output=True)


def _load_report(result: subprocess.CompletedProcess[bytes]) -> Mapping[str, Any]:
    return json.loads(result.stdout.decode("utf-8"))


def test_gate_proposal_ok_exit_0() -> None:
    result = _run_gate(proposal="proposal_ok.json")
    assert result.returncode == 0, result.stderr.decode("utf-8")
    report = _load_report(result)
    assert report["summary"]["status"] == "pass"
    assert len(report["steps"]) == 5
    assert [step["name"] for step in report["steps"]] == [
        "schema_validate",
        "intents_validate",
        "proposal_lint_validate",
        "run_tests",
        "consistency_gate",
    ]


def test_gate_proposal_missing_impacted_fails_step3() -> None:
    result = _run_gate(proposal="proposal_missing_impacted.json")
    assert result.returncode == 1
    report = _load_report(result)
    assert report["summary"]["failed_step"] == "proposal_lint_validate"


def test_gate_proposal_breaks_hard_test_fails_step4() -> None:
    result = _run_gate(
        proposal="proposal_breaks_hard_test.json",
        policy="policy_break.json",
    )
    assert result.returncode == 1
    report = _load_report(result)
    assert report["summary"]["failed_step"] == "run_tests"


def test_gate_proposal_c3_regression_fails_step5() -> None:
    result = _run_gate(
        proposal="proposal_c3_regression.json",
        policy="policy_pass.json",
        registry="registry.jsonl",
        trace="artifacts/current_intent.yaml",
    )
    assert result.returncode == 1
    report = _load_report(result)
    assert report["summary"]["failed_step"] == "consistency_gate"
    step5 = report["steps"][-1]
    assert any("C3:" in row["message"] for row in step5["errors"])


def test_gate_proposal_report_deterministic_bytes() -> None:
    report_a = Path("tmp_gate_report_a.json")
    report_b = Path("tmp_gate_report_b.json")
    try:
        result_a = _run_gate(proposal="proposal_ok.json", json_report=_repo_root() / report_a)
        result_b = _run_gate(proposal="proposal_ok.json", json_report=_repo_root() / report_b)
        assert result_a.returncode == 0
        assert result_b.returncode == 0
        assert result_a.stdout == result_b.stdout
        assert (_repo_root() / report_a).read_bytes() == (_repo_root() / report_b).read_bytes()
    finally:
        (_repo_root() / report_a).unlink(missing_ok=True)
        (_repo_root() / report_b).unlink(missing_ok=True)
