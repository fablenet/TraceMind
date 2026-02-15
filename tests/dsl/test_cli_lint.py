from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("networkx", reason="CLI requires networkx dependency")


def _run_cli(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=True, text=True, cwd=str(cwd))


def test_tm_dsl_lint_success(tmp_path: Path) -> None:
    workflow_path = tmp_path / "sample.wdl"
    workflow_path.write_text(
        "\n".join(
            [
                "version: dsl/v0",
                "workflow: ok",
                "inputs:",
                "  location: string",
                "steps:",
                "  - hello(echo.say):",
                "      message: $input.location",
                "outputs:",
                "  last: $step.hello",
            ]
        ),
        encoding="utf-8",
    )
    result = _run_cli([sys.executable, "-m", "tm.cli", "dsl", "lint", str(workflow_path)], cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    assert "No issues found" in result.stdout


def test_tm_dsl_lint_reports_missing_input(tmp_path: Path) -> None:
    workflow_path = tmp_path / "bad.wdl"
    workflow_path.write_text(
        "\n".join(
            [
                "version: dsl/v0",
                "workflow: sample",
                "steps:",
                "  - action(task.run):",
                "      param: $input.missing",
            ]
        ),
        encoding="utf-8",
    )
    result = _run_cli([sys.executable, "-m", "tm.cli", "dsl", "lint", str(workflow_path)], cwd=tmp_path)
    assert result.returncode == 1
    assert "missing-input" in result.stdout
