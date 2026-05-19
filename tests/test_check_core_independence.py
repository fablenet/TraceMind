"""Tests for ``scripts/check_core_independence.py``.

Pinned in CI so trace-mind core stays domain-neutral (Phase 5 Stage 5-2
task 2.8). When this test fails, the offending PR introduced either:

- an import of ``fablenet*`` or ``connector*`` (or a generated protobuf
  binding) under ``tm/``, OR
- a hard-coded FableNet-specific identifier (e.g. ``FNET-INT-001``,
  ``"fablenet:..."``, ``"fablenet-agent/..."``) under ``tm/``.

In both cases the fix is: parameterize the leakage point or move the
domain-specific code to its downstream consumer.
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

import check_core_independence as cci  # noqa: E402


def _run(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[int, str, str]:
    """Run main() with TM_ROOT pointing at ``tmp_path``."""
    monkeypatch.setattr(cci, "TM_ROOT", tmp_path)
    out_buf = io.StringIO()
    err_buf = io.StringIO()
    with redirect_stdout(out_buf), redirect_stderr(err_buf):
        rc = cci.main([])
    return rc, out_buf.getvalue(), err_buf.getvalue()


class TestCurrentTreeClean:
    def test_current_tm_tree_passes(self) -> None:
        """The live ``tm/`` tree must always pass this check."""
        rc = cci.main([])
        assert rc == 0, "trace-mind core has leaked domain-specific code"


class TestForbiddenImports:
    def test_fablenet_import_blocked(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        (tmp_path / "leak.py").write_text("from fablenet.core import Thing\n")
        rc, _, err = _run(monkeypatch, tmp_path)
        assert rc == 1
        assert "fablenet" in err
        assert "forbidden import" in err

    def test_connector_import_blocked(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        (tmp_path / "leak.py").write_text("import connector.client\n")
        rc, _, err = _run(monkeypatch, tmp_path)
        assert rc == 1
        assert "connector" in err

    def test_pb2_import_blocked(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        (tmp_path / "leak.py").write_text("from some.pkg import control_pb2\n")
        rc, _, err = _run(monkeypatch, tmp_path)
        assert rc == 1
        assert "protobuf" in err

    def test_pb2_grpc_import_blocked(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        (tmp_path / "leak.py").write_text("from x.y import orchestration_pb2_grpc\n")
        rc, _, err = _run(monkeypatch, tmp_path)
        assert rc == 1
        assert "protobuf" in err

    def test_normal_imports_allowed(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        (tmp_path / "ok.py").write_text(
            "from tm.policy.deterministic import PolicyEngine\nimport json\nfrom collections.abc import Mapping\n"
        )
        rc, _, _ = _run(monkeypatch, tmp_path)
        assert rc == 0


class TestForbiddenStrings:
    def test_fnet_intent_id_blocked(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        (tmp_path / "leak.py").write_text('VALUE = "FNET-INT-001"\n')
        rc, _, err = _run(monkeypatch, tmp_path)
        assert rc == 1
        assert "FNET-INT-001" in err

    def test_fablenet_actuator_prefix_blocked(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        (tmp_path / "leak.py").write_text('uri = "fablenet:throttle"\n')
        rc, _, err = _run(monkeypatch, tmp_path)
        assert rc == 1
        assert "fablenet:throttle" in err

    def test_fablenet_agent_id_blocked(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        (tmp_path / "leak.py").write_text('AGENT = "fablenet-agent/observe:0.1"\n')
        rc, _, err = _run(monkeypatch, tmp_path)
        assert rc == 1
        assert "fablenet-agent" in err

    def test_neutral_placeholder_allowed(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        (tmp_path / "ok.py").write_text('UNSPECIFIED = "intent.unspecified"\n')
        rc, _, _ = _run(monkeypatch, tmp_path)
        assert rc == 0

    def test_unrelated_string_with_substring_allowed(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        (tmp_path / "ok.py").write_text('# describes a fablenet-style integration\nVALUE = "ok"\n')
        rc, _, _ = _run(monkeypatch, tmp_path)
        assert rc == 0


class TestSyntaxErrorHandling:
    def test_syntax_error_reported_not_crashed(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        (tmp_path / "broken.py").write_text("def x(:\n")
        rc, _, err = _run(monkeypatch, tmp_path)
        assert rc == 1
        assert "SyntaxError" in err
