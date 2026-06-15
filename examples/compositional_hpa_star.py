"""Compositional verification demo — Kubernetes HPA / anti-sybil star (Stage 7-V.7).

A horizontal-pod-autoscaler (HPA) star: one **center** controller aggregates KPIs
from N **pod replicas**. This is the anti-sybil shape — many near-identical leaves
whose joint product blows up multiplicatively (``|C|·∏ᵢ|podᵢ|``), exactly the
Kripke state explosion Phase-7 Req1 targets.

Run it to see the headline number — compositional ``state_count`` materially below
monolithic, with **per-formula verdicts identical**:

    python -m examples.compositional_hpa_star          # prints a scaling table
    python -m examples.compositional_hpa_star --max 7

The same scenario is asserted in ``tests/test_compositional_hpa_demo.py``.
"""

from __future__ import annotations

import argparse
from typing import Dict, List, Tuple

from tm.artifacts.models import AgentBundleBody, AgentNetworkBody
from tm.verify.network import network_verify

CENTER_REF = "hpa.center"


def _pod_ref(i: int) -> str:
    return f"pod.{i}"


# Center: observe aggregate signal, then decide to scale. 3 reachable states.
_CENTER_VERIFY = {
    "initial_store": {},
    "changed_paths": ["tick"],
    "steps": {
        "observe": {"reads": [], "writes": ["observed"]},
        "decide": {"reads": ["observed"], "writes": ["scaled"]},
    },
    "rules": [
        {"name": "on_tick", "triggers": ["tick"], "steps": ["observe"]},
        {"name": "on_observed", "triggers": ["observed"], "steps": ["decide"]},
    ],
}


# Pod replica: recv → proc → emit, culminating in the interface KPI `hot`.
# 4 concrete states; the interface abstraction keeps only `hot` → 2 states.
_POD_VERIFY = {
    "initial_store": {},
    "changed_paths": ["start"],
    "steps": {
        "recv": {"reads": [], "writes": ["r1"]},
        "proc": {"reads": ["r1"], "writes": ["r2"]},
        "emit": {"reads": ["r2"], "writes": ["hot"]},
    },
    "rules": [
        {"name": "on_start", "triggers": ["start"], "steps": ["recv"]},
        {"name": "on_r1", "triggers": ["r1"], "steps": ["proc"]},
        {"name": "on_r2", "triggers": ["r2"], "steps": ["emit"]},
    ],
}


def _bundle(ref: str, verify: dict) -> AgentBundleBody:
    return AgentBundleBody.from_mapping(
        {"bundle_id": ref, "agents": [], "plan": [], "meta": {"verify": verify}}
    )


def build_hpa_star(n_pods: int) -> Tuple[AgentNetworkBody, Dict[str, AgentBundleBody], List[str]]:
    """Build an HPA star with ``n_pods`` replicas plus a battery of CTL formulas.

    The formulas deliberately exercise every discharge route: single-component
    (exact local), decomposable (per-leaf local), multi-component safety (A-G
    against abstractions), and out-of-class liveness (monolithic fallback).
    """
    if n_pods < 1:
        raise ValueError("n_pods must be >= 1")

    pod_refs = [_pod_ref(i) for i in range(n_pods)]
    bundles: Dict[str, AgentBundleBody] = {CENTER_REF: _bundle(CENTER_REF, _CENTER_VERIFY)}
    edges = []
    for ref in pod_refs:
        bundles[ref] = _bundle(ref, _POD_VERIFY)
        edges.append({"from": ref, "to": CENTER_REF, "kpi_keys": ["hot"]})

    network = AgentNetworkBody.from_mapping(
        {
            "network_id": f"hpa.star.{n_pods}",
            "topology": "star",
            "center_bundle_ref": CENTER_REF,
            "leaf_bundle_refs": pod_refs,
            "edges": edges,
            "transport_default": "inprocess",
        }
    )

    no_crash = " && ".join(f"!has({ref}.crashed)" for ref in pod_refs)
    formulas = [
        # SINGLE_COMPONENT (exact local on the center): scaling is reachable.
        "EF has(hpa.center.scaled)",
        # SAFETY_GLOBAL, true: center never scales together with a crashed pod
        # (pods never crash) — discharged against the abstractions, no fallback.
        f"AG !(has(hpa.center.scaled) && has({pod_refs[0]}.crashed))",
        # SAFETY_GLOBAL, false: center may scale with no pod hot → spurious-FAIL
        # recheck confirms FALSE on the full product (anti-sybil "no quorum" probe).
        f"AG !(has(hpa.center.scaled) && !has({pod_refs[0]}.hot))",
        # DECOMPOSABLE: no pod ever crashes (per-leaf local discharge).
        f"AG ({no_crash})",
        # OUT_OF_CLASS liveness → monolithic fallback.
        "AF has(hpa.center.scaled)",
    ]
    return network, bundles, formulas


def run(n_pods: int, *, max_depth: int = 64, hash_mode: str = "store") -> Dict[str, object]:
    """Verify the n-pod star both ways and return comparison metrics."""
    network, bundles, formulas = build_hpa_star(n_pods)
    mono = network_verify(network, bundles, formulas, max_depth=max_depth, hash_mode=hash_mode)
    comp = network_verify(
        network, bundles, formulas, mode="compositional", max_depth=max_depth, hash_mode=hash_mode
    )

    mono_v = [v.satisfied for v in mono.verdicts]
    comp_v = [v.satisfied for v in comp.verdicts]
    mono_states = mono.state_count
    comp_states = comp.compositional_state_count or comp.state_count
    return {
        "n_pods": n_pods,
        "monolithic_states": mono_states,
        "compositional_states": comp_states,
        "reduction_x": (mono_states / comp_states) if comp_states else 0.0,
        "parity": mono_v == comp_v,
        "verdicts": comp_v,
        "fallbacks": [f["trigger"] for f in comp.fallbacks],
    }


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compositional HPA star demo")
    parser.add_argument("--min", type=int, default=2, help="min pod count")
    parser.add_argument("--max", type=int, default=6, help="max pod count")
    parser.add_argument("--max-depth", type=int, default=64)
    args = parser.parse_args(argv)

    print(f"{'pods':>5} {'monolithic':>11} {'compositional':>14} {'reduction':>10} {'parity':>7}")
    for n in range(args.min, args.max + 1):
        m = run(n, max_depth=args.max_depth)
        print(
            f"{m['n_pods']:>5} {m['monolithic_states']:>11} {m['compositional_states']:>14} "
            f"{m['reduction_x']:>9.1f}x {str(m['parity']):>7}"
        )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
