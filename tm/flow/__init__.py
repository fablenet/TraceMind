# TraceMind Flow package
from importlib import import_module
from typing import Any

from .registry import checks as checks, registry as registry

_LAZY_EXPORTS: dict[str, tuple[str, str]] = {
    "FlowGraph": ("tm.flow.graph", "FlowGraph"),
    "NodeKind": ("tm.flow.graph", "NodeKind"),
    "Step": ("tm.flow.graph", "Step"),
    "chain": ("tm.flow.graph", "chain"),
    "FlowBase": ("tm.flow.repo", "FlowBase"),
    "FlowRepo": ("tm.flow.repo", "FlowRepo"),
    "flowrepo": ("tm.flow.repo", "flowrepo"),
    "StaticAnalyzer": ("tm.flow.analyzer", "StaticAnalyzer"),
    "AirflowStyleTracer": ("tm.flow.tracer", "AirflowStyleTracer"),
    "Engine": ("tm.flow.engine", "Engine"),
    "RecipeLoader": ("tm.flow.recipe_loader", "RecipeLoader"),
    "RecipeError": ("tm.flow.recipe_loader", "RecipeError"),
}


def __getattr__(name: str) -> Any:
    try:
        module_name, attr_name = _LAZY_EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from exc
    module = import_module(module_name)
    value = getattr(module, attr_name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(list(globals().keys()) + list(_LAZY_EXPORTS.keys()))


__all__ = ["registry", "checks", *_LAZY_EXPORTS.keys()]
