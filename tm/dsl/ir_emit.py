from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence, Tuple
import hashlib

from .compiler_flow import FlowCompilation
from .compiler_policy import PolicyCompilation
from .ir import WdlInput

IR_VERSION = "1.0.0"


class IrEmissionError(RuntimeError):
    """Raised when IR generation fails."""


@dataclass
class FlowIrBuildResult:
    """In-memory representation of an emitted Flow IR."""

    flow_id: str
    ir: Dict[str, Any]
    manifest_entry: Dict[str, Any]


def build_flow_ir(
    compilation: FlowCompilation,
    *,
    policy_resolver: Mapping[Path, PolicyCompilation],
    generated_at: Optional[datetime] = None,
) -> FlowIrBuildResult:
    flow = compilation.data.get("flow")
    if not isinstance(flow, dict):
        raise IrEmissionError("Compiled flow payload missing 'flow' object")

    steps = flow.get("steps")
    if not isinstance(steps, list):
        raise IrEmissionError("Compiled flow missing 'steps' array")
    edges = flow.get("edges")
    if not isinstance(edges, list):
        raise IrEmissionError("Compiled flow missing 'edges' array")

    nodes, step_kinds, policy_info = _build_nodes(steps, policy_resolver)
    edges_ir = _build_edges(edges)
    inputs_schema = _build_inputs_schema(compilation.workflow.inputs)
    metadata = _build_metadata(compilation, generated_at=generated_at)

    ir_payload = {
        "version": IR_VERSION,
        "flow": {
            "name": compilation.flow_id,
            "timeout_ms": _extract_flow_timeout(flow),
        },
        "constants": {
            "policyRef": policy_info.ref,
            "policy": policy_info.payload,
        },
        "inputs_schema": inputs_schema,
        "graph": {
            "nodes": nodes,
            "edges": edges_ir,
        },
        "metadata": metadata,
    }

    manifest_entry = {
        "name": compilation.flow_id,
        "source_file": str(compilation.source) if compilation.source else None,
        "policyRef": policy_info.ref,
        "step_kinds": sorted(step_kinds),
        "inputs_schema_hash": _hash_inputs_schema(inputs_schema),
        # ir_path is populated by the caller once the payload is written to disk.
        "ir_path": None,
    }

    return FlowIrBuildResult(flow_id=compilation.flow_id, ir=ir_payload, manifest_entry=manifest_entry)


# --------------------------------------------------------------------------- helpers
@dataclass
class _PolicyInfo:
    ref: str
    payload: Dict[str, Any]


def _build_nodes(
    steps: Iterable[dict],
    policy_resolver: Mapping[Path, PolicyCompilation],
) -> Tuple[list[Dict[str, Any]], set[str], _PolicyInfo]:
    nodes: list[Dict[str, Any]] = []
    kinds: set[str] = set()
    policy_ref: Optional[str] = None
    policy_payload: Dict[str, Any] = {}

    for step in steps:
        if not isinstance(step, dict):
            raise IrEmissionError("Step entry must be an object")
        step_id = step.get("id")
        if not isinstance(step_id, str) or not step_id:
            raise IrEmissionError("Step missing id")
        kind = step.get("kind")
        if not isinstance(kind, str):
            kind = "task"

        raw_config = step.get("config")
        config: Mapping[str, Any] = raw_config if isinstance(raw_config, Mapping) else {}
        timeout_ms = step.get("timeout_ms")
        if not isinstance(timeout_ms, int) or timeout_ms < 0:
            timeout_ms = 0
        retry_config = _extract_retry(config)

        node_type, params = _classify_node(kind, config)
        kinds.add(node_type)

        if policy_ref is None and isinstance(raw_config, Mapping):
            ref = raw_config.get("policy_ref")
            if isinstance(ref, str):
                policy_ref = (
                    raw_config.get("policy_id") if isinstance(raw_config.get("policy_id"), str) else Path(ref).stem
                )
                payload = _resolve_policy_payload(ref, policy_resolver)
                if payload is not None:
                    policy_payload = payload
                else:
                    policy_payload = {}

        node = {
            "id": step_id,
            "type": node_type,
            "with": params,
            "timeout_ms": timeout_ms,
            "retry": retry_config,
        }
        nodes.append(node)

    if policy_ref is None:
        policy_ref = ""

    return nodes, kinds, _PolicyInfo(ref=policy_ref, payload=policy_payload)


def _resolve_policy_payload(ref: str, resolver: Mapping[Path, PolicyCompilation]) -> Optional[Dict[str, Any]]:
    path = Path(ref)
    resolved = path.resolve()
    compilation = resolver.get(resolved)
    if compilation is not None:
        payload = copy.deepcopy(compilation.data)
        if isinstance(payload, Mapping):
            return dict(payload)
        return payload if isinstance(payload, dict) else None
    if resolved.exists():
        try:
            loaded = json.loads(resolved.read_text(encoding="utf-8"))
            return loaded if isinstance(loaded, dict) else None
        except Exception:
            return None
    return None


def _classify_node(kind: str, config: Mapping[str, Any]) -> Tuple[str, Dict[str, Any]]:
    if kind == "switch":
        params = {
            "key_from": config.get("key_from"),
            "cases": config.get("cases", {}),
            "default": config.get("default"),
        }
        return "switch", params

    call = config.get("call") if isinstance(config, Mapping) else None
    if isinstance(call, Mapping):
        target = call.get("target")
        if isinstance(target, str) and target:
            args_value = call.get("args")
            args: Mapping[str, Any] = args_value if isinstance(args_value, Mapping) else {}
            return target, dict(args)
    outputs = config.get("outputs") if isinstance(config, Mapping) else None
    if isinstance(outputs, Mapping):
        return "dsl.emit", dict(outputs)

    return kind or "task", dict(config)


def _extract_retry(config: Mapping[str, Any]) -> Dict[str, int]:
    retry = config.get("retry")
    if not isinstance(retry, Mapping):
        return {"max": 0, "backoff_ms": 0}
    max_attempts = retry.get("max")
    backoff = retry.get("backoff_ms")
    return {
        "max": max_attempts if isinstance(max_attempts, int) and max_attempts >= 0 else 0,
        "backoff_ms": backoff if isinstance(backoff, int) and backoff >= 0 else 0,
    }


def _build_edges(edges: Iterable[dict]) -> list[Dict[str, Any]]:
    edge_list: list[Dict[str, Any]] = []
    for edge in edges:
        if not isinstance(edge, dict):
            raise IrEmissionError("Edge entry must be an object")
        src = edge.get("from")
        dst = edge.get("to")
        if not isinstance(src, str) or not isinstance(dst, str):
            raise IrEmissionError("Edge requires 'from' and 'to' strings")
        payload: Dict[str, Any] = {
            "from": src,
            "to": dst,
            "on": "success",
        }
        when = edge.get("when")
        if isinstance(when, str):
            payload["case"] = when
        edge_list.append(payload)
    return edge_list


def _build_inputs_schema(inputs: Sequence[WdlInput]) -> Dict[str, Any]:
    if not inputs:
        return {}
    schema = {}
    for item in inputs:
        schema[item.name] = {"type": item.type_name}
    return schema


def _hash_inputs_schema(schema: Dict[str, Any]) -> str:
    encoded = json.dumps(schema, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _build_metadata(compilation: FlowCompilation, *, generated_at: Optional[datetime]) -> Dict[str, Any]:
    ts = generated_at or datetime.now(timezone.utc)
    return {
        "generated_at": ts.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "source_file": str(compilation.source) if compilation.source else None,
    }


def _extract_flow_timeout(flow: Mapping[str, Any]) -> int:
    timeout = flow.get("timeout_ms")
    if isinstance(timeout, int) and timeout >= 0:
        return timeout
    return 0
