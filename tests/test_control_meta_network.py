"""Tests for ``tm.control.meta.network`` — Phase 6 Stage 6-3.1."""

from __future__ import annotations


import pytest

from tm.artifacts.models import AgentNetworkBody
from tm.control.meta.controller import L1CycleResult
from tm.control.meta.escalation import EscalationReport, Severity
from tm.control.meta.network import (
    AgentNetworkTopology,
    CYCLE_RUN_OP,
    NetworkController,
    dispatch_leaf_cycle_request,
    make_leaf_cycle_handler,
)
from tm.control.meta.proof import ProofReportGenerator
from tm.transport import InProcessTransport, TransportNetworkError


def _two_leaf_network() -> AgentNetworkBody:
    return AgentNetworkBody.from_mapping(
        {
            "network_id": "network.demo.two_leaf",
            "topology": "star",
            "center_bundle_ref": "bundle.center",
            "leaf_bundle_refs": ["bundle.leaf_a", "bundle.leaf_b"],
            "edges": [
                {"from": "bundle.leaf_a", "to": "bundle.center", "kpi_keys": ["kpi.alpha"]},
                {"from": "bundle.leaf_b", "to": "bundle.center", "kpi_keys": ["kpi.beta"]},
            ],
            "transport_default": "inprocess",
        }
    )


def _mock_runner(prefix: str):
    def runner(cycle_number: int) -> L1CycleResult:
        return L1CycleResult(
            cycle_id=f"{prefix}-cycle-{cycle_number}",
            snapshot={"environment": {"metrics": {f"{prefix}.kpi": float(cycle_number)}}},
            report={"report_id": f"rep-{prefix}-{cycle_number}", "status": "ok"},
        )

    return runner


def _build_controller(
    body: AgentNetworkBody | None = None,
) -> tuple[NetworkController, InProcessTransport]:
    network = body or _two_leaf_network()
    topology = AgentNetworkTopology.from_agent_network(network)
    transport = InProcessTransport(known_peers=sorted(topology.leaves.keys()))
    proof_gen = ProofReportGenerator(intent_id="test.network")

    for peer_id in topology.leaves:
        transport.register_request_handler(
            peer_id,
            make_leaf_cycle_handler(
                _mock_runner(peer_id),
                proof_generator=proof_gen,
                peer_node_id=peer_id,
                intent_id="test.network",
            ),
        )

    controller = NetworkController(
        topology,
        transport,
        _mock_runner("bundle.center"),
        my_peer_id="bundle.center",
        intent_ref="test.network",
        proof_generator=proof_gen,
    )
    return controller, transport


class TestAgentNetworkTopology:
    def test_from_agent_network_star(self) -> None:
        topo = AgentNetworkTopology.from_agent_network(_two_leaf_network())
        assert topo.network_id == "network.demo.two_leaf"
        assert topo.center_peer_id == "bundle.center"
        assert set(topo.leaves.keys()) == {"bundle.leaf_a", "bundle.leaf_b"}
        assert topo.leaves["bundle.leaf_a"].kpi_keys == ("kpi.alpha",)

    def test_rejects_non_star_topology(self) -> None:
        payload = {
            "network_id": "network.tree",
            "topology": "tree",
            "center_bundle_ref": "bundle.center",
            "leaf_bundle_refs": ["bundle.leaf_a"],
            "edges": [{"from": "bundle.leaf_a", "to": "bundle.center", "kpi_keys": ["kpi.a"]}],
            "transport_default": "http",
        }
        with pytest.raises(ValueError, match="topology=star"):
            AgentNetworkTopology.from_agent_network(AgentNetworkBody.from_mapping(payload))

    def test_missing_edge_raises(self) -> None:
        body = AgentNetworkBody.from_mapping(
            {
                "network_id": "network.bad",
                "topology": "star",
                "center_bundle_ref": "bundle.center",
                "leaf_bundle_refs": ["bundle.leaf_a"],
                "edges": [
                    {"from": "bundle.leaf_x", "to": "bundle.center", "kpi_keys": ["kpi.alpha"]},
                ],
                "transport_default": "http",
            }
        )
        with pytest.raises(ValueError, match="no leaf-to-center edge"):
            AgentNetworkTopology.from_agent_network(body)


class TestLeafDispatch:
    def test_dispatch_leaf_cycle_request(self) -> None:
        proof_gen = ProofReportGenerator(intent_id="leaf.test")
        reply = dispatch_leaf_cycle_request(
            {"op": CYCLE_RUN_OP, "cycle_number": 2},
            l1_runner=_mock_runner("leaf"),
            proof_generator=proof_gen,
            peer_node_id="bundle.leaf_a",
        )
        outputs = reply["outputs"]
        assert outputs["cycle_id"] == "leaf-cycle-2"
        assert outputs["proof_report"]["peer_node_id"] == "bundle.leaf_a"

    def test_unknown_op_returns_error(self) -> None:
        reply = dispatch_leaf_cycle_request(
            {"op": "unknown"},
            l1_runner=_mock_runner("leaf"),
            proof_generator=ProofReportGenerator(intent_id="x"),
            peer_node_id="leaf",
        )
        assert "error" in reply

    def test_make_leaf_cycle_handler(self) -> None:
        handler = make_leaf_cycle_handler(_mock_runner("leaf"), peer_node_id="leaf")
        reply = handler({"op": CYCLE_RUN_OP, "cycle_number": 1})
        assert reply["outputs"]["proof_report"]["cycle_id"] == "leaf-cycle-1"


class TestNetworkController:
    def test_run_leaf_cycle(self) -> None:
        controller, _ = _build_controller()
        result = controller.run_leaf_cycle("bundle.leaf_a", 1)
        assert result.peer_id == "bundle.leaf_a"
        assert result.proof_report.peer_node_id == "bundle.leaf_a"
        assert result.proof_report.report_hash

    def test_run_leaf_cycles_sorted(self) -> None:
        controller, _ = _build_controller()
        results = controller.run_leaf_cycles(1)
        assert [r.peer_id for r in results] == ["bundle.leaf_a", "bundle.leaf_b"]

    def test_run_center_cycle(self) -> None:
        controller, _ = _build_controller()
        center = controller.run_center_cycle(3)
        assert center.cycle_id == "bundle.center-cycle-3"

    def test_build_center_proof_attaches_peers(self) -> None:
        controller, _ = _build_controller()
        leaf_results = controller.run_leaf_cycles(1)
        center_result = controller.run_center_cycle(1)
        proof = controller.build_center_proof(center_result, leaf_results)
        peer_entries = [e for e in proof.evidence_chain if e.event_type == "peer_proof_report"]
        assert len(peer_entries) == 2
        assert proof.report_hash

    def test_run_network_cycle_coherent_chain(self) -> None:
        controller, _ = _build_controller()
        result = controller.run_network_cycle(1)
        assert result.peer_chain_valid
        assert result.center_proof is not None
        assert len(result.leaf_results) == 2
        assert result.aggregated_escalation is None

    def test_unknown_leaf_raises(self) -> None:
        controller, _ = _build_controller()
        with pytest.raises(ValueError, match="unknown leaf"):
            controller.run_leaf_cycle("bundle.missing", 1)

    def test_missing_handler_raises_transport_error(self) -> None:
        network = _two_leaf_network()
        topology = AgentNetworkTopology.from_agent_network(network)
        transport = InProcessTransport(known_peers=["bundle.leaf_a"])
        controller = NetworkController(
            topology,
            transport,
            _mock_runner("bundle.center"),
            my_peer_id="bundle.center",
        )
        with pytest.raises(TransportNetworkError):
            controller.run_leaf_cycle("bundle.leaf_a", 1)

    def test_tampered_leaf_invalidates_peer_chain(self) -> None:
        controller, _ = _build_controller()
        result = controller.run_network_cycle(1)
        center_proof = result.center_proof
        assert center_proof is not None
        leaf_map = {lr.peer_id: lr.proof_report for lr in result.leaf_results}
        leaf_map["bundle.leaf_a"].report_hash = "tampered"
        from tm.control.meta.proof import verify_peer_chain

        ok, errs = verify_peer_chain(center_proof, leaf_map)
        assert not ok
        assert any("mismatch" in e for e in errs)

    def test_peer_chain_failure_produces_escalation(self) -> None:
        controller, _ = _build_controller()
        result = controller.run_network_cycle(1)
        result.leaf_results[0].proof_report.report_hash = "bad-hash"
        valid, errors = __import__("tm.control.meta.proof", fromlist=["verify_peer_chain"]).verify_peer_chain(
            result.center_proof,
            {lr.peer_id: lr.proof_report for lr in result.leaf_results},
        )
        assert not valid
        agg = controller._network_escalator.aggregate(  # noqa: SLF001
            None,
            {},
            peer_chain_valid=valid,
            peer_chain_errors=errors,
        )
        assert agg is not None
        assert agg.severity == Severity.CRITICAL
        assert not agg.peer_chain_valid

    def test_parse_leaf_reply_with_escalation(self) -> None:
        from tm.control.meta.network import _parse_leaf_reply

        proof_gen = ProofReportGenerator(intent_id="x")
        proof = proof_gen.generate(
            cycle_result=type("R", (), {"bundle_artifact_id": "c1", "start_time": "", "end_time": ""})(),
            pre_snapshot={},
        )
        esc = EscalationReport(
            report_id="esc-1",
            timestamp="2026-01-01T00:00:00Z",
            severity=Severity.WARNING,
            intent_ref="intent.test",
            verdicts=(),
            kpi_history=(),
            recent_rules_fired=(),
            recent_errors=(),
            gap_summary="stalled",
            suggested_actions=(),
            counterexample=None,
            peer_node_id="bundle.leaf_a",
        )
        reply = {
            "outputs": {
                "cycle_id": "c1",
                "snapshot": {},
                "report": {},
                "proof_report": proof.to_dict(),
                "escalation": esc.to_dict(),
            }
        }
        parsed = _parse_leaf_reply("bundle.leaf_a", reply)
        assert parsed.escalation is not None
        assert parsed.escalation.peer_node_id == "bundle.leaf_a"
