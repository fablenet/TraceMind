from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_AST_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "ast" / "v0.1"
_VALID_DIR = _AST_FIXTURES / "valid"
_INVALID_DIR = _AST_FIXTURES / "invalid"


def _run_tm(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "tm", *args],
        capture_output=True,
        text=True,
        cwd=_REPO_ROOT,
    )


def test_validate_ast_valid_directory() -> None:
    result = _run_tm("validate", str(_VALID_DIR))
    assert result.returncode == 0, result.stderr
    assert "canonical AST valid" in result.stdout


def test_validate_ast_invalid_directory_reports_paths() -> None:
    result = _run_tm("validate", str(_INVALID_DIR))
    assert result.returncode == 1
    assert "$.spec.tests[0]" in result.stderr
    assert "canonical AST invalid" in result.stderr


def test_validate_ast_invalid_directory_json_output() -> None:
    result = _run_tm("validate", "--json", str(_INVALID_DIR))
    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert "ast" in payload
    entries = {Path(item["file"]).name: item for item in payload["ast"]}
    assert "testsuite_missing_test_stability.json" in entries
    errors = entries["testsuite_missing_test_stability.json"]["errors"]
    assert any(error["json_path"] == "$.spec.tests[0]" and "stability" in error["message"] for error in errors)


def test_validate_ast_single_file_with_flag() -> None:
    proposal = _VALID_DIR / "proposal_patch_guard.json"
    result = _run_tm("validate", "--ast", str(proposal))
    assert result.returncode == 0, result.stderr
