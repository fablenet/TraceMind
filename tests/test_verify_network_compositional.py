"""network_verify ``mode`` wiring (Stage 7-V.4).

Guards two things:
1. **Zero regression** — ``mode="monolithic"`` (the default) keeps the exact
   legacy report shape; no new keys leak into the serialized output.
2. **Compositional parity** — ``mode="compositional"`` dispatches to the
   assume-guarantee engine and returns per-formula verdicts identical to the
   monolithic product, while exposing the headline state reduction.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tm.artifacts.models import AgentBundleBody, AgentNetworkBody
from tm.verify.network import (
    load_agent_bundle_body,
    load_agent_network_body,
    load_formulas,
    network_verify,
    network_verify_from_paths,
    resolve_leaf_specs,
)

FIXTURES = Path("tests/fixtures/network_violation")

_LEGACY_KEYS = {
    "verified", "network_id", "component_ids", "formulas",
    "verdicts", "state_count", "edge_count", "deadlock_count",
}


def _network() -> AgentNetworkBody:
    return load_agent_network_body(FIXTURES / "agent_network.yaml")


def _bundles() -> dict[str, AgentBundleBody]:
    return {
        "bundle.center": load_agent_bundle_body(FIXTURES / "bundle.center.yaml"),
        "bundle.leaf_a": load_agent_bundle_body(FIXTURES / "bundle.leaf_a.yaml"),
        "bundle.leaf_b": load_agent_bundle_body(FIXTURES / "bundle.leaf_b.yaml"),
    }


def _formulas() -> list[str]:
    return load_formulas(FIXTURES / "formulas.yaml")


# ─── zero regression (default monolithic) ───────────────────────────


def test_default_mode_is_monolithic_and_report_shape_unchanged():
    report = network_verify(_network(), _bundles(), _formulas(), max_depth=16)
    assert report.mode == "monolithic"
    # serialized output must be byte-identical to Stage 6-4 — only legacy keys.
    assert set(report.to_dict().keys()) == _LEGACY_KEYS


def test_invalid_mode_rejected():
    with pytest.raises(ValueError, match="mode must be"):
        network_verify(_network(), _bundles(), _formulas(), mode="bogus")


# ─── compositional wiring ───────────────────────────────────────────


def test_compositional_matches_monolithic_verdicts():
    mono = network_verify(_network(), _bundles(), _formulas(), hash_mode="store")
    comp = network_verify(_network(), _bundles(), _formulas(), mode="compositional", hash_mode="store")
    assert comp.mode == "compositional"
    assert [v.satisfied for v in comp.verdicts] == [v.satisfied for v in mono.verdicts]
    assert comp.verified == mono.verified


def test_compositional_reports_state_reduction_and_extra_keys():
    comp = network_verify(_network(), _bundles(), _formulas(), mode="compositional", hash_mode="store")
    keys = set(comp.to_dict().keys())
    assert _LEGACY_KEYS <= keys
    assert {"mode", "abstraction_stats", "compositional_state_count",
            "monolithic_state_count", "fallbacks", "local_verdicts"} <= keys
    assert comp.compositional_state_count < comp.monolithic_state_count
    for stats in comp.abstraction_stats.values():
        assert stats["abstract_state_count"] <= stats["concrete_state_count"]


def test_resolve_leaf_specs_pulls_kpi_from_edges():
    specs = resolve_leaf_specs(_network(), _bundles())
    by_id = {s.component_id: s for s in specs}
    assert set(by_id) == {"bundle.leaf_a", "bundle.leaf_b"}
    for spec in specs:
        assert spec.kpi_keys  # every leaf has at least one interface KPI


def test_from_paths_threads_mode():
    comp = network_verify_from_paths(
        FIXTURES / "agent_network.yaml",
        bundle_paths={
            "bundle.center": FIXTURES / "bundle.center.yaml",
            "bundle.leaf_a": FIXTURES / "bundle.leaf_a.yaml",
            "bundle.leaf_b": FIXTURES / "bundle.leaf_b.yaml",
        },
        formulas_path=FIXTURES / "formulas.yaml",
        mode="compositional",
        hash_mode="store",
    )
    assert comp.mode == "compositional"
    mono = network_verify_from_paths(
        FIXTURES / "agent_network.yaml",
        bundle_paths={
            "bundle.center": FIXTURES / "bundle.center.yaml",
            "bundle.leaf_a": FIXTURES / "bundle.leaf_a.yaml",
            "bundle.leaf_b": FIXTURES / "bundle.leaf_b.yaml",
        },
        formulas_path=FIXTURES / "formulas.yaml",
        hash_mode="store",
    )
    assert [v.satisfied for v in comp.verdicts] == [v.satisfied for v in mono.verdicts]
