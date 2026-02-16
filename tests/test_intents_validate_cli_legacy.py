from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def _repo_root() -> Path:
    return Path(__file__).resolve().parent


def _fixture(name: str) -> Path:
    return _repo_root() / "fixtures" / "intents" / name


def _run_validate(path: Path, *, as_json: bool = False) -> subprocess.CompletedProcess[bytes]:
    cmd = [
        sys.executable,
        "-m",
        "tm",
        "intents",
        "validate",
        "--intents",
        str(path),
    ]
    if as_json:
        cmd.append("--json")
    return subprocess.run(cmd, cwd=_repo_root(), capture_output=True)


def test_valid_exit_0() -> None:
    result = _run_validate(_fixture("valid_intent_tree.json"))
    assert result.returncode == 0, result.stderr.decode("utf-8")
    assert b"valid" in result.stdout


def test_invalid_duplicate_id_exit_1() -> None:
    result = _run_validate(_fixture("invalid_duplicate_id.json"))
    assert result.returncode == 1
    out = result.stdout.decode("utf-8")
    assert "TM-INT-DUP" in out
    assert "duplicate id" in out
    assert "intents[1].id" in out or "intents[2].id" in out


def test_invalid_missing_parent_exit_1() -> None:
    result = _run_validate(_fixture("invalid_missing_parent.json"))
    assert result.returncode == 1
    out = result.stdout.decode("utf-8")
    assert "TM-INT-ORPHAN" in out
    assert "parent_intent" in out


def test_invalid_cycle_exit_1() -> None:
    result = _run_validate(_fixture("invalid_cycle.json"))
    assert result.returncode == 1
    out = result.stdout.decode("utf-8")
    assert "cycle detected" in out
    assert "trace_links.parent_intent" in out


def test_invalid_leaf_missing_success_criteria_exit_1() -> None:
    result = _run_validate(_fixture("invalid_leaf_missing_success_criteria.json"))
    assert result.returncode == 1
    out = result.stdout.decode("utf-8")
    assert "TM-INT-LEAF" in out
    assert "success_criteria" in out


def test_json_deterministic_bytes() -> None:
    target = _fixture("invalid_missing_parent.json")
    result_a = _run_validate(target, as_json=True)
    result_b = _run_validate(target, as_json=True)
    assert result_a.returncode == 1
    assert result_b.returncode == 1
    assert result_a.stdout == result_b.stdout
    assert result_a.stdout.endswith(b"\n")
