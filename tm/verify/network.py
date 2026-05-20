"""Joint Kripke verification over AgentNetwork topologies — Phase 6 Stage 6-4.

Loads an :class:`AgentNetworkBody` plus referenced bundle artifacts, composes
N offline component adapters, and runs :func:`joint_verify` with optional
``peer()`` CTL sugar and cross-node counterexample projection.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Sequence

try:
    import yaml  # type: ignore[import-untyped]
except ModuleNotFoundError:
    yaml = None

from tm.artifacts.models import AgentBundleBody, AgentNetworkBody
from tm.verify.bundle_adapter import adapter_from_bundle
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

    def to_dict(self) -> Dict[str, Any]:
        return {
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


def network_verify(
    network: AgentNetworkBody,
    bundles: Mapping[str, AgentBundleBody],
    formulas: Sequence[str],
    *,
    max_depth: int = 16,
    hash_mode: str = "full",
    project_counterexamples: bool = True,
) -> NetworkVerifyReport:
    """Verify CTL formulas over the joint product of an AgentNetwork."""
    if not formulas:
        raise ValueError("network_verify requires at least one formula")

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


def network_verify_from_paths(
    network_path: Path,
    *,
    bundle_paths: Mapping[str, Path],
    formulas_path: Path | None = None,
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
    "write_report_json",
]
