"""AgentNetwork-aware control — Phase 6 Stage 6-3.1.

Parses an :class:`AgentNetworkBody` into a star-topology routing table and
provides :class:`NetworkController`, which wraps :class:`MetaController`'s
L1 cycle semantics but dispatches leaf MAPE-K cycles over a
:class:`tm.transport.Transport` instead of in-process function calls.

Wire op for leaf/center cycle dispatch: ``controller.cycle.run`` (carried in
the transport RPC envelope ``body.op`` field).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

from tm.artifacts.models import AgentNetworkBody, AgentNetworkEdge
from tm.control.meta.controller import L1CycleResult, L1Runner
from tm.control.meta.convergence import ConvergenceDetector
from tm.control.meta.escalation import (
    UNSPECIFIED_INTENT_REF,
    CrossNodeEscalationReport,
    EscalationReport,
    NetworkEscalator,
)
from tm.control.meta.proof import (
    ProofReport,
    ProofReportGenerator,
    attach_peer_proofs,
    verify_peer_chain,
)
from tm.transport import Transport, TransportError, TransportNetworkError

log = logging.getLogger(__name__)

CYCLE_RUN_OP = "controller.cycle.run"


@dataclass(frozen=True)
class LeafEdgeConfig:
    """Per-leaf edge contract extracted from AgentNetwork."""

    bundle_ref: str
    kpi_keys: tuple[str, ...]


@dataclass(frozen=True)
class AgentNetworkTopology:
    """Star topology view derived from an AgentNetwork artifact body."""

    network_id: str
    center_peer_id: str
    center_bundle_ref: str
    transport_default: str
    leaves: Mapping[str, LeafEdgeConfig]

    @classmethod
    def from_agent_network(cls, body: AgentNetworkBody) -> "AgentNetworkTopology":
        if body.topology != "star":
            raise ValueError(f"AgentNetworkTopology only supports topology=star in v0.3, got '{body.topology}'")
        center = body.center_bundle_ref
        leaves: dict[str, LeafEdgeConfig] = {}
        for leaf_ref in body.leaf_bundle_refs:
            edge = _edge_for_leaf(body.edges, leaf_ref, center)
            leaves[leaf_ref] = LeafEdgeConfig(
                bundle_ref=leaf_ref,
                kpi_keys=tuple(edge.kpi_keys),
            )
        return cls(
            network_id=body.network_id,
            center_peer_id=center,
            center_bundle_ref=center,
            transport_default=body.transport_default,
            leaves=leaves,
        )


def _edge_for_leaf(
    edges: Sequence[AgentNetworkEdge],
    leaf_ref: str,
    center_ref: str,
) -> AgentNetworkEdge:
    for edge in edges:
        if edge.source == leaf_ref and edge.target == center_ref:
            return edge
    raise ValueError(f"no leaf-to-center edge for '{leaf_ref}' in AgentNetwork")


@dataclass
class LeafCycleResult:
    peer_id: str
    cycle_id: str
    snapshot: dict[str, Any]
    report: dict[str, Any]
    proof_report: ProofReport
    escalation: EscalationReport | None = None


@dataclass
class NetworkCycleResult:
    cycle_number: int
    leaf_results: list[LeafCycleResult] = field(default_factory=list)
    center_result: L1CycleResult | None = None
    center_proof: ProofReport | None = None
    peer_chain_valid: bool = True
    peer_chain_errors: list[str] = field(default_factory=list)
    aggregated_escalation: CrossNodeEscalationReport | None = None


LeafCycleHandler = Callable[[Mapping[str, Any]], Mapping[str, Any]]


def dispatch_leaf_cycle_request(
    message: Mapping[str, Any],
    *,
    l1_runner: L1Runner,
    proof_generator: ProofReportGenerator,
    peer_node_id: str,
) -> Mapping[str, Any]:
    """Server-side handler: run one L1 cycle and return proof payload."""
    if message.get("op") != CYCLE_RUN_OP:
        return {"error": f"unknown op '{message.get('op')}'"}

    cycle_number = int(message.get("cycle_number") or 1)
    l1_result = l1_runner(cycle_number)

    proof = proof_generator.generate(
        cycle_result=_L1ResultAdapter(l1_result),
        pre_snapshot=l1_result.snapshot,
    )
    proof_dict = proof.to_dict()
    proof_dict["peer_node_id"] = peer_node_id

    return {
        "outputs": {
            "cycle_id": l1_result.cycle_id,
            "snapshot": dict(l1_result.snapshot),
            "report": dict(l1_result.report),
            "proof_report": proof_dict,
        }
    }


def make_leaf_cycle_handler(
    l1_runner: L1Runner,
    *,
    proof_generator: ProofReportGenerator | None = None,
    peer_node_id: str,
    intent_id: str = UNSPECIFIED_INTENT_REF,
) -> LeafCycleHandler:
    """Build a transport request handler for a leaf node."""
    gen = proof_generator or ProofReportGenerator(intent_id=intent_id)

    def handler(message: Mapping[str, Any]) -> Mapping[str, Any]:
        return dispatch_leaf_cycle_request(
            message,
            l1_runner=l1_runner,
            proof_generator=gen,
            peer_node_id=peer_node_id,
        )

    return handler


class _L1ResultAdapter:
    """Minimal adapter so ProofReportGenerator can consume L1CycleResult."""

    def __init__(self, result: L1CycleResult) -> None:
        self.bundle_artifact_id = result.cycle_id
        self.env_snapshot = _SnapshotWrapper(result.snapshot)
        self.execution_report = _ReportWrapper(result.report)
        self.policy_decisions: list = []
        self.start_time = ""
        self.end_time = ""


class _SnapshotWrapper:
    def __init__(self, snapshot: Mapping[str, Any]) -> None:
        self.body = snapshot


class _ReportWrapper:
    def __init__(self, report: Mapping[str, Any]) -> None:
        self.body = _ReportBody(report)


class _ReportBody:
    def __init__(self, report: Mapping[str, Any]) -> None:
        self.report_id = str(report.get("report_id", ""))
        self.status = str(report.get("status", report.get("act_status", "ok")))
        self.artifact_refs = dict(report.get("artifact_refs", {}))
        self.errors = list(report.get("errors", []))


class NetworkController:
    """Star-topology controller: center + transport-routed leaves."""

    def __init__(
        self,
        topology: AgentNetworkTopology,
        transport: Transport,
        center_l1_runner: L1Runner,
        *,
        my_peer_id: str,
        intent_ref: str = UNSPECIFIED_INTENT_REF,
        proof_generator: ProofReportGenerator | None = None,
        detector: ConvergenceDetector | None = None,
        timeout_s: float | None = 30.0,
    ) -> None:
        self._topology = topology
        self._transport = transport
        self._center_runner = center_l1_runner
        self._my_peer_id = my_peer_id
        self._intent_ref = intent_ref
        self._proof_gen = proof_generator or ProofReportGenerator(intent_id=intent_ref)
        self._detector = detector
        self._timeout_s = timeout_s
        self._network_escalator = NetworkEscalator(network_id=topology.network_id)

    @property
    def topology(self) -> AgentNetworkTopology:
        return self._topology

    def run_leaf_cycle(self, peer_id: str, cycle_number: int) -> LeafCycleResult:
        leaf = self._topology.leaves.get(peer_id)
        if leaf is None:
            raise ValueError(f"unknown leaf peer '{peer_id}'")

        envelope = {
            "op": CYCLE_RUN_OP,
            "cycle_number": cycle_number,
            "bundle_ref": leaf.bundle_ref,
            "kpi_keys": list(leaf.kpi_keys),
            "network_id": self._topology.network_id,
        }
        idempotency_key = f"{self._topology.network_id}:{peer_id}:cycle:{cycle_number}"

        try:
            reply = self._transport.request(
                peer_id,
                envelope,
                timeout_s=self._timeout_s,
                idempotency_key=idempotency_key,
            )
        except TransportError as exc:
            raise TransportNetworkError(
                f"leaf cycle failed for '{peer_id}': {exc}",
                peer_id=peer_id,
            ) from exc

        return _parse_leaf_reply(peer_id, reply)

    def run_leaf_cycles(self, cycle_number: int) -> list[LeafCycleResult]:
        results: list[LeafCycleResult] = []
        for peer_id in sorted(self._topology.leaves.keys()):
            results.append(self.run_leaf_cycle(peer_id, cycle_number))
        return results

    def run_center_cycle(self, cycle_number: int) -> L1CycleResult:
        return self._center_runner(cycle_number)

    def build_center_proof(
        self,
        center_result: L1CycleResult,
        leaf_results: Sequence[LeafCycleResult],
    ) -> ProofReport:
        center_proof = self._proof_gen.generate(
            cycle_result=_L1ResultAdapter(center_result),
            pre_snapshot=center_result.snapshot,
        )
        center_proof.peer_node_id = self._topology.center_peer_id
        peer_pairs = [(lr.peer_id, lr.proof_report) for lr in leaf_results]
        return attach_peer_proofs(center_proof, peer_pairs)

    def run_network_cycle(self, cycle_number: int) -> NetworkCycleResult:
        leaf_results = self.run_leaf_cycles(cycle_number)
        center_result = self.run_center_cycle(cycle_number)
        center_proof = self.build_center_proof(center_result, leaf_results)

        valid, errors = verify_peer_chain(center_proof, {lr.peer_id: lr.proof_report for lr in leaf_results})
        if not valid:
            log.warning("Peer chain verification failed: %s", errors)

        peer_escalations = {lr.peer_id: lr.escalation for lr in leaf_results if lr.escalation is not None}
        center_escalation = None
        if self._detector is not None:
            # Optional: run meta convergence on center KPIs only in this cycle.
            pass

        aggregated = self._network_escalator.aggregate(
            center_escalation,
            peer_escalations,
            peer_chain_valid=valid,
            peer_chain_errors=errors,
        )

        return NetworkCycleResult(
            cycle_number=cycle_number,
            leaf_results=list(leaf_results),
            center_result=center_result,
            center_proof=center_proof,
            peer_chain_valid=valid,
            peer_chain_errors=list(errors),
            aggregated_escalation=aggregated,
        )


def _parse_leaf_reply(peer_id: str, reply: Mapping[str, Any]) -> LeafCycleResult:
    if reply.get("error"):
        raise TransportNetworkError(str(reply["error"]), peer_id=peer_id)

    outputs = reply.get("outputs")
    if not isinstance(outputs, Mapping):
        raise TransportNetworkError(
            f"leaf '{peer_id}' returned reply without outputs mapping",
            peer_id=peer_id,
        )

    proof_raw = outputs.get("proof_report")
    if not isinstance(proof_raw, Mapping):
        raise TransportNetworkError(
            f"leaf '{peer_id}' reply missing proof_report",
            peer_id=peer_id,
        )

    proof = ProofReport.from_dict(proof_raw)
    if not proof.report_hash:
        proof.report_hash = proof._compute_hash()

    escalation_raw = outputs.get("escalation")
    escalation = None
    if isinstance(escalation_raw, Mapping):
        escalation = EscalationReport.from_dict(escalation_raw)

    return LeafCycleResult(
        peer_id=peer_id,
        cycle_id=str(outputs.get("cycle_id") or proof.cycle_id),
        snapshot=dict(outputs.get("snapshot") or {}),
        report=dict(outputs.get("report") or {}),
        proof_report=proof,
        escalation=escalation,
    )


__all__ = [
    "AgentNetworkTopology",
    "CYCLE_RUN_OP",
    "LeafCycleResult",
    "LeafEdgeConfig",
    "NetworkController",
    "NetworkCycleResult",
    "dispatch_leaf_cycle_request",
    "make_leaf_cycle_handler",
]
