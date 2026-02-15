from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def _fixture(name: str) -> Path:
    return Path(__file__).resolve().parents[2] / "fixtures" / "flows" / name


def test_simulate_cli_detects_deadlock(tmp_path):
    flow_path = tmp_path / "deadlock.yml"
    flow_path.write_text("""
steps:
  A:
    locks:
      - {name: db, mode: exclusive}
      - {name: cache, mode: exclusive}
  B:
    locks:
      - {name: cache, mode: exclusive}
      - {name: db, mode: exclusive}
""".strip())
    result = subprocess.run(
        [sys.executable, "-m", "tm", "simulate", "run", "--flow", str(flow_path), "--json"],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    payload = json.loads(result.stdout)
    assert payload["report"]["deadlocks"] >= 1


def test_simulate_cli_ok(tmp_path):
    flow_path = tmp_path / "ok.yml"
    flow_path.write_text("""
steps:
  start:
    locks:
      - {name: db, mode: shared}
    next: [finish]
  finish: {}
""".strip())
    result = subprocess.run(
        [sys.executable, "-m", "tm", "simulate", "run", "--flow", str(flow_path)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "deadlocks: 0" in result.stdout
