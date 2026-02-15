from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def test_lint_detects_cycle(tmp_path):
    flow_file = tmp_path / "flow.yml"
    flow_file.write_text("""
steps:
  a: { next: [b] }
  b: { next: [a] }
""".strip())
    result = subprocess.run(
        [sys.executable, "-m", "tm", "flow", "lint", str(flow_file)],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "cycle" in result.stdout.lower()


def test_lint_ok_flow(tmp_path):
    project_root = Path(__file__).resolve().parents[2]
    flow_path = project_root / "fixtures" / "flows" / "tiny_ok.yml"
    result = subprocess.run(
        [sys.executable, "-m", "tm", "flow", "lint", str(flow_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert "ok" in result.stdout.lower()
