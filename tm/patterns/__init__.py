"""Pattern Library — Stage 5-3 deliverable.

Public API:

- :class:`PatternLibrary` — registry of loaded PropertyPattern templates
- :func:`load_seed_patterns` — convenience to load the shipped seeds
- :func:`instantiate_pattern` (in :mod:`tm.patterns.instantiate`) —
  template + slot fills → :class:`tm.artifacts.IntentBody` candidate
"""

from .instantiate import (
    IntentBuildRequest,
    PatternInstance,
    PatternInstantiationError,
    build_intent_from_patterns,
    instantiate_pattern,
)
from .library import (
    SEED_ROOT,
    PatternEntry,
    PatternLibrary,
    load_seed_patterns,
)

__all__ = [
    "IntentBuildRequest",
    "PatternEntry",
    "PatternInstance",
    "PatternInstantiationError",
    "PatternLibrary",
    "SEED_ROOT",
    "build_intent_from_patterns",
    "instantiate_pattern",
    "load_seed_patterns",
]
