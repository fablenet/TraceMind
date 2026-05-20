"""Three-node network e2e smoke — Phase 6 Stage 6-3.5.

Uses in-process transport with three logical peers (1 center + 2 leaves) to
exercise the full network cycle, peer proof chain verification, tamper
detection, and cross-node escalation aggregation without subprocess servers.
"""

from __future__ import annotations

from tm.artifacts.models import AgentNetworkBody
from tm.control.meta.controller import L1CycleResult
from tm.control.meta.escalation import Severity
from tm.control.meta.proof import ProofReportGenerator, verify_peer_chain
from tm.server.routes_network import NetworkRuntime


def _network_body() -> AgentNetworkBody:
    return AgentNetworkBody.from_mapping(
        {
            "network_id": "network.e2e.three_node",
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


def _runner(node_id: str):
    def run(cycle_number: int) -> L1CycleResult:
        return L1CycleResult(
            cycle_id=f"{node_id}-cycle-{cycle_number}",
            snapshot={
                "environment": {
                    "metrics": {
                        "kpi.alpha": float(cycle_number),
                        "kpi.beta": float(cycle_number) * 2,
                    }
                }
            },
            report={
                "report_id": f"rep-{node_id}-{cycle_number}",
                "status": "ok",
                "artifact_refs": {"bundle": node_id},
            },
        )

    return run


def _build_runtime() -> NetworkRuntime:
    return NetworkRuntime.build(
        _network_body(),
        center_l1_runner=_runner("bundle.center"),
        leaf_runners={
            "bundle.leaf_a": _runner("bundle.leaf_a"),
            "bundle.leaf_b": _runner("bundle.leaf_b"),
        },
        intent_ref="intent.e2e",
    )


def test_three_node_full_network_cycle() -> None:
    runtime = _build_runtime()
    result = runtime.controller.run_network_cycle(1)

    assert len(result.leaf_results) == 2
    assert result.center_result is not None
    assert result.center_proof is not None
    assert result.peer_chain_valid
    assert result.aggregated_escalation is None

    center = result.center_proof
    leaf_map = {lr.peer_id: lr.proof_report for lr in result.leaf_results}
    ok, errors = verify_peer_chain(center, leaf_map)
    assert ok, errors

    peer_entries = [e for e in center.evidence_chain if e.event_type == "peer_proof_report"]
    assert len(peer_entries) == 2
    for entry in peer_entries:
        assert entry.peer_chain_ref
        assert entry.peer_node_id in leaf_map


def test_tampered_leaf_triggers_escalation() -> None:
    runtime = _build_runtime()
    result = runtime.controller.run_network_cycle(1)
    assert result.center_proof is not None

    leaf_map = {lr.peer_id: lr.proof_report for lr in result.leaf_results}
    leaf_map["bundle.leaf_a"].report_hash = "tampered-hash"

    ok, errors = verify_peer_chain(result.center_proof, leaf_map)
    assert not ok

    agg = runtime.controller._network_escalator.aggregate(  # noqa: SLF001
        None,
        {},
        peer_chain_valid=False,
        peer_chain_errors=errors,
    )
    assert agg is not None
    assert agg.severity == Severity.CRITICAL
    assert not agg.peer_chain_valid


def test_http_runtime_matches_controller_cycle() -> None:
    runtime = _build_runtime()
    via_controller = runtime.controller.run_network_cycle(2)
    center_proof = via_controller.center_proof
    assert center_proof is not None
    assert center_proof.cycle_id == "bundle.center-cycle-2"
    assert all(lr.proof_report.report_hash for lr in via_controller.leaf_results)


def test_proof_generator_shared_across_nodes() -> None:
    runtime = _build_runtime()
    assert isinstance(runtime.controller._proof_gen, ProofReportGenerator)  # noqa: SLF001
    result = runtime.controller.run_network_cycle(1)
    for lr in result.leaf_results:
        assert lr.proof_report.intent_id == "intent.e2e"


def test_network_runtime_exposes_leaf_handlers() -> None:
    runtime = _build_runtime()
    assert set(runtime.leaf_handlers.keys()) == {"bundle.leaf_a", "bundle.leaf_b"}
    reply = runtime.leaf_handlers["bundle.leaf_a"]({"op": "controller.cycle.run", "cycle_number": 1})
    assert "outputs" in reply


def test_multiple_cycles_independent_hashes() -> None:
    runtime = _build_runtime()
    first = runtime.controller.run_network_cycle(1)
    second = runtime.controller.run_network_cycle(2)
    assert first.center_proof is not None
    assert second.center_proof is not None
    assert first.center_proof.report_hash != second.center_proof.report_hash
