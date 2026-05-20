"""Network violation detection fixtures — Phase 6 Stage 6-4.5."""

from __future__ import annotations

from pathlib import Path

from tm.verify.network import network_verify_from_paths

FIXTURES = Path("tests/fixtures/network_violation")


def _report():
    return network_verify_from_paths(
        FIXTURES / "agent_network.yaml",
        bundle_paths={
            "bundle.center": FIXTURES / "bundle.center.yaml",
            "bundle.leaf_a": FIXTURES / "bundle.leaf_a.yaml",
            "bundle.leaf_b": FIXTURES / "bundle.leaf_b.yaml",
        },
        formulas_path=FIXTURES / "formulas.yaml",
        max_depth=16,
    )


def test_cross_node_safety_not_vacuously_true() -> None:
    report = _report()
    safety = report.verdicts[0]
    assert safety.satisfied is False
    assert safety.violation_path
    assert safety.counterexample


def test_counterexample_shows_center_quarantined_leaf_not_downgraded() -> None:
    report = _report()
    final = report.verdicts[0].counterexample[-1]["nodes"]
    center_store = final["bundle.center"]["store"]
    leaf_store = final["bundle.leaf_a"]["store"]
    assert center_store.get("quarantined") is True
    assert leaf_store.get("downgraded") is not True


def test_liveness_formula_still_satisfied() -> None:
    report = _report()
    assert report.verdicts[1].satisfied is True


def test_overall_report_not_verified() -> None:
    assert _report().verified is False


def test_violation_path_reaches_failing_state() -> None:
    report = _report()
    assert report.verdicts[0].violation_path[-1] >= 0


def test_report_json_has_per_node_traces() -> None:
    payload = _report().to_dict()
    ce = payload["verdicts"][0]["counterexample"]
    assert ce[-1]["nodes"]["bundle.center"]["store"]["quarantined"] is True
