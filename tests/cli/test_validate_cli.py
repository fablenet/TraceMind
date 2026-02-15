from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def _fixture(*parts: str) -> Path:
    return Path(__file__).resolve().parents[2] / "fixtures" / Path(*parts)


def test_validate_cli_detects_conflicts(tmp_path):
    policy = _fixture("policies", "arms_overlap.yml")
    flow = _fixture("flows", "exclusive_db.yml")

    result = subprocess.run(
        [sys.executable, "-m", "tm", "validate", "--flows", str(flow), "--policies", str(policy), "--json"],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    payload = json.loads(result.stdout)
    assert any(conflict["kind"] == "policy.arm_overlap" for conflict in payload["conflicts"])


def test_validate_cli_reports_lock_conflict(tmp_path):
    flow_a = tmp_path / "fa.yml"
    flow_a.write_text("""
id: flow_a
steps:
  s1:
    locks:
      - {name: db, mode: exclusive}
""".strip())
    flow_b = tmp_path / "fb.yml"
    flow_b.write_text("""
id: flow_b
steps:
  t1:
    locks:
      - {name: db, mode: shared}
""".strip())
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "tm",
            "validate",
            "--flows",
            str(flow_a),
            str(flow_b),
            "--policies",
            str(_fixture("policies", "arms_overlap.yml")),
            "--json",
        ],
        capture_output=True,
        text=True,
    )
    payload = json.loads(result.stdout)
    kinds = {conflict["kind"] for conflict in payload["conflicts"]}
    assert "flow.lock_conflict" in kinds


def test_validate_cli_no_conflict(tmp_path):
    policy = tmp_path / "policy.yml"
    policy.write_text("""
policy_id: ok
arms:
  - name: a
    if: {cost: "<=0.5"}
  - name: b
    if: {cost: ">0.5"}
""".strip())
    flow = tmp_path / "flow.yml"
    flow.write_text("id: flow_ok\nsteps: {}\n")

    result = subprocess.run(
        [sys.executable, "-m", "tm", "validate", "--flows", str(flow), "--policies", str(policy)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "no conflicts" in result.stdout.lower()
