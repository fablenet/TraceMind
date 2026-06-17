"""Convert AgentBundle artifacts into TraceMind Kripke adapters.

Each bundle may declare an offline verification model under ``meta.verify``::

    meta:
      verify:
        initial_store: {}
        changed_paths: ["start"]
        steps:
          detect:
            reads: []
            writes: [detected]
        rules:
          - name: on_start
            triggers: [start]
            steps: [detect]

This keeps network-level joint verification independent of runtime transport.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Sequence

from tm.artifacts.models import AgentBundleBody
from tm.pipeline.engine import Plan, Rule, StepSpec
from tm.verify.adapter import TraceMindAdapter
from tm.verify.spec import VerifySpec


def _noop(ctx: object) -> object:
    return ctx


def plan_from_verify_meta(meta: Mapping[str, Any]) -> Plan:
    """Build a :class:`Plan` from ``meta.verify`` step/rule declarations."""
    steps_raw = meta.get("steps") or {}
    if not isinstance(steps_raw, Mapping):
        raise ValueError("meta.verify.steps must be a mapping")

    steps: Dict[str, StepSpec] = {}
    for name, raw in steps_raw.items():
        if not isinstance(raw, Mapping):
            raise ValueError(f"meta.verify.steps['{name}'] must be a mapping")
        steps[str(name)] = StepSpec(
            name=str(name),
            reads=[str(r) for r in (raw.get("reads") or [])],
            writes=[str(w) for w in (raw.get("writes") or [])],
            fn=_noop,
            clears=[str(c) for c in (raw.get("clears") or [])],
        )

    rules_raw = meta.get("rules") or []
    if not isinstance(rules_raw, Sequence) or isinstance(rules_raw, str):
        raise ValueError("meta.verify.rules must be a sequence")

    rules: List[Rule] = []
    for raw in rules_raw:
        if not isinstance(raw, Mapping):
            raise ValueError("each meta.verify.rules entry must be a mapping")
        rules.append(
            Rule(
                name=str(raw.get("name") or "rule"),
                triggers=[str(t) for t in (raw.get("triggers") or [])],
                steps=[str(s) for s in (raw.get("steps") or [])],
            )
        )

    if not steps:
        raise ValueError("meta.verify.steps must contain at least one step")
    return Plan(steps=steps, rules=rules)


def verify_spec_from_bundle(body: AgentBundleBody) -> VerifySpec:
    """Parse ``meta.verify`` from an AgentBundle body."""
    meta = body.meta.get("verify")
    if not isinstance(meta, Mapping):
        raise ValueError(f"bundle '{body.bundle_id}' missing meta.verify mapping")

    properties_raw = meta.get("properties") or []
    properties = []
    if isinstance(properties_raw, Sequence) and not isinstance(properties_raw, str):
        from tm.verify.spec import PropertySpec

        for i, entry in enumerate(properties_raw):
            if not isinstance(entry, Mapping):
                continue
            properties.append(
                PropertySpec(
                    name=str(entry.get("name") or f"property_{i}"),
                    formula=str(entry.get("formula") or ""),
                )
            )

    return VerifySpec(
        initial_store=dict(meta.get("initial_store") or {}),
        initial_pending=[str(x) for x in (meta.get("initial_pending") or [])],
        changed_paths=[str(x) for x in (meta.get("changed_paths") or [])],
        invariants=[str(x) for x in (meta.get("invariants") or [])],
        properties=properties,
    )


def adapter_from_bundle(body: AgentBundleBody) -> TraceMindAdapter:
    """Build a component adapter from bundle ``meta.verify``."""
    meta = body.meta.get("verify")
    if not isinstance(meta, Mapping):
        raise ValueError(f"bundle '{body.bundle_id}' missing meta.verify mapping")
    plan = plan_from_verify_meta(meta)
    spec = verify_spec_from_bundle(body)
    return TraceMindAdapter.from_plan(
        plan,
        initial_store=spec.initial_store,
        changed_paths=spec.changed_paths,
        initial_pending=spec.initial_pending,
    )


__all__ = [
    "adapter_from_bundle",
    "plan_from_verify_meta",
    "verify_spec_from_bundle",
]
