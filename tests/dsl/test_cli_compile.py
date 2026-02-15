from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("networkx", reason="CLI requires networkx dependency")
pytest.importorskip("yaml", reason="PyYAML required for DSL compilation")


WDL_SAMPLE = """
version: dsl/v0
workflow: plant-monitor
triggers:
  cron:
    schedule: "* * * * *"
inputs:
  endpoint: string
  nodes: list<string>
steps:
  - read(opcua.read):
      endpoint: $input.endpoint
      node_ids: $input.nodes
outputs:
  decision: $step.read
"""


PDL_SAMPLE = """
version: pdl/v0
arms:
  default:
    threshold: 75.0
    action_on_violation: WRITE_BACK
    target_node: ns=2;i=5001
evaluate:
  action = "WRITE_BACK"
emit:
  action: action
"""


def _run_cli(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=True, text=True, cwd=str(cwd))


def test_tm_dsl_compile(tmp_path: Path) -> None:
    src_dir = tmp_path / "dsl"
    src_dir.mkdir()
    (src_dir / "sample.wdl").write_text(WDL_SAMPLE.strip(), encoding="utf-8")
    (src_dir / "sample.pdl").write_text(PDL_SAMPLE.strip(), encoding="utf-8")

    result = _run_cli([sys.executable, "-m", "tm.cli", "dsl", "compile", str(src_dir)], cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    assert "flow" in result.stdout
    out_dir = tmp_path / "out"
    flow_dir = out_dir / "flows"
    policy_dir = out_dir / "policies"
    triggers_file = out_dir / "triggers.yaml"
    assert flow_dir.exists()
    assert policy_dir.exists()
    assert triggers_file.exists()


def test_tm_dsl_compile_json(tmp_path: Path) -> None:
    src_dir = tmp_path / "dsl"
    src_dir.mkdir()
    (src_dir / "sample.wdl").write_text(WDL_SAMPLE.strip(), encoding="utf-8")
    result = _run_cli(
        [sys.executable, "-m", "tm.cli", "dsl", "compile", str(src_dir), "--json", "--out", str(tmp_path / "build")],
        cwd=tmp_path,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    kinds = {item["kind"] for item in payload["artifacts"]}
    assert "trigger" in kinds
    build_dir = tmp_path / "build"
    assert (build_dir / "triggers.yaml").exists()
