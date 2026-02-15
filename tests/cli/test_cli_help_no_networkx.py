from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def test_cli_help_fmt_validate_without_networkx_import() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    script = """
import builtins

_orig_import = builtins.__import__

def _blocked_import(name, globals=None, locals=None, fromlist=(), level=0):
    if name == "networkx" or name.startswith("networkx."):
        raise ModuleNotFoundError("No module named 'networkx'")
    return _orig_import(name, globals, locals, fromlist, level)

builtins.__import__ = _blocked_import

from tm.cli import _build_parser

parser = _build_parser()
for argv in (["fmt", "--help"], ["validate", "--help"]):
    try:
        parser.parse_args(argv)
    except SystemExit as exc:
        if exc.code != 0:
            raise
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=repo_root,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr
