"""CLI tests for ``tm verify network --mode compositional`` — Stage 7-V.6."""

from __future__ import annotations

import json
from pathlib import Path

from tm.cli.verify_network import run_verify_network

FIXTURES = Path("tests/fixtures/network_violation")


def _args(**overrides):
    base = {
        "agent_network": str(FIXTURES / "agent_network.yaml"),
        "bundle": [
            f"bundle.center={FIXTURES / 'bundle.center.yaml'}",
            f"bundle.leaf_a={FIXTURES / 'bundle.leaf_a.yaml'}",
            f"bundle.leaf_b={FIXTURES / 'bundle.leaf_b.yaml'}",
        ],
        "formulas": str(FIXTURES / "formulas.yaml"),
        "max_depth": 16,
        "hash_mode": "store",
        "mode": "monolithic",
        "format": "text",
    }
    base.update(overrides)
    return type("Args", (), base)()


def test_default_mode_is_monolithic_text(capsys):
    run_verify_network(_args())
    out = capsys.readouterr().out
    assert "mode=monolithic" in out


def test_compositional_mode_text_shows_reduction(capsys):
    run_verify_network(_args(mode="compositional", format="text"))
    out = capsys.readouterr().out
    assert "mode=compositional" in out
    # the fixture's safety formula triggers a spurious-FAIL recheck, so the
    # monolithic comparison number is present.
    assert "compositional=" in out and "monolithic=" in out
    assert "fallback[" in out


def test_compositional_json_exposes_additive_fields(capsys):
    run_verify_network(_args(mode="compositional", format="json"))
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "compositional"
    for key in ("abstraction_stats", "compositional_state_count", "fallbacks", "local_verdicts"):
        assert key in payload


def test_monolithic_json_has_no_compositional_keys(capsys):
    run_verify_network(_args(mode="monolithic", format="json"))
    payload = json.loads(capsys.readouterr().out)
    assert "mode" not in payload  # byte-identical legacy shape
    assert "abstraction_stats" not in payload


def test_compositional_matches_monolithic_exit_code(capsys):
    mono_code = run_verify_network(_args(mode="monolithic", format="json"))
    capsys.readouterr()
    comp_code = run_verify_network(_args(mode="compositional", format="json"))
    capsys.readouterr()
    assert mono_code == comp_code  # verdict parity → identical pass/fail


def test_compositional_verdict_parity(capsys):
    run_verify_network(_args(mode="monolithic", format="json"))
    mono = json.loads(capsys.readouterr().out)
    run_verify_network(_args(mode="compositional", format="json"))
    comp = json.loads(capsys.readouterr().out)
    assert [v["satisfied"] for v in mono["verdicts"]] == [v["satisfied"] for v in comp["verdicts"]]
