"""Conflict detection and classification for multi-demand plan merging.

When two independent control scenarios produce plans that touch the same
actuator, this module classifies the conflict type so an arbiter / composer
can apply appropriate resolution rules.

The classifier is **domain-neutral**: callers supply ``intent_categories``
(a mapping ``intent_id -> category``) and optional ``numeric_keys`` that
describe which fields in ``target_state`` should be treated as opposing
scalar magnitudes. ``PRIORITY_ORDER`` is a stable default vocabulary;
callers may override the priority levels indirectly by mapping their
intents to one of these categories.

This module was extracted to ``tm.policy`` in Phase 5 Stage 5-2 from the
``fablenet-control/arbiter/conflict.py`` original (audited as FULLY_GENERIC).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping, Sequence


class ConflictKind(str, Enum):
    SAME_ACTUATOR_COMPATIBLE = "same_actuator_compatible"
    SAME_ACTUATOR_OPPOSITE = "same_actuator_opposite_direction"
    SAFETY_VS_EFFICIENCY = "safety_vs_efficiency"
    LIVENESS_VS_LIVENESS = "liveness_vs_liveness"


PRIORITY_ORDER = {
    "safety": 100,
    "liveness": 50,
    "efficiency": 10,
    "fairness": 40,
}


@dataclass(frozen=True)
class Conflict:
    """A detected conflict between two decisions on the same actuator."""

    actuator: str
    kind: ConflictKind
    decision_a: Mapping[str, Any]
    decision_b: Mapping[str, Any]
    intent_a: str
    intent_b: str
    description: str


class ConflictClassifier:
    """Classifies conflicts between decisions from different plans.

    Parameters
    ----------
    intent_categories:
        Maps intent IDs to categories like ``"safety"``, ``"liveness"``,
        ``"efficiency"``, ``"fairness"`` for priority-based resolution.
    numeric_keys:
        Field names inside ``decision.target_state`` that are treated as
        opposing scalar magnitudes. Defaults to a generic control-plane
        vocabulary; override for domain-specific actuators.
    """

    _DEFAULT_NUMERIC_KEYS: tuple[str, ...] = (
        "reject_rate",
        "qps",
        "burst",
        "queue_depth",
        "latency_p95_ms",
    )

    def __init__(
        self,
        intent_categories: Mapping[str, str] | None = None,
        numeric_keys: Sequence[str] | None = None,
    ):
        self._categories: dict[str, str] = dict(intent_categories or {})
        self._numeric_keys: set[str] = set(numeric_keys or self._DEFAULT_NUMERIC_KEYS)

    def get_category(self, intent_id: str) -> str:
        """Public accessor for the category mapped to ``intent_id``.

        Returns the empty string when no category is registered. Added in
        Stage 5-2 to replace direct ``_categories`` access from callers
        (e.g. ``PlanComposer``). Composer-style callers should rely on
        this method instead of reaching into the private dict.
        """
        return self._categories.get(intent_id, "")

    def categories(self) -> Mapping[str, str]:
        """Read-only view of the full intent-to-category map."""
        return dict(self._categories)

    def classify(
        self,
        actuator: str,
        decision_a: Mapping[str, Any],
        decision_b: Mapping[str, Any],
        intent_a: str,
        intent_b: str,
    ) -> Conflict:
        cat_a = self._categories.get(intent_a, "")
        cat_b = self._categories.get(intent_b, "")

        if cat_a and cat_b and cat_a != cat_b:
            prio_a = PRIORITY_ORDER.get(cat_a, 0)
            prio_b = PRIORITY_ORDER.get(cat_b, 0)
            if (prio_a >= PRIORITY_ORDER["safety"]) != (prio_b >= PRIORITY_ORDER["safety"]):
                kind = ConflictKind.SAFETY_VS_EFFICIENCY
            elif cat_a == "liveness" and cat_b == "liveness":
                kind = ConflictKind.LIVENESS_VS_LIVENESS
            else:
                kind = self._classify_direction(decision_a, decision_b)
        elif cat_a == "liveness" and cat_b == "liveness":
            kind = ConflictKind.LIVENESS_VS_LIVENESS
        else:
            kind = self._classify_direction(decision_a, decision_b)

        desc = f"{actuator}: {kind.value} between {intent_a}({cat_a or '?'}) and {intent_b}({cat_b or '?'})"

        return Conflict(
            actuator=actuator,
            kind=kind,
            decision_a=decision_a,
            decision_b=decision_b,
            intent_a=intent_a,
            intent_b=intent_b,
            description=desc,
        )

    def _classify_direction(
        self,
        a: Mapping[str, Any],
        b: Mapping[str, Any],
    ) -> ConflictKind:
        state_a = a.get("target_state", {})
        state_b = b.get("target_state", {})
        if not isinstance(state_a, Mapping) or not isinstance(state_b, Mapping):
            return ConflictKind.SAME_ACTUATOR_OPPOSITE

        opposing = False
        for key in self._numeric_keys:
            va = state_a.get(key)
            vb = state_b.get(key)
            if va is not None and vb is not None:
                try:
                    diff = float(va) - float(vb)
                    if abs(diff) > 1e-9:
                        opposing = True
                        break
                except (ValueError, TypeError):
                    pass

        if opposing:
            return ConflictKind.SAME_ACTUATOR_OPPOSITE
        return ConflictKind.SAME_ACTUATOR_COMPATIBLE


__all__ = ["Conflict", "ConflictClassifier", "ConflictKind", "PRIORITY_ORDER"]
