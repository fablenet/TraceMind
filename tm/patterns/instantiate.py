"""Pattern instantiation — template + slot fills → concrete CTL formula.

This module is the **non-LLM bottom path** (Stage 5-3 task 3.3): given a
PropertyPattern template and a dict of slot fills, produce:

1. A concrete CTL formula string (template substitution)
2. An :class:`tm.artifacts.IntentBody` candidate that references the
   pattern + slot fills, suitable for ``tm artifacts verify``

Both outputs are produced from **purely declarative input**: no LLM
provider is invoked. This satisfies the Phase 5 invariant "every LLM
path must have an equivalent non-LLM path".

## Slot fill semantics

- Each slot value is a string substituted into the template via
  ``str.format(**slot_fills)``
- Slot values may be **CTL sub-expressions** (not just atoms), e.g.
  ``mediation_predicate = "has(tier_downgraded) OR has(quarantined)"``
  for anti-sybil's L2 property — the formula template just splices them in
- The resolved formula is validated by ``tm.verify.ctl.parse_expr`` to
  catch obvious syntax errors at instantiation time
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Sequence

from tm.artifacts import IntentBody, PropertyPatternBody
from tm.verify.ctl import parse_expr

from .library import PatternEntry, PatternLibrary


@dataclass(frozen=True)
class PatternInstance:
    """Concrete instantiation of a :class:`PropertyPatternBody`.

    Carries the resolved CTL formula plus enough provenance to trace back
    to the source pattern (for governance, regression, and KB feedback).
    """

    pattern_id: str
    category: str
    resolved_formula: str
    slot_fills: Dict[str, str]
    title: str
    source_template: str

    def as_property_entry(self, name: str | None = None) -> Dict[str, Any]:
        """Render as a ``properties.yaml``-style entry usable by ``tm verify``.

        The output shape matches ``tm.verify.spec.load_spec``'s expected
        property entries (``name`` + ``formula``). Useful for piping a
        pattern instance directly into the existing CTL verifier without
        any intermediate ceremony.
        """
        return {
            "name": name or f"{self.pattern_id}:{self.title}",
            "formula": self.resolved_formula,
        }


class PatternInstantiationError(ValueError):
    """Raised when slot fills do not satisfy a pattern's slot contract."""


def _resolve_pattern(
    pattern: PatternEntry | PropertyPatternBody,
) -> PropertyPatternBody:
    if isinstance(pattern, PatternEntry):
        return pattern.body
    return pattern


def _validate_slot_fills(
    pattern: PropertyPatternBody,
    slot_fills: Mapping[str, str],
) -> None:
    declared = {slot.name for slot in pattern.slots}
    provided = set(slot_fills.keys())

    unknown = provided - declared
    if unknown:
        raise PatternInstantiationError(
            f"pattern '{pattern.pattern_id}': unknown slot(s): {sorted(unknown)}; declared slots are {sorted(declared)}"
        )

    required = {slot.name for slot in pattern.slots if slot.required}
    missing = required - provided
    if missing:
        raise PatternInstantiationError(f"pattern '{pattern.pattern_id}': missing required slot(s): {sorted(missing)}")

    for name, value in slot_fills.items():
        if not isinstance(value, str):
            raise PatternInstantiationError(
                f"pattern '{pattern.pattern_id}': slot '{name}' value must be a string, got {type(value).__name__}"
            )
        if not value.strip():
            raise PatternInstantiationError(f"pattern '{pattern.pattern_id}': slot '{name}' value must be non-empty")


def instantiate_pattern(
    pattern: PatternEntry | PropertyPatternBody,
    slot_fills: Mapping[str, str],
    *,
    title: str | None = None,
    validate_formula: bool = True,
) -> PatternInstance:
    """Resolve a pattern template to a concrete CTL formula.

    Args:
        pattern: A loaded pattern (either a :class:`PatternEntry` from the
            library or a raw :class:`PropertyPatternBody`).
        slot_fills: Mapping from slot name to substitution string. Required
            slots must all be present; unknown slots are rejected.
        title: Optional human-readable label for this instance. Defaults to
            the pattern's own title.
        validate_formula: If True (default), parse the resolved formula with
            :func:`tm.verify.ctl.parse_expr` to catch syntax errors early.

    Returns:
        A :class:`PatternInstance` with the resolved formula and provenance.

    Raises:
        PatternInstantiationError: If slot fills are malformed.
        ValueError: If the resolved formula fails CTL parsing (and
            ``validate_formula`` is True).
    """
    body = _resolve_pattern(pattern)
    _validate_slot_fills(body, slot_fills)

    try:
        resolved = body.formula_template.format(**dict(slot_fills))
    except KeyError as exc:
        raise PatternInstantiationError(
            f"pattern '{body.pattern_id}': formula_template references "
            f"undefined slot {exc!s}; this is a pattern-side bug, not a user error"
        ) from exc
    except IndexError as exc:
        raise PatternInstantiationError(
            f"pattern '{body.pattern_id}': formula_template has positional placeholder; "
            "only named slot placeholders are supported"
        ) from exc

    if validate_formula:
        try:
            parse_expr(resolved)
        except Exception as exc:
            raise PatternInstantiationError(
                f"pattern '{body.pattern_id}': resolved formula failed CTL parse: {resolved!r}; {exc}"
            ) from exc

    return PatternInstance(
        pattern_id=body.pattern_id,
        category=body.category,
        resolved_formula=resolved,
        slot_fills=dict(slot_fills),
        title=title or body.title,
        source_template=body.formula_template,
    )


@dataclass
class IntentBuildRequest:
    """All inputs needed to build an :class:`IntentBody` from patterns.

    Bundled as a dataclass so a CLI / form / API can collect them
    uniformly. Every field is plain data — **no LLM provider**.
    """

    intent_id: str
    title: str
    context: str
    goal: str
    instances: Sequence[tuple[str, Mapping[str, str]]]
    """Each tuple is (pattern_id, slot_fills) — order is preserved for trace."""
    non_goals: Sequence[str] = field(default_factory=list)
    actors: Sequence[str] = field(default_factory=list)
    inputs: Sequence[str] = field(default_factory=list)
    outputs: Sequence[str] = field(default_factory=list)
    constraints: Sequence[str] = field(default_factory=list)
    success_metrics: Sequence[str] = field(default_factory=list)
    risks: Sequence[str] = field(default_factory=list)
    assumptions: Sequence[str] = field(default_factory=list)
    parent_intent: str | None = None
    related_intents: Sequence[str] = field(default_factory=list)


def build_intent_from_patterns(
    library: PatternLibrary,
    request: IntentBuildRequest,
) -> tuple[IntentBody, List[PatternInstance]]:
    """Construct a candidate :class:`IntentBody` from a list of pattern instances.

    Each ``(pattern_id, slot_fills)`` entry is looked up in ``library`` and
    fully instantiated via :func:`instantiate_pattern`. The resulting
    IntentBody:

    - Lists all unique pattern_ids in ``property_pattern_refs``
    - Stores slot fills under ``slot_fills[pattern_id]``
    - Has ``trace_links`` populated from ``request.parent_intent`` /
      ``related_intents`` for Governance Rule 4 compliance

    Returns the IntentBody **and** the list of :class:`PatternInstance`
    objects so callers can also emit a ``properties.yaml`` for ``tm verify``.

    Raises:
        KeyError: If any referenced pattern_id is not in the library.
        PatternInstantiationError: If slot fills do not satisfy a pattern.
    """
    instances: List[PatternInstance] = []
    seen_patterns: List[str] = []
    aggregated_slot_fills: Dict[str, Dict[str, Any]] = {}

    for pattern_id, slot_fills in request.instances:
        entry = library.get(pattern_id)
        instance = instantiate_pattern(entry, slot_fills)
        instances.append(instance)
        if pattern_id not in aggregated_slot_fills:
            seen_patterns.append(pattern_id)
            aggregated_slot_fills[pattern_id] = dict(slot_fills)
        else:
            # Multiple instances of the same pattern in one intent — merge,
            # but warn if slot values disagree. The IntentBody schema only
            # stores one slot_fill mapping per pattern_id (Stage 5-1 design).
            existing = aggregated_slot_fills[pattern_id]
            for k, v in slot_fills.items():
                if k in existing and existing[k] != v:
                    raise PatternInstantiationError(
                        f"intent '{request.intent_id}': pattern '{pattern_id}' "
                        f"reused with conflicting slot '{k}' "
                        f"(existing={existing[k]!r}, new={v!r}); "
                        "use distinct pattern instances or unify the slot value"
                    )
                existing[k] = v

    intent_body = IntentBody(
        intent_id=request.intent_id,
        title=request.title,
        context=request.context,
        goal=request.goal,
        non_goals=list(request.non_goals),
        actors=list(request.actors),
        inputs=list(request.inputs),
        outputs=list(request.outputs),
        constraints=list(request.constraints),
        success_metrics=list(request.success_metrics),
        risks=list(request.risks),
        assumptions=list(request.assumptions),
        trace_links=_build_trace_links(request),
        property_pattern_refs=seen_patterns,
        slot_fills=aggregated_slot_fills,
    )
    return intent_body, instances


def _build_trace_links(request: IntentBuildRequest):
    # Local import to keep this module self-contained at the top.
    from tm.artifacts.models import TraceLinks

    return TraceLinks(
        parent_intent=request.parent_intent,
        related_intents=list(request.related_intents),
    )


__all__ = [
    "IntentBuildRequest",
    "PatternInstance",
    "PatternInstantiationError",
    "build_intent_from_patterns",
    "instantiate_pattern",
]
