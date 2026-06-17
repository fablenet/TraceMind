from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping

try:
    import yaml  # type: ignore[import-untyped]
except ModuleNotFoundError:
    yaml = None

from tm.pipeline.engine import Plan, Rule, StepSpec


@dataclass
class PropertySpec:
    name: str
    formula: str


@dataclass
class VerifySpec:
    initial_store: Dict[str, Any]
    initial_pending: List[str]
    changed_paths: List[str]
    invariants: List[str]
    properties: List[PropertySpec]


def _load_structured(path: Path) -> Mapping[str, Any]:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() in {".yaml", ".yml"}:
        if yaml is None:
            raise RuntimeError("PyYAML is required to read YAML specs")
        return yaml.safe_load(text) or {}
    return json.loads(text)


def load_plan(path: Path) -> Plan:
    data = _load_structured(path)
    steps_raw = data.get("steps") or {}
    rules_raw = data.get("rules") or []
    steps: Dict[str, StepSpec] = {}
    for name, raw in steps_raw.items():
        reads = list(raw.get("reads") or [])
        writes = list(raw.get("writes") or [])
        clears = list(raw.get("clears") or [])
        steps[name] = StepSpec(name=name, reads=reads, writes=writes, fn=lambda ctx: ctx, clears=clears)
    rules: List[Rule] = []
    for raw in rules_raw:
        rules.append(
            Rule(
                name=str(raw.get("name", "")) or "rule",
                triggers=list(raw.get("triggers") or []),
                steps=list(raw.get("steps") or []),
            )
        )
    return Plan(steps=steps, rules=rules)


def load_spec(path: Path) -> VerifySpec:
    data = _load_structured(path)
    invariants = [str(inv) for inv in data.get("invariants", [])]
    properties = [
        PropertySpec(name=str(entry.get("name", f"property_{i}")), formula=str(entry.get("formula", "")))
        for i, entry in enumerate(data.get("properties", []))
    ]
    return VerifySpec(
        initial_store=dict(data.get("initial_store") or {}),
        initial_pending=list(data.get("initial_pending") or []),
        changed_paths=list(data.get("changed_paths") or []),
        invariants=invariants,
        properties=properties,
    )
