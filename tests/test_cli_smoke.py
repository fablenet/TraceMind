import shutil
import subprocess
import sys

import pytest

pytest.importorskip("networkx", reason="CLI requires networkx dependency")


def _run_cli(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, text=True)


def _assert_version(result: subprocess.CompletedProcess[str]) -> None:
    assert result.returncode == 0
    output = (result.stdout or "").strip()
    assert output
    assert "tm" in output.lower()


def test_tm_console_version():
    tm_executable = shutil.which("tm")
    if tm_executable:
        command = [tm_executable, "--version"]
    else:
        # Fallback to module invocation when script entry point is not installed yet
        command = [sys.executable, "-m", "tm.cli", "--version"]
    _assert_version(_run_cli(command))


def test_tm_module_version():
    command = [sys.executable, "-m", "tm", "--version"]
    _assert_version(_run_cli(command))
