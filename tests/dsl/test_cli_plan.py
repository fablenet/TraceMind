from __future__ import annotations

import subprocess
import sys
from pathlib import Path

PYTHON = sys.executable


WDL_SAMPLE = """
version: dsl/v0
workflow: cli
steps:
  - hello(op.echo):
      value: 1
outputs:
  result: $step.hello
"""


def test_tm_dsl_plan_cli(tmp_path: Path) -> None:
    wdl_path = tmp_path / "flow.wdl"
    wdl_path.write_text(WDL_SAMPLE, encoding="utf-8")
    dot_path = tmp_path / "plan.dot"
    json_path = tmp_path / "plan.json"

    result = subprocess.run(
        [
            PYTHON,
            "-m",
            "tm.cli",
            "dsl",
            "plan",
            str(wdl_path),
            "--dot",
            str(dot_path),
            "--json",
            str(json_path),
        ],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
    )
    assert result.returncode == 0, result.stderr
    assert dot_path.exists()
    assert json_path.exists()
    assert "nodes=" in result.stdout
