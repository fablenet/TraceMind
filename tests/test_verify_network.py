"""Tests for ``tm.verify.network`` — Phase 6 Stage 6-4.1."""

from __future__ import annotations

from pathlib import Path

import pytest

from tm.artifacts.models import AgentBundleBody, AgentNetworkBody
from tm.verify.bundle_adapter import adapter_from_bundle
from tm.verify.network import (
    load_agent_bundle_body,
    load_agent_network_body,
    load_formulas,
    network_verify,
    network_verify_from_paths,
    resolve_bundle_adapters,
)

FIXTURES = Path("tests/fixtures/network_violation")


def _load_fixture_bundle(name: str) -> AgentBundleBody:
    return load_agent_bundle_body(FIXTURES / name)


def _load_fixture_network() -> AgentNetworkBody:
    return load_agent_network_body(FIXTURES / "agent_network.yaml")


def _bundle_map() -> dict[str, AgentBundleBody]:
    return {
        "bundle.center": _load_fixture_bundle("bundle.center.yaml"),
        "bundle.leaf_a": _load_fixture_bundle("bundle.leaf_a.yaml"),
        "bundle.leaf_b": _load_fixture_bundle("bundle.leaf_b.yaml"),
    }


class TestBundleAdapter:
    def test_adapter_from_center_bundle(self) -> None:
        adapter = adapter_from_bundle(_load_fixture_bundle("bundle.center.yaml"))
        init = adapter.initial_state()
        assert "detect" in init.pending

    def test_missing_verify_meta_raises(self) -> None:
        body = AgentBundleBody.from_mapping(
            {
                "bundle_id": "bundle.bad",
                "agents": [],
                "plan": [],
                "meta": {},
            }
        )
        with pytest.raises(ValueError, match="meta.verify"):
            adapter_from_bundle(body)


class TestNetworkLoading:
    def test_load_agent_network(self) -> None:
        network = _load_fixture_network()
        assert network.network_id == "network.violation.demo"
        assert network.center_bundle_ref == "bundle.center"

    def test_load_formulas(self) -> None:
        formulas = load_formulas(FIXTURES / "formulas.yaml")
        assert len(formulas) == 2
        assert formulas[0].startswith("AG")

    def test_resolve_bundle_adapters_order(self) -> None:
        network = _load_fixture_network()
        adapters, ids = resolve_bundle_adapters(network, _bundle_map())
        assert ids == ["bundle.center", "bundle.leaf_a", "bundle.leaf_b"]
        assert len(adapters) == 3


class TestNetworkVerify:
    def test_safety_violation_detected(self) -> None:
        report = network_verify(
            _load_fixture_network(),
            _bundle_map(),
            ["AG !(peer(bundle.center, has(quarantined)) && !peer(bundle.leaf_a, has(downgraded)))"],
            max_depth=16,
        )
        assert report.verified is False
        assert report.verdicts[0].satisfied is False
        assert report.verdicts[0].violation_path
        assert report.verdicts[0].counterexample

    def test_liveness_formula_can_pass(self) -> None:
        report = network_verify(
            _load_fixture_network(),
            _bundle_map(),
            ["EF peer(bundle.center, has(quarantined))"],
            max_depth=16,
        )
        assert report.verdicts[0].satisfied is True

    def test_from_paths_helper(self) -> None:
        report = network_verify_from_paths(
            FIXTURES / "agent_network.yaml",
            bundle_paths={
                "bundle.center": FIXTURES / "bundle.center.yaml",
                "bundle.leaf_a": FIXTURES / "bundle.leaf_a.yaml",
                "bundle.leaf_b": FIXTURES / "bundle.leaf_b.yaml",
            },
            formulas_path=FIXTURES / "formulas.yaml",
            max_depth=16,
        )
        assert report.network_id == "network.violation.demo"
        assert len(report.verdicts) == 2

    def test_counterexample_names_nodes(self) -> None:
        report = network_verify(
            _load_fixture_network(),
            _bundle_map(),
            ["AG !(peer(bundle.center, has(quarantined)) && !peer(bundle.leaf_a, has(downgraded)))"],
        )
        ce = report.verdicts[0].counterexample
        assert ce
        final = ce[-1]["nodes"]
        assert "bundle.center" in final
        assert "bundle.leaf_a" in final

    def test_n3_completes_within_depth(self) -> None:
        report = network_verify(
            _load_fixture_network(),
            _bundle_map(),
            ["EF peer(bundle.center, has(quarantined))"],
            max_depth=16,
        )
        assert report.state_count > 0
        assert report.state_count <= 64

    def test_missing_bundle_ref_raises(self) -> None:
        with pytest.raises(KeyError):
            resolve_bundle_adapters(
                _load_fixture_network(), {"bundle.center": _load_fixture_bundle("bundle.center.yaml")}
            )

    def test_empty_formulas_rejected(self) -> None:
        with pytest.raises(ValueError, match="at least one formula"):
            network_verify(_load_fixture_network(), _bundle_map(), [])

    def test_report_to_dict(self) -> None:
        report = network_verify(
            _load_fixture_network(),
            _bundle_map(),
            ["EF peer(bundle.center, has(quarantined))"],
        )
        payload = report.to_dict()
        assert payload["network_id"] == "network.violation.demo"
        assert "verdicts" in payload

    def test_peer_syntax_equivalent_to_namespaced(self) -> None:
        peer_report = network_verify(
            _load_fixture_network(),
            _bundle_map(),
            ["EF peer(bundle.center, has(quarantined))"],
        )
        namespaced_report = network_verify(
            _load_fixture_network(),
            _bundle_map(),
            ["EF has(bundle.center.quarantined)"],
        )
        assert peer_report.verdicts[0].satisfied == namespaced_report.verdicts[0].satisfied

    def test_transport_independent_offline_verify(self) -> None:
        """Verify is offline — same inputs must yield identical verdicts twice."""
        kwargs = dict(
            network=_load_fixture_network(),
            bundles=_bundle_map(),
            formulas=["EF peer(bundle.center, has(quarantined))"],
            max_depth=16,
        )
        first = network_verify(**kwargs)
        second = network_verify(**kwargs)
        assert first.verified == second.verified
        assert first.state_count == second.state_count

    def test_failed_formulas_helper(self) -> None:
        report = network_verify_from_paths(
            FIXTURES / "agent_network.yaml",
            bundle_paths={
                "bundle.center": FIXTURES / "bundle.center.yaml",
                "bundle.leaf_a": FIXTURES / "bundle.leaf_a.yaml",
                "bundle.leaf_b": FIXTURES / "bundle.leaf_b.yaml",
            },
            formulas_path=FIXTURES / "formulas.yaml",
        )
        assert report.failed_formulas()

    def test_component_ids_use_bundle_refs(self) -> None:
        report = network_verify(
            _load_fixture_network(),
            _bundle_map(),
            ["EF peer(bundle.center, has(quarantined))"],
        )
        assert report.component_ids == ["bundle.center", "bundle.leaf_a", "bundle.leaf_b"]
