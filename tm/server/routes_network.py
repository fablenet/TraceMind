"""Network-aware controller routes — Phase 6 Stage 6-3.4.

Exposes star-topology AgentNetwork cycle orchestration over HTTP:

- ``POST /api/v1/network/cycle/run`` — center runs a full network cycle
- ``POST /api/v1/network/leaf/cycle/run`` — leaf runs one L1 cycle
- ``GET /api/v1/network/topology`` — diagnostics for the configured network
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Mapping

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from tm.artifacts.models import AgentNetworkBody
from tm.control.meta.controller import L1Runner
from tm.control.meta.network import (
    AgentNetworkTopology,
    NetworkController,
    dispatch_leaf_cycle_request,
    make_leaf_cycle_handler,
)
from tm.control.meta.proof import ProofReportGenerator
from tm.transport import InProcessTransport, Transport

L1RunnerFactory = Callable[[str], L1Runner]


@dataclass
class NetworkRuntime:
    """In-process network runtime wired to a shared transport."""

    topology: AgentNetworkTopology
    transport: Transport
    controller: NetworkController
    leaf_handlers: Mapping[str, Callable[[Mapping[str, Any]], Mapping[str, Any]]] = field(default_factory=dict)

    @classmethod
    def build(
        cls,
        body: AgentNetworkBody,
        *,
        center_l1_runner: L1Runner,
        leaf_runners: Mapping[str, L1Runner],
        my_peer_id: str | None = None,
        intent_ref: str = "intent.unspecified",
    ) -> "NetworkRuntime":
        topology = AgentNetworkTopology.from_agent_network(body)
        peer_ids = sorted(leaf_runners.keys())
        transport = InProcessTransport(known_peers=peer_ids)
        proof_gen = ProofReportGenerator(intent_id=intent_ref)

        leaf_handlers: Dict[str, Callable[[Mapping[str, Any]], Mapping[str, Any]]] = {}
        for peer_id, runner in leaf_runners.items():
            handler = make_leaf_cycle_handler(
                runner,
                proof_generator=proof_gen,
                peer_node_id=peer_id,
                intent_id=intent_ref,
            )
            transport.register_request_handler(peer_id, handler)
            leaf_handlers[peer_id] = handler

        controller = NetworkController(
            topology,
            transport,
            center_l1_runner,
            my_peer_id=my_peer_id or topology.center_peer_id,
            intent_ref=intent_ref,
            proof_generator=proof_gen,
        )
        return cls(
            topology=topology,
            transport=transport,
            controller=controller,
            leaf_handlers=leaf_handlers,
        )


class NetworkCycleRunRequest(BaseModel):
    cycle_number: int = Field(default=1, ge=1)


class LeafCycleRunRequest(BaseModel):
    peer_node_id: str
    cycle_number: int = Field(default=1, ge=1)


class NetworkCycleRunResponse(BaseModel):
    cycle_number: int
    peer_chain_valid: bool
    peer_chain_errors: list[str]
    center_proof: Dict[str, Any]
    leaf_proofs: Dict[str, Dict[str, Any]]
    aggregated_escalation: Dict[str, Any] | None


class LeafCycleRunResponse(BaseModel):
    peer_node_id: str
    cycle_id: str
    snapshot: Dict[str, Any]
    report: Dict[str, Any]
    proof_report: Dict[str, Any]
    escalation: Dict[str, Any] | None = None


class NetworkTopologyResponse(BaseModel):
    network_id: str
    center_peer_id: str
    leaf_peer_ids: list[str]
    transport_default: str


def _get_runtime(request: Request) -> NetworkRuntime:
    runtime = getattr(request.app.state, "network_runtime", None)
    if runtime is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="network runtime is not configured on this server",
        )
    return runtime


def create_network_router() -> APIRouter:
    router = APIRouter(prefix="/api/v1/network", tags=["network", "network.v1"])

    @router.get("/topology", response_model=NetworkTopologyResponse)
    def get_topology(request: Request) -> NetworkTopologyResponse:
        runtime = _get_runtime(request)
        topo = runtime.topology
        return NetworkTopologyResponse(
            network_id=topo.network_id,
            center_peer_id=topo.center_peer_id,
            leaf_peer_ids=sorted(topo.leaves.keys()),
            transport_default=topo.transport_default,
        )

    @router.post("/cycle/run", response_model=NetworkCycleRunResponse)
    def run_network_cycle(payload: NetworkCycleRunRequest, request: Request) -> NetworkCycleRunResponse:
        runtime = _get_runtime(request)
        result = runtime.controller.run_network_cycle(payload.cycle_number)
        center_proof = result.center_proof.to_dict() if result.center_proof is not None else {}
        leaf_proofs = {lr.peer_id: lr.proof_report.to_dict() for lr in result.leaf_results}
        agg = result.aggregated_escalation.to_dict() if result.aggregated_escalation is not None else None
        return NetworkCycleRunResponse(
            cycle_number=result.cycle_number,
            peer_chain_valid=result.peer_chain_valid,
            peer_chain_errors=list(result.peer_chain_errors),
            center_proof=center_proof,
            leaf_proofs=leaf_proofs,
            aggregated_escalation=agg,
        )

    @router.post("/leaf/cycle/run", response_model=LeafCycleRunResponse)
    def run_leaf_cycle(payload: LeafCycleRunRequest, request: Request) -> LeafCycleRunResponse:
        runtime = _get_runtime(request)
        handler = runtime.leaf_handlers.get(payload.peer_node_id)
        if handler is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"unknown leaf peer '{payload.peer_node_id}'",
            )
        reply = handler({"op": "controller.cycle.run", "cycle_number": payload.cycle_number})
        if reply.get("error"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(reply["error"]),
            )
        outputs = reply.get("outputs")
        if not isinstance(outputs, Mapping):
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="leaf handler returned reply without outputs",
            )
        proof_raw = outputs.get("proof_report")
        if not isinstance(proof_raw, Mapping):
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="leaf handler reply missing proof_report",
            )
        escalation_raw = outputs.get("escalation")
        escalation = dict(escalation_raw) if isinstance(escalation_raw, Mapping) else None
        return LeafCycleRunResponse(
            peer_node_id=payload.peer_node_id,
            cycle_id=str(outputs.get("cycle_id") or proof_raw.get("cycle_id", "")),
            snapshot=dict(outputs.get("snapshot") or {}),
            report=dict(outputs.get("report") or {}),
            proof_report=dict(proof_raw),
            escalation=escalation,
        )

    return router


__all__ = [
    "LeafCycleRunRequest",
    "LeafCycleRunResponse",
    "NetworkCycleRunRequest",
    "NetworkCycleRunResponse",
    "NetworkRuntime",
    "NetworkTopologyResponse",
    "create_network_router",
    "dispatch_leaf_cycle_request",
]
