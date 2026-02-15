from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _intent_candidate_payload(
    *, artifact_id: str, intent_id: str, invariant_status: dict[str, bool], status: str = "candidate"
) -> dict:
    return {
        "envelope": {
            "artifact_id": artifact_id,
            "status": status,
            "artifact_type": "intent",
            "version": "v0",
            "created_by": "test",
            "created_at": "2025-01-01T00:00:00Z",
            "body_hash": "",
            "envelope_hash": "",
            "meta": {"invariant_status": invariant_status},
        },
        "body": {
            "intent_id": intent_id,
            "title": "Test Intent",
            "context": "ctx",
            "goal": "goal",
            "non_goals": [],
            "actors": [],
            "inputs": [],
            "outputs": [],
            "constraints": [],
            "success_metrics": [],
            "risks": [],
            "assumptions": [],
            "trace_links": {"related_intents": []},
        },
    }


def _run_tm(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "tm", *args],
        capture_output=True,
        text=True,
        cwd=_repo_root(),
    )


def _write_artifact(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _registry_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_artifacts_accept_blocks_registry_on_c3_regression(tmp_path: Path) -> None:
    registry_path = tmp_path / "registry.jsonl"
    out_dir = tmp_path / "accepted"
    out_dir.mkdir()

    intent_id = "TM-INT-900"
    first_candidate = tmp_path / "candidate_a.yaml"
    second_candidate = tmp_path / "candidate_b.yaml"

    _write_artifact(
        first_candidate,
        _intent_candidate_payload(
            artifact_id="intent-a",
            intent_id=intent_id,
            invariant_status={"slo_met": True},
        ),
    )
    _write_artifact(
        second_candidate,
        _intent_candidate_payload(
            artifact_id="intent-b",
            intent_id=intent_id,
            invariant_status={"slo_met": False},
        ),
    )

    success = _run_tm(
        [
            "artifacts",
            "accept",
            str(first_candidate),
            "--out",
            str(out_dir),
            "--registry",
            str(registry_path),
        ]
    )
    assert success.returncode == 0, success.stderr
    assert len(_registry_lines(registry_path)) == 1

    failure = _run_tm(
        [
            "artifacts",
            "accept",
            str(second_candidate),
            "--out",
            str(out_dir),
            "--registry",
            str(registry_path),
        ]
    )
    assert failure.returncode == 1
    assert "C3" in failure.stdout
    assert len(_registry_lines(registry_path)) == 1
