from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _fixture(*parts: str) -> Path:
    return Path(__file__).resolve().parents[1] / "fixtures" / "intents" / "v0.1" / Path(*parts)


def _run_validate(path: Path, *, as_json: bool = False) -> subprocess.CompletedProcess[str]:
    cmd = [sys.executable, "-m", "tm", "intents", "validate", str(path)]
    if as_json:
        cmd.append("--json")
    return subprocess.run(
        cmd,
        cwd=_repo_root(),
        text=True,
        capture_output=True,
    )


def test_intents_validate_valid_fixture() -> None:
    result = _run_validate(_fixture("valid", "tree_valid.json"))
    assert result.returncode == 0, result.stderr
    assert "valid" in result.stdout


def test_intents_validate_invalid_fixtures_exit_1() -> None:
    fixtures = [
        ("tree_missing_parent.json", "parent_intent"),
        ("tree_cycle.json", "cycle"),
        ("tree_duplicate_id.json", "duplicate id"),
        ("tree_leaf_missing_success_criteria.json", "success_criteria"),
    ]
    for filename, snippet in fixtures:
        result = _run_validate(_fixture("invalid", filename))
        assert result.returncode == 1, f"{filename} expected exit=1\nstdout={result.stdout}\nstderr={result.stderr}"
        assert snippet in result.stdout


def test_intents_validate_json_output_shape() -> None:
    target = _fixture("invalid", "tree_cycle.json")
    result = _run_validate(target, as_json=True)
    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert isinstance(payload, list)
    assert payload
    row = payload[0]
    assert set(row.keys()) == {"file", "intent_id", "path", "message"}
    assert row["file"].endswith("tree_cycle.json")
