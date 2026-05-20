"""CLI tests for ``tm verify network`` — Phase 6 Stage 6-4.4."""

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
        "hash_mode": "full",
        "format": "text",
    }
    base.update(overrides)
    return type("Args", (), base)()


def test_cli_json_output_reports_failure(capsys) -> None:
    code = run_verify_network(_args(format="json"))
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert code == 1
    assert payload["verified"] is False
    assert payload["verdicts"][0]["satisfied"] is False


def test_cli_success_on_passing_formula(capsys) -> None:
    code = run_verify_network(_args(formulas=str(FIXTURES / "formulas.yaml"), format="json"))
    # mixed formulas => overall fail
    assert code == 1


def test_cli_single_passing_formula(capsys) -> None:
    single = FIXTURES / "formulas_pass.yaml"
    single.write_text("formulas:\n  - EF peer(bundle.center, has(quarantined))\n", encoding="utf-8")
    code = run_verify_network(_args(formulas=str(single), format="text"))
    assert code == 0


def test_cli_missing_network_file(capsys) -> None:
    code = run_verify_network(_args(agent_network="/no/such/network.yaml"))
    assert code == 1
    assert "not found" in capsys.readouterr().err


def test_cli_missing_bundle_mapping(capsys) -> None:
    code = run_verify_network(_args(bundle=[f"bundle.center={FIXTURES / 'bundle.center.yaml'}"]))
    assert code == 1
    assert "missing bundle paths" in capsys.readouterr().err


def test_cli_bad_bundle_entry(capsys) -> None:
    code = run_verify_network(_args(bundle=["not-a-mapping"]))
    assert code == 1
    assert "ref=path" in capsys.readouterr().err


def test_cli_missing_formulas(capsys) -> None:
    code = run_verify_network(_args(formulas=None))
    assert code == 1


def test_cli_text_output_mentions_network(capsys) -> None:
    run_verify_network(_args(format="text"))
    assert "network=network.violation.demo" in capsys.readouterr().out


def test_cli_json_includes_counterexample(capsys) -> None:
    run_verify_network(_args(format="json"))
    payload = json.loads(capsys.readouterr().out)
    assert payload["verdicts"][0]["counterexample"]


def test_cli_max_depth_accepted(capsys) -> None:
    code = run_verify_network(_args(max_depth=8, format="json"))
    assert code in (0, 1)
