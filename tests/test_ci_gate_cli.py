from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping


def _repo_root() -> Path:
    return Path(__file__).resolve().parent


def _fixture(*parts: str) -> Path:
    return _repo_root() / "fixtures" / "ci_gate" / "v0.1" / Path(*parts)


def _run_ci_gate(*, proposals_glob: str, intents: Path, policy: Path) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "tm",
            "ci",
            "gate",
            "--proposals-glob",
            proposals_glob,
            "--intents",
            str(intents),
            "--policy",
            str(policy),
        ],
        cwd=_repo_root(),
        capture_output=True,
    )


def _json(result: subprocess.CompletedProcess[bytes]) -> Mapping[str, Any]:
    return json.loads(result.stdout.decode("utf-8"))


def test_ci_gate_fails_if_any_proposal_fails() -> None:
    glob_pattern = str(_fixture("proposals", "proposal_*.json"))
    result = _run_ci_gate(
        proposals_glob=glob_pattern,
        intents=_fixture("proposals", "intents.json"),
        policy=_fixture("proposals", "policy.json"),
    )
    assert result.returncode == 1
    payload = _json(result)
    assert payload["summary"]["total"] == 2
    assert payload["summary"]["failed"] == 1
    statuses = {item["proposal"]: item["status"] for item in payload["results"]}
    assert any(status == "fail" for status in statuses.values())


def test_ci_gate_passes_when_no_proposals_found() -> None:
    glob_pattern = str(_fixture("proposals", "no_match", "*.json"))
    result = _run_ci_gate(
        proposals_glob=glob_pattern,
        intents=_fixture("proposals", "intents.json"),
        policy=_fixture("proposals", "policy.json"),
    )
    assert result.returncode == 0
    payload = _json(result)
    assert payload["summary"]["total"] == 0
    assert b"no proposals found" in result.stderr
