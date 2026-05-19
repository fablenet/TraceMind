from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List

from .spec import FlowSpec
from .inspector import FlowInspector


@dataclass(frozen=True)
class FlowArtifact:
    base_path: Path
    json_path: Path
    dot_path: Path


def export_flow_artifact(spec: FlowSpec, out_dir: str | Path) -> FlowArtifact:
    """Persist DOT and JSON artifacts for ``spec`` into ``out_dir``."""

    path = Path(out_dir)
    path.mkdir(parents=True, exist_ok=True)

    flow_id = spec.flow_id or spec.name
    flow_rev = spec.flow_revision()
    slug = _slugify(f"{flow_id}@{flow_rev}")
    json_path = path / f"{slug}.json"
    dot_path = path / f"{slug}.dot"

    inspector = FlowInspector(spec)
    payload = inspector.export_json()
    nodes: list[dict[str, object]] = []
    steps_obj = payload.get("steps")
    if isinstance(steps_obj, list):
        for step in steps_obj:
            if not isinstance(step, dict):
                continue
            name = step.get("name")
            if not isinstance(name, str):
                continue
            nodes.append(
                {
                    "name": name,
                    "step_id": step.get("step_id"),
                    "operation": step.get("operation"),
                }
            )
    edges = _edges(spec)

    artifact = {
        "flow_id": flow_id,
        "flow_name": spec.name,
        "flow_rev": flow_rev,
        "entry": spec.entrypoint,
        "nodes": nodes,
        "edges": edges,
    }
    json_path.write_text(_dump_json(artifact), encoding="utf-8")
    dot_path.write_text(_build_dot(spec, nodes, edges), encoding="utf-8")

    return FlowArtifact(base_path=path, json_path=json_path, dot_path=dot_path)


def _dump_json(data: dict) -> str:
    import json

    return json.dumps(data, indent=2, ensure_ascii=False)


def _edges(spec: FlowSpec) -> List[dict[str, str]]:
    edges: List[dict[str, str]] = []
    for step in spec:
        for nxt in step.next_steps:
            edges.append({"from": step.name, "to": nxt})
    return edges


def _build_dot(spec: FlowSpec, nodes: Iterable[dict], edges: Iterable[dict]) -> str:
    lines: List[str] = []
    flow_id = spec.flow_id or spec.name
    header = f'digraph "{flow_id}@{spec.flow_revision()}" {{'
    lines.append(header)
    lines.append("  node [shape=box];")

    entry = spec.entrypoint
    for node in nodes:
        name = node.get("name")
        step_id = node.get("step_id", "")
        if not name:
            continue
        label = f"{name}\\n{step_id}" if step_id else name
        attrs = [f'label="{label}"']
        if entry and name == entry:
            attrs.append("shape=doubleoctagon")
        lines.append(f'  "{name}" [{", ".join(attrs)}];')

    for edge in edges:
        src = edge.get("from")
        dst = edge.get("to")
        if src and dst:
            lines.append(f'  "{src}" -> "{dst}";')

    lines.append("}")
    return "\n".join(lines) + "\n"


def _slugify(value: str) -> str:
    safe = []
    for ch in value:
        if ch.isalnum() or ch in {"-", "_", ".", "@"}:
            safe.append(ch)
        else:
            safe.append("-")
    return "".join(safe)


__all__ = ["FlowArtifact", "export_flow_artifact"]
