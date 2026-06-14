"""Joint Kripke verification over AgentNetwork topologies — Phase 6 Stage 6-4.

Loads an :class:`AgentNetworkBody` plus referenced bundle artifacts, composes
N offline component adapters, and runs :func:`joint_verify` with optional
``peer()`` CTL sugar and cross-node counterexample projection.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

try:
    import yaml  # type: ignore[import-untyped]
except ModuleNotFoundError:
    yaml = None

from tm.artifacts.models import AgentBundleBody, AgentNetworkBody
from tm.verify.bundle_adapter import adapter_from_bundle
from tm.verify.compositional import LeafSpec, assume_guarantee_verify
from tm.verify.joint import (
    JointVerdict,
    joint_verify,
)
from tm.verify.spec import _load_structured


@dataclass
class NetworkVerifyReport:
    """JSON-serializable outcome of network-level joint verification."""

    verified: bool
    network_id: str
    component_ids: List[str]
    formulas: List[str]
    verdicts: List[JointVerdict]
    state_count: int
    edge_count: int
    deadlock_count: int
    # 7-V.4 — additive compositional fields. Empty/None in monolithic mode and
    # NOT serialized there, so default-monolithic output is byte-identical.
    mode: str = "monolithic"
    local_verdicts: List[Dict[str, Any]] = field(default_factory=list)
    abstraction_stats: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    compositional_state_count: Optional[int] = None
    monolithic_state_count: Optional[int] = None
    fallbacks: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "verified": self.verified,
            "network_id": self.network_id,
            "component_ids": list(self.component_ids),
            "formulas": list(self.formulas),
            "verdicts": [
                {
                    "formula": v.formula,
                    "satisfied": v.satisfied,
                    "violation_path": list(v.violation_path),
                    "counterexample": list(v.counterexample),
                }
                for v in self.verdicts
            ],
            "state_count": self.state_count,
            "edge_count": self.edge_count,
            "deadlock_count": self.deadlock_count,
        }
        if self.mode != "monolithic":
            out["mode"] = self.mode
            out["local_verdicts"] = list(self.local_verdicts)
            out["abstraction_stats"] = dict(self.abstraction_stats)
            out["compositional_state_count"] = self.compositional_state_count
            out["monolithic_state_count"] = self.monolithic_state_count
            out["fallbacks"] = list(self.fallbacks)
        return out

    def failed_formulas(self) -> List[str]:
        return [v.formula for v in self.verdicts if not v.satisfied]


def load_agent_network_body(path: Path) -> AgentNetworkBody:
    data = _load_structured(path)
    if "body" in data and isinstance(data["body"], Mapping):
        data = data["body"]
    return AgentNetworkBody.from_mapping(data)


def load_agent_bundle_body(path: Path) -> AgentBundleBody:
    data = _load_structured(path)
    if "body" in data and isinstance(data["body"], Mapping):
        data = data["body"]
    return AgentBundleBody.from_mapping(data)


def load_formulas(path: Path | None) -> List[str]:
    if path is None:
        return []
    data = _load_structured(path)
    if isinstance(data, Mapping):
        raw = data.get("formulas") or data.get("properties") or []
        if isinstance(raw, Sequence) and not isinstance(raw, str):
            out: List[str] = []
            for entry in raw:
                if isinstance(entry, str):
                    out.append(entry)
                elif isinstance(entry, Mapping) and entry.get("formula"):
                    out.append(str(entry["formula"]))
            return out
    if isinstance(data, Sequence) and not isinstance(data, str):
        return [str(x) for x in data]
    raise ValueError(f"unsupported formulas document at {path}")


def resolve_bundle_adapters(
    network: AgentNetworkBody,
    bundles: Mapping[str, AgentBundleBody],
) -> tuple[List[Any], List[str]]:
    """Return adapters and component ids in center-first, leaves-sorted order."""
    required = [network.center_bundle_ref, *network.leaf_bundle_refs]
    adapters = []
    ids: List[str] = []
    for bundle_ref in required:
        body = bundles.get(bundle_ref)
        if body is None:
            raise KeyError(f"missing bundle artifact for ref '{bundle_ref}'")
        adapters.append(adapter_from_bundle(body))
        ids.append(bundle_ref)
    return adapters, ids


def resolve_leaf_specs(
    network: AgentNetworkBody,
    bundles: Mapping[str, AgentBundleBody],
) -> List[LeafSpec]:
    """Map each leaf to a :class:`LeafSpec` for assume-guarantee verification.

    Interface fact set = union of ``kpi_keys`` on every edge touching the leaf
    (those are the typed, edge-visible KPIs — the leaf's interface). Optional
    A-G contracts (``guarantees`` / ``assumptions`` as CTL strings) may be
    supplied under ``network.metadata['contracts'][<leaf_ref>]``.
    """
    meta = network.metadata if isinstance(network.metadata, Mapping) else {}
    contracts = meta.get("contracts") if isinstance(meta.get("contracts"), Mapping) else {}

    specs: List[LeafSpec] = []
    for ref in network.leaf_bundle_refs:
        body = bundles.get(ref)
        if body is None:
            raise KeyError(f"missing bundle artifact for ref '{ref}'")
        kpis: List[str] = []
        for edge in network.edges:
            if ref in (edge.source, edge.target):
                for k in edge.kpi_keys:
                    if k not in kpis:
                        kpis.append(k)
        contract = contracts.get(ref) if isinstance(contracts.get(ref), Mapping) else {}
        specs.append(
            LeafSpec.of(
                ref,
                body,
                kpis,
                guarantees=tuple(contract.get("guarantees") or ()),
                assumptions=tuple(contract.get("assumptions") or ()),
            )
        )
    return specs


def network_verify(
    network: AgentNetworkBody,
    bundles: Mapping[str, AgentBundleBody],
    formulas: Sequence[str],
    *,
    mode: str = "monolithic",
    max_depth: int = 16,
    hash_mode: str = "full",
    project_counterexamples: bool = True,
) -> NetworkVerifyReport:
    """Verify CTL formulas over an AgentNetwork.

    ``mode="monolithic"`` (default) builds the full joint product — behavior is
    byte-identical to Stage 6-4. ``mode="compositional"`` runs assume-guarantee
    discharge against per-leaf interface abstractions, falling back to the
    monolithic product for any out-of-class formula or spurious abstract FAIL
    (so per-formula verdicts always match monolithic).
    """
    if not formulas:
        raise ValueError("network_verify requires at least one formula")
    if mode not in ("monolithic", "compositional"):
        raise ValueError(f"mode must be 'monolithic' or 'compositional', got '{mode}'")

    if mode == "compositional":
        return _network_verify_compositional(
            network, bundles, formulas, max_depth=max_depth, hash_mode=hash_mode
        )

    components, component_ids = resolve_bundle_adapters(network, bundles)
    report = joint_verify(
        components,
        formulas,
        component_ids=component_ids,
        max_depth=max_depth,
        hash_mode=hash_mode,
        project_counterexamples=project_counterexamples,
    )
    return NetworkVerifyReport(
        verified=report.verified,
        network_id=network.network_id,
        component_ids=list(report.component_ids),
        formulas=list(report.formulas),
        verdicts=list(report.verdicts),
        state_count=report.state_count,
        edge_count=report.edge_count,
        deadlock_count=report.deadlock_count,
    )


def _network_verify_compositional(
    network: AgentNetworkBody,
    bundles: Mapping[str, AgentBundleBody],
    formulas: Sequence[str],
    *,
    max_depth: int,
    hash_mode: str,
) -> NetworkVerifyReport:
    center_bundle = bundles.get(network.center_bundle_ref)
    if center_bundle is None:
        raise KeyError(f"missing bundle artifact for ref '{network.center_bundle_ref}'")
    leaf_specs = resolve_leaf_specs(network, bundles)

    crep = assume_guarantee_verify(
        center_bundle,
        leaf_specs,
        formulas,
        center_id=network.center_bundle_ref,
        max_depth=max_depth,
        hash_mode=hash_mode,
    )
    verdicts = [JointVerdict(formula=v.formula, satisfied=v.satisfied) for v in crep.verdicts]
    return NetworkVerifyReport(
        verified=crep.verified,
        network_id=network.network_id,
        component_ids=[crep.center_id, *crep.leaf_ids],
        formulas=list(formulas),
        verdicts=verdicts,
        state_count=crep.compositional_state_count,
        edge_count=0,
        deadlock_count=0,
        mode="compositional",
        local_verdicts=[
            {"component_id": lv.component_id, "guarantee": lv.guarantee, "satisfied": lv.satisfied}
            for lv in crep.local_verdicts
        ],
        abstraction_stats={cid: st.to_dict() for cid, st in crep.abstraction_stats.items()},
        compositional_state_count=crep.compositional_state_count,
        monolithic_state_count=crep.monolithic_state_count,
        fallbacks=[
            {
                "formula": f.formula,
                "trigger": f.trigger,
                "reason": f.reason,
                "monolithic_satisfied": f.monolithic_satisfied,
                "via": v.via,
            }
            for f, v in _zip_fallbacks(crep)
        ],
    )


def _zip_fallbacks(crep) -> List[tuple]:
    """Pair each Fallback with the CompositionalVerdict for the same formula."""
    via_by_formula = {v.formula: v for v in crep.verdicts}
    return [(f, via_by_formula[f.formula]) for f in crep.fallbacks]


def network_verify_from_paths(
    network_path: Path,
    *,
    bundle_paths: Mapping[str, Path],
    formulas_path: Path | None = None,
    mode: str = "monolithic",
    max_depth: int = 16,
    hash_mode: str = "full",
) -> NetworkVerifyReport:
    network = load_agent_network_body(network_path)
    bundles = {ref: load_agent_bundle_body(path) for ref, path in bundle_paths.items()}
    formulas = load_formulas(formulas_path)
    return network_verify(
        network,
        bundles,
        formulas,
        mode=mode,
        max_depth=max_depth,
        hash_mode=hash_mode,
    )


def write_report_json(report: NetworkVerifyReport, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report.to_dict(), indent=2), encoding="utf-8")


__all__ = [
    "NetworkVerifyReport",
    "load_agent_bundle_body",
    "load_agent_network_body",
    "load_formulas",
    "network_verify",
    "network_verify_from_paths",
    "resolve_bundle_adapters",
    "resolve_leaf_specs",
    "write_report_json",
]
