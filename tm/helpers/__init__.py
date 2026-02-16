from __future__ import annotations

import asyncio
import importlib
import inspect
import json
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Sequence, List

try:
    import yaml  # type: ignore[import-untyped]
except ModuleNotFoundError:  # pragma: no cover
    yaml = None


# ---------------------------------------------------------------------------
# Callable resolution
# ---------------------------------------------------------------------------


def _resolve_callable(ref: Any) -> Callable:
    if callable(ref):
        return ref
    if not isinstance(ref, str):
        raise ValueError(f"Cannot resolve callable from {ref!r}")
    if ":" in ref:
        module, _, attr = ref.partition(":")
    else:
        module, _, attr = ref.rpartition(".")
    if not module or not attr:
        raise ValueError(f"Invalid callable reference '{ref}'")
    module_obj = importlib.import_module(module)
    if not hasattr(module_obj, attr):
        raise ValueError(f"Module '{module}' has no attribute '{attr}'")
    target = getattr(module_obj, attr)
    if not callable(target):
        raise TypeError(f"Resolved reference '{ref}' is not callable")
    return target


async def _maybe_call(func: Callable, *args, **kwargs):
    if inspect.iscoroutinefunction(func):
        return await func(*args, **kwargs)
    return func(*args, **kwargs)


# ---------------------------------------------------------------------------
# Switch/when/parallel helpers
# ---------------------------------------------------------------------------


def _to_str(value: Any) -> str:
    return "" if value is None else str(value)


async def switch(
    ctx: Mapping[str, Any],
    state: Mapping[str, Any],
    *,
    selector: Callable[[Mapping[str, Any], Mapping[str, Any]], Any] | str | None = None,
    cases: Mapping[str, Any] | None = None,
    default: str | None = None,
) -> str:
    """Return branch label for switch steps."""
    config = ctx.get("config", {}) if isinstance(ctx, Mapping) else {}
    if cases is None and isinstance(config, Mapping):
        cases = config.get("cases")
        default = default or config.get("default")
    if selector is None and isinstance(config, Mapping):
        selector = config.get("selector")

    if selector is None:
        value = state.get("branch")
    else:
        func = _resolve_callable(selector)
        value = await _maybe_call(func, ctx, state)
    label = _to_str(value)
    if cases and label not in cases:
        return _to_str(default)
    return label or _to_str(default)


async def when(
    ctx: Mapping[str, Any],
    state: Mapping[str, Any],
    predicate: Callable[[Mapping[str, Any], Mapping[str, Any]], bool] | str | None = None,
) -> bool:
    """Evaluate predicate for conditional routing."""
    if predicate is None:
        return bool(state)
    func = _resolve_callable(predicate)
    result = await _maybe_call(func, ctx, state)
    return bool(result)


async def parallel(
    ctx: Mapping[str, Any],
    state: Mapping[str, Any],
    branches: Mapping[str, Callable | str] | None = None,
) -> Dict[str, Any]:
    """Execute branch callables concurrently and merge their outputs."""

    async def runner(name: str, func: Callable) -> tuple[str, Any]:
        value = await _maybe_call(func, ctx, state)
        return name, value

    config = ctx.get("config", {}) if isinstance(ctx, Mapping) else {}
    if branches is None and isinstance(config, Mapping):
        branches = config.get("branches_map")
    if branches is None:
        raise ValueError("parallel helper requires branches mapping")
    tasks = [runner(name, _resolve_callable(func)) for name, func in branches.items()]
    results: Dict[str, Any] = {}
    for name, value in await asyncio.gather(*tasks):
        results[name] = value

    merged: Dict[str, Any] = {}
    for name, value in results.items():
        if isinstance(value, Mapping):
            merged = deep_merge(merged, value)
        else:
            merged[name] = value
    return merged


# ---------------------------------------------------------------------------
# I/O helpers
# ---------------------------------------------------------------------------


def load_json(source: str | Path) -> Any:
    path = Path(source)
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return json.loads(str(source))


def dump_json(data: Any, *, indent: int = 2) -> str:
    return json.dumps(data, ensure_ascii=False, indent=indent)


def load_yaml(source: str | Path) -> Any:
    if yaml is None:
        raise RuntimeError("PyYAML not installed")
    path = Path(source)
    if path.exists():
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    return yaml.safe_load(str(source))


# ---------------------------------------------------------------------------
# Patch helpers
# ---------------------------------------------------------------------------


def deep_merge(a: Any, b: Any, *, array_mode: str = "replace") -> Any:
    if isinstance(a, Mapping) and isinstance(b, Mapping):
        result = {k: deepcopy(v) for k, v in a.items()}
        for key, value in b.items():
            if key in result:
                result[key] = deep_merge(result[key], value, array_mode=array_mode)
            else:
                result[key] = deepcopy(value)
        return result
    if isinstance(a, list) and isinstance(b, list):
        return a + list(b) if array_mode == "concat" else list(b)
    return deepcopy(b)


def json_patch_diff(src: Any, dst: Any, path: str = "") -> List[Dict[str, Any]]:
    if src == dst:
        return []
    if isinstance(src, Mapping) and isinstance(dst, Mapping):
        ops: List[Dict[str, Any]] = []
        for key in src.keys() - dst.keys():
            ops.append({"op": "remove", "path": f"{path}/{key}"})
        for key in dst.keys() - src.keys():
            ops.append({"op": "add", "path": f"{path}/{key}", "value": dst[key]})
        for key in src.keys() & dst.keys():
            ops.extend(json_patch_diff(src[key], dst[key], f"{path}/{key}"))
        return ops
    if isinstance(src, list) and isinstance(dst, list):
        if src == dst:
            return []
        return [{"op": "replace", "path": path or "/", "value": dst}]
    return [{"op": "replace", "path": path or "/", "value": dst}]


def json_patch_apply(doc: Any, patch: Sequence[Mapping[str, Any]]) -> Any:
    target = deepcopy(doc)

    def _set(root: Any, pointer: str, value: Any) -> None:
        parts = [p for p in pointer.split("/") if p]
        node = root
        for part in parts[:-1]:
            if isinstance(node, list):
                node = node[int(part)]
            else:
                node = node.setdefault(part, {})
        if not parts:
            raise ValueError("Cannot set root value using empty path")
        key = parts[-1]
        if isinstance(node, list):
            idx = int(key)
            if idx == len(node):
                node.append(value)
            else:
                node[idx] = value
        else:
            node[key] = value

    def _remove(root: Any, pointer: str) -> None:
        parts = [p for p in pointer.split("/") if p]
        node = root
        for part in parts[:-1]:
            node = node[int(part)] if isinstance(node, list) else node[part]
        key = parts[-1]
        if isinstance(node, list):
            node.pop(int(key))
        else:
            node.pop(key, None)

    for op in patch:
        name = op.get("op")
        path = op.get("path")
        if not isinstance(path, str):
            raise ValueError("Patch entry missing path")
        if name == "add" or name == "replace":
            _set(target, path, op.get("value"))
        elif name == "remove":
            _remove(target, path)
        else:
            raise ValueError(f"Unsupported patch operation '{name}'")
    return target


def json_merge_patch(target: Any, patch: Mapping[str, Any]) -> Any:
    if not isinstance(patch, Mapping):
        return deepcopy(patch)
    if not isinstance(target, Mapping):
        target = {}
    result = {k: deepcopy(v) for k, v in target.items()}
    for key, value in patch.items():
        if value is None:
            result.pop(key, None)
        elif isinstance(value, Mapping):
            result[key] = json_merge_patch(result.get(key, {}), value)
        else:
            result[key] = deepcopy(value)
    return result


def plan_has_patch(ctx: Mapping[str, Any], state: Mapping[str, Any]) -> bool:
    if isinstance(state, Mapping):
        candidate = state.get("plan_patch") or state.get("reflection", {}).get("plan_patch")
        if candidate:
            return True
    if isinstance(ctx, Mapping):
        paths = [
            ctx.get("plan", {}).get("plan_patch"),
            ctx.get("reflect", {}).get("reflection", {}).get("plan_patch"),
        ]
        return any(bool(p) for p in paths)
    return False


def apply_patch(document: Any, patch: Mapping[str, Any] | Sequence[Mapping[str, Any]] | None) -> Any:
    if not patch:
        return deepcopy(document)
    ops: Sequence[Mapping[str, Any]]
    if isinstance(patch, Mapping):
        ops = patch.get("ops")  # type: ignore[assignment]
        if not isinstance(ops, Sequence):
            raise ValueError("plan_patch.ops must be a sequence")
    elif isinstance(patch, Sequence):
        ops = patch
    else:
        raise ValueError("patch must be a mapping or sequence")
    return json_patch_apply(document, ops)


__all__ = [
    "switch",
    "when",
    "parallel",
    "load_json",
    "load_yaml",
    "dump_json",
    "deep_merge",
    "json_patch_diff",
    "json_patch_apply",
    "json_merge_patch",
    "plan_has_patch",
    "apply_patch",
]
