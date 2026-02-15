from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _intent_artifact_payload(
    *, artifact_id: str, intent_id: str, invariant_status: dict[str, bool], status: str = "accepted"
) -> dict:
    return {
        "envelope": {
            "artifact_id": artifact_id,
            "status": status,
            "artifact_type": "intent",
            "version": "v0",
            "created_by": "test",
            "created_at": "2025-01-01T00:00:00Z",
            "body_hash": "body-hash-shared",
            "envelope_hash": "env-hash",
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


def test_artifacts_consistency_cli_detects_c3_regression(tmp_path: Path) -> None:
    registry_path = tmp_path / "registry.jsonl"
    previous_artifact = tmp_path / "accepted_prev.yaml"
    current_artifact = tmp_path / "accepted_current.yaml"
    intent_id = "TM-INT-777"

    previous_payload = _intent_artifact_payload(
        artifact_id="intent-prev", intent_id=intent_id, invariant_status={"slo_met": True}
    )
    current_payload = _intent_artifact_payload(
        artifact_id="intent-current", intent_id=intent_id, invariant_status={"slo_met": False}
    )
    previous_artifact.write_text(json.dumps(previous_payload, indent=2), encoding="utf-8")
    current_artifact.write_text(json.dumps(current_payload, indent=2), encoding="utf-8")

    registry_entry = {
        "artifact_id": previous_payload["envelope"]["artifact_id"],
        "artifact_type": previous_payload["envelope"]["artifact_type"],
        "body_hash": previous_payload["envelope"]["body_hash"],
        "path": str(previous_artifact),
        "meta": previous_payload["envelope"]["meta"],
        "version": previous_payload["envelope"]["version"],
        "created_at": previous_payload["envelope"]["created_at"],
        "status": previous_payload["envelope"]["status"],
        "intent_id": intent_id,
    }
    registry_path.write_text(json.dumps(registry_entry) + "\n", encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "tm",
            "artifacts",
            "consistency",
            "--artifact",
            str(current_artifact),
            "--registry",
            str(registry_path),
        ],
        capture_output=True,
        text=True,
        cwd=_repo_root(),
    )

    assert result.returncode == 1
    assert "C3" in result.stdout
    assert "regresses invariants" in result.stdout
