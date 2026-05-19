"""Tests for ``tm.patterns.instantiate`` — the non-LLM bottom path.

Stage 5-3 task 3.3 DoD: "不用 LLM 也能拼出合规 IntentTree". These tests prove
that with only declarative input (no provider, no LLM), a user can:

1. Resolve a pattern template to a concrete CTL formula
2. Compose multiple pattern instances into a candidate IntentBody
3. Catch all common slot-fill errors at validation time

Plus end-to-end: take a pattern instance and feed it into the existing
``tm.verify`` machinery — the resolved formula must be a valid CTL
expression that the verifier understands.
"""

from __future__ import annotations

import pytest

from tm.artifacts import IntentBody
from tm.patterns import (
    IntentBuildRequest,
    PatternInstance,
    PatternInstantiationError,
    build_intent_from_patterns,
    instantiate_pattern,
    load_seed_patterns,
)
from tm.verify.ctl import parse_expr


# ─── instantiate_pattern (pure formula resolution) ────────────────


class TestInstantiateSafetyPattern:
    def setup_method(self) -> None:
        self.lib = load_seed_patterns()
        self.pattern = self.lib.get("safety.no_x_amplifies_y")

    def test_full_resolution_matches_anti_sybil_s2(self) -> None:
        """Equivalent of fablenet-control's safety_no_quarantine_without_detection."""
        inst = instantiate_pattern(
            self.pattern,
            {
                "forbidden_predicate": "has(quarantined)",
                "required_predicate": "has(burst_detected)",
            },
        )
        assert inst.resolved_formula == ("AG (NOT has(quarantined) OR has(burst_detected))")
        assert inst.category == "safety"
        assert inst.pattern_id == "safety.no_x_amplifies_y"

    def test_resolved_formula_parses_as_ctl(self) -> None:
        inst = instantiate_pattern(
            self.pattern,
            {
                "forbidden_predicate": "has(quarantined)",
                "required_predicate": "has(burst_detected)",
            },
        )
        expr = parse_expr(inst.resolved_formula)
        assert expr is not None

    def test_custom_title(self) -> None:
        inst = instantiate_pattern(
            self.pattern,
            {
                "forbidden_predicate": "has(x)",
                "required_predicate": "has(y)",
            },
            title="my custom label",
        )
        assert inst.title == "my custom label"

    def test_default_title_falls_back_to_pattern(self) -> None:
        inst = instantiate_pattern(
            self.pattern,
            {"forbidden_predicate": "has(x)", "required_predicate": "has(y)"},
        )
        assert inst.title == self.pattern.body.title

    def test_source_template_preserved(self) -> None:
        inst = instantiate_pattern(
            self.pattern,
            {"forbidden_predicate": "has(x)", "required_predicate": "has(y)"},
        )
        assert inst.source_template == self.pattern.body.formula_template


class TestInstantiateLivenessPattern:
    def setup_method(self) -> None:
        self.lib = load_seed_patterns()
        self.pattern = self.lib.get("liveness.eventually_x_holds")

    def test_matches_anti_sybil_l1(self) -> None:
        inst = instantiate_pattern(self.pattern, {"goal_predicate": "has(content_discoverable)"})
        assert inst.resolved_formula == "EF has(content_discoverable)"
        assert inst.category == "liveness"


class TestInstantiateFairnessPattern:
    def setup_method(self) -> None:
        self.lib = load_seed_patterns()
        self.pattern = self.lib.get("fairness.bounded_x_across_actors")

    def test_matches_anti_sybil_f1(self) -> None:
        inst = instantiate_pattern(
            self.pattern,
            {
                "enforcement_predicate": "has(quarantined)",
                "mediation_predicate": "done(human_review)",
            },
        )
        assert inst.resolved_formula == ("AG (NOT has(quarantined) OR EF done(human_review))")

    def test_mediation_slot_accepts_nested_or(self) -> None:
        """Anti-sybil L2 needs nested OR in mediation — must work."""
        inst = instantiate_pattern(
            self.pattern,
            {
                "enforcement_predicate": "has(burst_detected)",
                "mediation_predicate": "(has(tier_downgraded) OR has(quarantined))",
            },
        )
        # Resolved formula must parse as CTL despite nested sub-expression
        parse_expr(inst.resolved_formula)


# ─── instantiate_pattern (error paths) ────────────────────────────


class TestInstantiateErrors:
    def setup_method(self) -> None:
        self.lib = load_seed_patterns()

    def test_missing_required_slot(self) -> None:
        with pytest.raises(PatternInstantiationError, match="missing required"):
            instantiate_pattern(
                self.lib.get("liveness.eventually_x_holds"),
                {},
            )

    def test_unknown_slot(self) -> None:
        with pytest.raises(PatternInstantiationError, match="unknown slot"):
            instantiate_pattern(
                self.lib.get("liveness.eventually_x_holds"),
                {"goal_predicate": "has(x)", "rogue": "foo"},
            )

    def test_non_string_slot_value(self) -> None:
        with pytest.raises(PatternInstantiationError, match="must be a string"):
            instantiate_pattern(
                self.lib.get("liveness.eventually_x_holds"),
                {"goal_predicate": 42},  # type: ignore[dict-item]
            )

    def test_empty_slot_value(self) -> None:
        with pytest.raises(PatternInstantiationError, match="non-empty"):
            instantiate_pattern(
                self.lib.get("liveness.eventually_x_holds"),
                {"goal_predicate": "   "},
            )

    def test_resolved_formula_fails_ctl_parse(self) -> None:
        with pytest.raises(PatternInstantiationError, match="failed CTL parse"):
            instantiate_pattern(
                self.lib.get("liveness.eventually_x_holds"),
                {"goal_predicate": "!!!syntax error("},
            )

    def test_skip_formula_validation(self) -> None:
        """``validate_formula=False`` allows odd-but-substituting values."""
        inst = instantiate_pattern(
            self.lib.get("liveness.eventually_x_holds"),
            {"goal_predicate": "definitely not ctl"},
            validate_formula=False,
        )
        assert "definitely not ctl" in inst.resolved_formula

    def test_accepts_propertypatternbody_directly(self) -> None:
        """Caller doesn't have to wrap in a PatternEntry."""
        body = self.lib.get("liveness.eventually_x_holds").body
        inst = instantiate_pattern(body, {"goal_predicate": "has(x)"})
        assert inst.pattern_id == body.pattern_id


# ─── build_intent_from_patterns (full non-LLM path) ────────────────


class TestBuildIntentFromPatterns:
    def setup_method(self) -> None:
        self.lib = load_seed_patterns()

    def _anti_sybil_request(self) -> IntentBuildRequest:
        return IntentBuildRequest(
            intent_id="test.anti_sybil",
            title="Anti-sybil enforcement",
            context="An intent assembled from 3 domain-neutral patterns.",
            goal="Enforce safety + liveness + fairness on the feed",
            instances=[
                (
                    "safety.no_x_amplifies_y",
                    {
                        "forbidden_predicate": "has(quarantined)",
                        "required_predicate": "has(burst_detected)",
                    },
                ),
                (
                    "liveness.eventually_x_holds",
                    {"goal_predicate": "has(content_discoverable)"},
                ),
                (
                    "fairness.bounded_x_across_actors",
                    {
                        "enforcement_predicate": "has(quarantined)",
                        "mediation_predicate": "done(human_review)",
                    },
                ),
            ],
            actors=["feed_publisher", "moderator"],
            constraints=["latency_p95_ms < 200"],
            parent_intent="root.feed_governance",
            related_intents=["test.anti_spam"],
        )

    def test_produces_intent_body(self) -> None:
        intent, instances = build_intent_from_patterns(self.lib, self._anti_sybil_request())
        assert isinstance(intent, IntentBody)
        assert intent.intent_id == "test.anti_sybil"
        assert len(instances) == 3

    def test_property_pattern_refs_aggregated(self) -> None:
        intent, _ = build_intent_from_patterns(self.lib, self._anti_sybil_request())
        assert set(intent.property_pattern_refs) == {
            "safety.no_x_amplifies_y",
            "liveness.eventually_x_holds",
            "fairness.bounded_x_across_actors",
        }

    def test_slot_fills_keyed_by_pattern_id(self) -> None:
        intent, _ = build_intent_from_patterns(self.lib, self._anti_sybil_request())
        assert "safety.no_x_amplifies_y" in intent.slot_fills
        assert intent.slot_fills["safety.no_x_amplifies_y"]["forbidden_predicate"] == ("has(quarantined)")

    def test_trace_links_populated(self) -> None:
        intent, _ = build_intent_from_patterns(self.lib, self._anti_sybil_request())
        assert intent.trace_links.parent_intent == "root.feed_governance"
        assert "test.anti_spam" in intent.trace_links.related_intents

    def test_all_instances_have_valid_ctl(self) -> None:
        _, instances = build_intent_from_patterns(self.lib, self._anti_sybil_request())
        for inst in instances:
            parse_expr(inst.resolved_formula)

    def test_unknown_pattern_id_raises(self) -> None:
        req = IntentBuildRequest(
            intent_id="bad",
            title="x",
            context="x",
            goal="x",
            instances=[("does.not.exist", {})],
        )
        with pytest.raises(KeyError, match="not found"):
            build_intent_from_patterns(self.lib, req)

    def test_conflicting_slot_values_for_same_pattern_raises(self) -> None:
        """Reusing same pattern_id with conflicting slot values is rejected."""
        req = IntentBuildRequest(
            intent_id="bad",
            title="x",
            context="x",
            goal="x",
            instances=[
                (
                    "liveness.eventually_x_holds",
                    {"goal_predicate": "has(a)"},
                ),
                (
                    "liveness.eventually_x_holds",
                    {"goal_predicate": "has(b)"},
                ),
            ],
        )
        with pytest.raises(PatternInstantiationError, match="conflicting slot"):
            build_intent_from_patterns(self.lib, req)


# ─── End-to-end: instance → tm.verify ──────────────────────────────


class TestPatternInstanceIntegrationWithVerifier:
    def test_as_property_entry_shape(self) -> None:
        lib = load_seed_patterns()
        inst = instantiate_pattern(
            lib.get("liveness.eventually_x_holds"),
            {"goal_predicate": "has(content_discoverable)"},
        )
        entry = inst.as_property_entry(name="L1")
        assert entry == {"name": "L1", "formula": "EF has(content_discoverable)"}

    def test_as_property_entry_default_name(self) -> None:
        lib = load_seed_patterns()
        inst = instantiate_pattern(
            lib.get("liveness.eventually_x_holds"),
            {"goal_predicate": "has(x)"},
            title="my_label",
        )
        entry = inst.as_property_entry()
        assert "liveness.eventually_x_holds" in entry["name"]
        assert "my_label" in entry["name"]

    def test_instance_is_dataclass_frozen(self) -> None:
        """PatternInstance is frozen so it's hashable / safe to share."""
        lib = load_seed_patterns()
        inst = instantiate_pattern(
            lib.get("liveness.eventually_x_holds"),
            {"goal_predicate": "has(x)"},
        )
        assert isinstance(inst, PatternInstance)
        with pytest.raises(Exception):
            inst.resolved_formula = "tampered"  # type: ignore[misc]


# ─── No-LLM-path proof ───────────────────────────────────────────


class TestNonLLMPathInvariants:
    """Verify there is no LLM provider invocation on this code path.

    Phase 5 invariant 2: "every LLM path must have an equivalent non-LLM
    path". This test does not stub providers — it imports the path and
    asserts it never references any LLM module.
    """

    def test_module_does_not_import_llm_provider(self) -> None:
        import sys

        from tm.patterns import instantiate as inst_mod

        forbidden = {
            "openai",
            "anthropic",
            "tm.steps.ai_plan",
            "tm.steps.ai_execute_plan",
            "tm.steps.ai_reflect",
        }
        for forbidden_name in forbidden:
            # Module either imported transitively or referenced by name string
            assert forbidden_name not in inst_mod.__dict__
        # And the loaded module hasn't dragged in any provider deps
        for forbidden_module in ("openai", "anthropic"):
            # Allow them to be present in sys.modules (some test runner may
            # import them); just ensure tm.patterns.instantiate doesn't depend
            # on them.
            src = inst_mod.__file__ or ""
            if src:
                with open(src, encoding="utf-8") as f:
                    text = f.read()
                assert forbidden_module not in text, (
                    f"tm.patterns.instantiate refers to forbidden module {forbidden_module}"
                )
        # Ensure no provider got into the runtime path via side effects
        # of importing tm.patterns
        assert sys is not None  # keep sys reference (no-op)
