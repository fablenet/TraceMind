"""Tests for ``scripts/check_no_llm_in_completeness.py`` (Phase 7 Stage 7-0.7).

Pins the zero-LLM discipline for the 5W1H completeness + uncertainty modules:
they must judge structural completeness deterministically and never import any
LLM/AI machinery (invariant 3).
"""

from __future__ import annotations

import io
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"

if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import check_no_llm_in_completeness as guard  # noqa: E402


def _run(monkeypatch: pytest.MonkeyPatch, *files: Path) -> tuple[int, str, str]:
    monkeypatch.setattr(guard, "GUARDED_FILES", tuple(files))
    out_buf, err_buf = io.StringIO(), io.StringIO()
    with redirect_stdout(out_buf), redirect_stderr(err_buf):
        rc = guard.main([])
    return rc, out_buf.getvalue(), err_buf.getvalue()


class TestLiveTreeClean:
    def test_real_guarded_modules_pass(self) -> None:
        rc = guard.main([])
        assert rc == 0, "completeness/uncertainty leaked an LLM import"


class TestForbiddenImports:
    def test_tm_ai_import_blocked(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        leak = tmp_path / "leak.py"
        leak.write_text("from tm.ai.providers import Provider\n", encoding="utf-8")
        rc, _, err = _run(monkeypatch, leak)
        assert rc == 1
        assert "tm.ai" in err and "forbidden LLM import" in err

    def test_tm_steps_ai_blocked(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        leak = tmp_path / "leak.py"
        leak.write_text("import tm.steps.ai_propose_pattern\n", encoding="utf-8")
        rc, _, err = _run(monkeypatch, leak)
        assert rc == 1
        assert "ai pipeline step" in err.lower() or "ai_" in err

    def test_routes_llm_blocked(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        leak = tmp_path / "leak.py"
        leak.write_text("from tm.server.routes_llm import router\n", encoding="utf-8")
        rc, _, err = _run(monkeypatch, leak)
        assert rc == 1
        assert "routes_llm" in err

    def test_vendor_sdk_blocked(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        leak = tmp_path / "leak.py"
        leak.write_text("import openai\n", encoding="utf-8")
        rc, _, err = _run(monkeypatch, leak)
        assert rc == 1
        assert "vendor SDK" in err

    def test_llm_component_blocked(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        leak = tmp_path / "leak.py"
        leak.write_text("from somewhere import llm_helper\n", encoding="utf-8")
        rc, _, err = _run(monkeypatch, leak)
        assert rc == 1
        assert "llm" in err.lower()

    def test_lazy_import_inside_function_caught(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        leak = tmp_path / "leak.py"
        leak.write_text("def f():\n    from tm.ai.llm_client import call\n    return call\n", encoding="utf-8")
        rc, _, err = _run(monkeypatch, leak)
        assert rc == 1


class TestAllowed:
    def test_deterministic_imports_allowed(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        ok = tmp_path / "ok.py"
        ok.write_text(
            "from tm.policy.deterministic import canonical_json_bytes\n"
            "from tm.intent.uncertainty import Disposition\n"
            "import json\nfrom pathlib import Path\n",
            encoding="utf-8",
        )
        rc, out, _ = _run(monkeypatch, ok)
        assert rc == 0
        assert "OK" in out


class TestMissingAndSyntax:
    def test_missing_guarded_file_errors(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        rc, _, err = _run(monkeypatch, tmp_path / "nope.py")
        assert rc == 2
        assert "does not exist" in err

    def test_syntax_error_reported(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        bad = tmp_path / "bad.py"
        bad.write_text("def x(:\n", encoding="utf-8")
        rc, _, err = _run(monkeypatch, bad)
        assert rc == 1
        assert "SyntaxError" in err
