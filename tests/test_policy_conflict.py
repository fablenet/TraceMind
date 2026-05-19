"""Tests for ``tm.policy.conflict`` extracted from ``fablenet-control/arbiter``.

Locks the public surface: ``Conflict``, ``ConflictClassifier``, ``ConflictKind``,
``PRIORITY_ORDER``, and the new public ``get_category`` / ``categories``
accessors that replace previous private ``_categories`` access from composer
callers.
"""

from __future__ import annotations

from tm.policy import (
    PRIORITY_ORDER,
    Conflict,
    ConflictClassifier,
    ConflictKind,
)


class TestConflictKindClassification:
    def test_safety_vs_efficiency_uses_priority_gap(self) -> None:
        classifier = ConflictClassifier(intent_categories={"safe": "safety", "fast": "efficiency"})
        conflict = classifier.classify(
            actuator="api.gateway",
            decision_a={"target_state": {"reject_rate": 0.9}},
            decision_b={"target_state": {"reject_rate": 0.1}},
            intent_a="safe",
            intent_b="fast",
        )
        assert isinstance(conflict, Conflict)
        assert conflict.kind == ConflictKind.SAFETY_VS_EFFICIENCY

    def test_liveness_vs_liveness(self) -> None:
        classifier = ConflictClassifier(intent_categories={"A": "liveness", "B": "liveness"})
        conflict = classifier.classify(
            actuator="api.gateway",
            decision_a={"target_state": {"qps": 100}},
            decision_b={"target_state": {"qps": 200}},
            intent_a="A",
            intent_b="B",
        )
        assert conflict.kind == ConflictKind.LIVENESS_VS_LIVENESS

    def test_same_actuator_compatible_when_numeric_match(self) -> None:
        classifier = ConflictClassifier(intent_categories={"A": "efficiency", "B": "efficiency"})
        conflict = classifier.classify(
            actuator="api.gateway",
            decision_a={"target_state": {"qps": 100}},
            decision_b={"target_state": {"qps": 100}},
            intent_a="A",
            intent_b="B",
        )
        assert conflict.kind == ConflictKind.SAME_ACTUATOR_COMPATIBLE

    def test_same_actuator_opposite_when_numeric_differs(self) -> None:
        classifier = ConflictClassifier(intent_categories={"A": "efficiency", "B": "efficiency"})
        conflict = classifier.classify(
            actuator="api.gateway",
            decision_a={"target_state": {"qps": 100}},
            decision_b={"target_state": {"qps": 200}},
            intent_a="A",
            intent_b="B",
        )
        assert conflict.kind == ConflictKind.SAME_ACTUATOR_OPPOSITE

    def test_unknown_category_falls_back_to_direction(self) -> None:
        classifier = ConflictClassifier(intent_categories={})
        conflict = classifier.classify(
            actuator="api.gateway",
            decision_a={"target_state": {"qps": 100}},
            decision_b={"target_state": {"qps": 100}},
            intent_a="A",
            intent_b="B",
        )
        assert conflict.kind == ConflictKind.SAME_ACTUATOR_COMPATIBLE


class TestPublicCategoryAPI:
    def test_get_category_returns_registered(self) -> None:
        classifier = ConflictClassifier(intent_categories={"a": "safety"})
        assert classifier.get_category("a") == "safety"

    def test_get_category_returns_empty_for_unknown(self) -> None:
        classifier = ConflictClassifier()
        assert classifier.get_category("unknown") == ""

    def test_categories_view_is_read_only_copy(self) -> None:
        classifier = ConflictClassifier(intent_categories={"a": "safety"})
        view = classifier.categories()
        view["a"] = "tampered"
        assert classifier.get_category("a") == "safety"


class TestNumericKeysCustomization:
    def test_custom_numeric_keys(self) -> None:
        classifier = ConflictClassifier(
            intent_categories={"A": "efficiency", "B": "efficiency"},
            numeric_keys=["custom_metric"],
        )
        conflict = classifier.classify(
            actuator="my.actuator",
            decision_a={"target_state": {"custom_metric": 1}},
            decision_b={"target_state": {"custom_metric": 2}},
            intent_a="A",
            intent_b="B",
        )
        assert conflict.kind == ConflictKind.SAME_ACTUATOR_OPPOSITE

    def test_default_numeric_keys_recognized(self) -> None:
        classifier = ConflictClassifier(intent_categories={"A": "efficiency", "B": "efficiency"})
        for key in ("reject_rate", "qps", "burst", "queue_depth", "latency_p95_ms"):
            conflict = classifier.classify(
                actuator="x",
                decision_a={"target_state": {key: 1}},
                decision_b={"target_state": {key: 2}},
                intent_a="A",
                intent_b="B",
            )
            assert conflict.kind == ConflictKind.SAME_ACTUATOR_OPPOSITE, key


class TestPriorityOrder:
    def test_safety_highest(self) -> None:
        assert PRIORITY_ORDER["safety"] > PRIORITY_ORDER["liveness"]
        assert PRIORITY_ORDER["safety"] > PRIORITY_ORDER["efficiency"]
        assert PRIORITY_ORDER["safety"] > PRIORITY_ORDER["fairness"]
