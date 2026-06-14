"""Partial-order reduction (POR) measurement — Stage 7-V.5.

The **store-hash lever**: exploring with ``hash_mode="store"`` hashes only the
store, so joint states that differ *only* in the order pending/done/events were
produced — i.e. scheduling-equivalent interleavings — collapse to one. That is a
cheap partial-order reduction already present in the explorer
(see ``docs/verify/SEMANTICS.md``). This module makes the lever **explicit and
measurable**: it explores the same product under ``full`` and ``store`` hashing
and quantifies how many states POR eliminates, per the 7-V acceptance criteria.

Soundness caveat (declared, not assumed): store-hash is sound only for
properties over **store facts** — ``has(...)``. Formulas that reference
``pending(...)`` / ``done(...)`` / ``Terminal`` depend on the very scheduling
state that store-hash collapses and MUST use ``hash_mode="full"``. POR is a
measurement/optimization here; it never silently changes a verdict.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

from tm.verify.explorer import Explorer
from tm.verify.joint import ComponentAdapter, JointAdapter


@dataclass(frozen=True)
class PORMeasurement:
    """How much the store-hash POR lever shrinks a product's reachable graph."""

    full_state_count: int
    store_state_count: int
    full_edge_count: int
    store_edge_count: int
    max_depth: int

    @property
    def states_eliminated(self) -> int:
        return self.full_state_count - self.store_state_count

    @property
    def state_reduction_ratio(self) -> float:
        """Fraction of states POR removed (0.0 when nothing merged)."""
        if self.full_state_count == 0:
            return 0.0
        return self.states_eliminated / self.full_state_count

    def to_dict(self) -> Dict[str, Any]:
        return {
            "full_state_count": self.full_state_count,
            "store_state_count": self.store_state_count,
            "full_edge_count": self.full_edge_count,
            "store_edge_count": self.store_edge_count,
            "states_eliminated": self.states_eliminated,
            "state_reduction_ratio": self.state_reduction_ratio,
            "max_depth": self.max_depth,
        }


def _edge_count(edges: Dict[int, List[int]]) -> int:
    return sum(len(succs) for succs in edges.values())


def measure_por(
    components: Sequence[ComponentAdapter],
    *,
    component_ids: Optional[Sequence[str]] = None,
    max_depth: int = 16,
) -> PORMeasurement:
    """Explore the joint product under ``full`` vs ``store`` hashing and compare.

    Deterministic and verdict-free — this only *counts* states/edges; it never
    evaluates a formula, so it cannot change any verification result.
    """
    adapter = JointAdapter.from_components(components, component_ids=component_ids)
    full = Explorer(adapter).run(max_depth=max_depth, hash_mode="full")
    store = Explorer(adapter).run(max_depth=max_depth, hash_mode="store")
    return PORMeasurement(
        full_state_count=len(full.states),
        store_state_count=len(store.states),
        full_edge_count=_edge_count(full.edges),
        store_edge_count=_edge_count(store.edges),
        max_depth=max_depth,
    )


def measure_network_por(network, bundles, *, max_depth: int = 16) -> PORMeasurement:
    """POR measurement over an AgentNetwork's monolithic product."""
    from tm.verify.network import resolve_bundle_adapters

    components, ids = resolve_bundle_adapters(network, bundles)
    return measure_por(components, component_ids=ids, max_depth=max_depth)


__all__ = [
    "PORMeasurement",
    "measure_network_por",
    "measure_por",
]
