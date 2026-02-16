from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping


def _repo_root() -> Path:
    return Path(__file__).resolve().parent


def _fixture(*parts: str) -> Path:
    return _repo_root() / "fixtures" / "proposal" / "v0.1" / Path(*parts)


def _run_validate(proposal: Path, *, as_json: bool = False) -> subprocess.CompletedProcess[bytes]:
    cmd = [
        sys.executable,
        "-m",
        "tm",
        "proposal",
        "validate",
        "--proposal",
        str(proposal),
        "--intents",
        str(_fixture("intent_tree.json")),
    ]
    if as_json:
        cmd.append("--json")
    return subprocess.run(cmd, cwd=_repo_root(), capture_output=True)


def _parse_json_report(result: subprocess.CompletedProcess[bytes]) -> Mapping[str, Any]:
    return json.loads(result.stdout.decode("utf-8"))


def test_proposal_validate_valid_exit_0() -> None:
    result = _run_validate(_fixture("proposal_valid.json"))
    assert result.returncode == 0, result.stderr.decode("utf-8")
    assert b"valid" in result.stdout


def test_proposal_validate_invalid_unknown_impacted_exit_1() -> None:
    result = _run_validate(_fixture("proposal_invalid_unknown_impacted.json"))
    assert result.returncode == 1
    out = result.stdout.decode("utf-8")
    assert "PROPOSAL_IMPACTED_UNKNOWN_INTENT" in out
    assert "spec.impacted_intents[0]" in out


def test_proposal_validate_invalid_missing_refs_exit_1() -> None:
    result = _run_validate(_fixture("proposal_invalid_missing_refs.json"))
    assert result.returncode == 1
    out = result.stdout.decode("utf-8")
    assert "PROPOSAL_PATCH_REF_MISSING" in out
    assert "PROPOSAL_TEST_REF_MISSING" in out


def test_proposal_validate_invalid_testsuite_intent_exit_1() -> None:
    result = _run_validate(_fixture("proposal_invalid_testsuite_intent.json"))
    assert result.returncode == 1
    out = result.stdout.decode("utf-8")
    assert "PROPOSAL_TEST_INTENT_UNKNOWN" in out
    assert "spec.testsuite_refs[0]#spec.tests[0].intent_refs[0]" in out


def test_proposal_validate_invalid_risk_exit_1() -> None:
    result = _run_validate(_fixture("proposal_invalid_risk.json"))
    assert result.returncode == 1
    out = result.stdout.decode("utf-8")
    assert "PROPOSAL_RISK_INVALID" in out
    assert "spec.risk" in out


def test_proposal_validate_json_deterministic_bytes() -> None:
    proposal = _fixture("proposal_invalid_missing_refs.json")
    result_a = _run_validate(proposal, as_json=True)
    result_b = _run_validate(proposal, as_json=True)
    assert result_a.returncode == 1
    assert result_b.returncode == 1
    assert result_a.stdout == result_b.stdout
    assert result_a.stdout.endswith(b"\n")
    report = _parse_json_report(result_a)
    assert set(report.keys()) == {"summary", "errors", "warnings"}
    assert set(report["summary"].keys()) == {"errors", "warnings"}
