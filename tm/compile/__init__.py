"""Compilation utilities — Stage 5-4 deliverables.

Currently exports the :func:`compile_intent_to_bundle` chain (task 4.5).
"""

from .intent_to_bundle import (
    CATEGORY_TO_STAGE,
    CompilationResult,
    MAPE_PHASES,
    compile_intent_to_bundle,
)

__all__ = [
    "CATEGORY_TO_STAGE",
    "CompilationResult",
    "MAPE_PHASES",
    "compile_intent_to_bundle",
]
