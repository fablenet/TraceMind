"""Pattern Library — discovery and lookup of seed PropertyPattern templates.

The Pattern Library is **Phase 5 Stage 5-3's deliverable**: the canonical
home for domain-neutral PropertyPattern templates. Seeds ship with the
``trace-mind`` package; downstream products (FableNet, K8s scenarios, …)
load them at runtime and fill in slots to produce concrete property
specifications.

## File layout

```
tm/patterns/seed/
    safety/                 # one directory per pattern category
        no_x_amplifies_y.yaml
    liveness/
        eventually_x_holds.yaml
    fairness/
        bounded_x_across_actors.yaml
```

Each ``*.yaml`` is a **body-only** PropertyPattern document — the body
fields described in :class:`tm.artifacts.PropertyPatternBody`. Envelope is
not stored on disk; it is constructed at instantiation time (see
:mod:`tm.patterns.instantiate`) so that each instance has fresh,
properly-hashed envelope metadata.

## Why body-only on disk?

- Easier to edit (no need to recompute hashes on each change)
- Library-level identity is the ``pattern_id``, not an envelope hash
- Decouples "this is the template" from "this is a concrete instance"

Concrete pattern instances (post-slot-fill) are full artifacts with
envelopes and are stored / registered separately by downstream products.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, List, Mapping

from tm.artifacts import PropertyPatternBody, validate_property_pattern_spec

SEED_ROOT: Path = Path(__file__).resolve().parent / "seed"


def _import_yaml():
    try:
        import yaml  # type: ignore[import-untyped]
    except ModuleNotFoundError as exc:  # pragma: no cover - rare optional dep
        raise RuntimeError("PyYAML is required to load pattern seeds") from exc
    return yaml


def _load_pattern_body(path: Path) -> PropertyPatternBody:
    yaml = _import_yaml()
    text = path.read_text(encoding="utf-8")
    raw = yaml.safe_load(text)
    if not isinstance(raw, Mapping):
        raise ValueError(f"{path}: pattern body must be a YAML mapping")
    validate_property_pattern_spec(raw)
    return PropertyPatternBody.from_mapping(raw)


@dataclass(frozen=True)
class PatternEntry:
    """A loaded seed pattern with its source path for diagnostics."""

    body: PropertyPatternBody
    path: Path

    @property
    def pattern_id(self) -> str:
        return self.body.pattern_id

    @property
    def category(self) -> str:
        return self.body.category


class PatternLibrary:
    """In-memory registry of loaded PropertyPattern templates.

    Construction is cheap; loading from disk is lazy on first access via
    :meth:`load` (or eager via :meth:`from_directory`). The library
    intentionally keeps an immutable view: extending the library at runtime
    requires constructing a new instance.
    """

    def __init__(self, entries: Iterable[PatternEntry] = ()) -> None:
        self._by_id: dict[str, PatternEntry] = {}
        for entry in entries:
            self._add(entry)

    @classmethod
    def from_directory(cls, root: Path | None = None) -> "PatternLibrary":
        """Eagerly load every ``*.yaml`` under ``root`` (recursive).

        Defaults to the shipped seed directory under ``tm/patterns/seed/``.
        Files are loaded depth-first in sorted order for deterministic
        listing.
        """
        base = (root or SEED_ROOT).resolve()
        if not base.exists():
            raise FileNotFoundError(f"pattern directory not found: {base}")
        entries: List[PatternEntry] = []
        for path in sorted(base.rglob("*.yaml")):
            body = _load_pattern_body(path)
            entries.append(PatternEntry(body=body, path=path))
        return cls(entries)

    def _add(self, entry: PatternEntry) -> None:
        if entry.pattern_id in self._by_id:
            existing = self._by_id[entry.pattern_id].path
            raise ValueError(f"duplicate pattern_id '{entry.pattern_id}' (existing: {existing}, new: {entry.path})")
        self._by_id[entry.pattern_id] = entry

    def __contains__(self, pattern_id: str) -> bool:
        return pattern_id in self._by_id

    def __len__(self) -> int:
        return len(self._by_id)

    def ids(self) -> List[str]:
        """All pattern ids, sorted lexicographically."""
        return sorted(self._by_id.keys())

    def get(self, pattern_id: str) -> PatternEntry:
        """Look up a pattern by id. Raises ``KeyError`` if unknown."""
        if pattern_id not in self._by_id:
            available = ", ".join(self.ids()) or "<empty>"
            raise KeyError(f"pattern '{pattern_id}' not found in library; available: {available}")
        return self._by_id[pattern_id]

    def filter_by_category(self, category: str) -> List[PatternEntry]:
        """All entries with the given category, sorted by id."""
        return [self._by_id[pid] for pid in self.ids() if self._by_id[pid].category == category]

    def entries(self) -> List[PatternEntry]:
        """All entries, sorted by id (stable iteration order)."""
        return [self._by_id[pid] for pid in self.ids()]

    def summary(self) -> List[dict[str, Any]]:
        """Compact dict view for CLI ``pattern list``."""
        return [
            {
                "pattern_id": entry.pattern_id,
                "category": entry.category,
                "title": entry.body.title,
                "slots": [slot.name for slot in entry.body.slots],
            }
            for entry in self.entries()
        ]


def load_seed_patterns() -> PatternLibrary:
    """Convenience: load the shipped seed pattern library.

    Equivalent to ``PatternLibrary.from_directory(SEED_ROOT)``.
    """
    return PatternLibrary.from_directory(SEED_ROOT)


__all__ = [
    "PatternEntry",
    "PatternLibrary",
    "SEED_ROOT",
    "load_seed_patterns",
]
