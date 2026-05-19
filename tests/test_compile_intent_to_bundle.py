"""Tests for ``tm.compile.intent_to_bundle`` (Stage 5-4 task 4.5).

Covers:
- Round-trip from PatternInstance(s) → IntentBody → PlanBody → AgentBundleBody
- All compiled artifacts pass K-Ontology v0.2 governance (``verify``)
- Deterministic, idempotent output
- Coverage check rejects intents whose pattern_refs aren't supplied
- MAPE-K skeleton structure (4 phases, 4 agents, correct wiring)
- Pattern-category → stage routing matches CATEGORY_TO_STAGE
- Unknown pattern category is tolerated (property still emitted)
- Determinism: same input → same plan_id, bundle_id, step names
- Coverage of CompilationResult.as_dict for serialization
"""

from __future__ import annotations

import pytest

from tm.artifacts import (
    Artifact,
    ArtifactEnvelope,
    ArtifactStatus,
    ArtifactType,
    body_hash,
    verify,
)
from tm.compile import (
    CATEGORY_TO_STAGE,
    CompilationResult,
    MAPE_PHASES,
    compile_intent_to_bundle,
)
from tm.compile.intent_to_bundle import (
    _bundle_to_dict,
    _intent_to_dict,
    _plan_to_dict,
)
from tm.patterns import (
    IntentBuildRequest,
    PatternInstance,
    build_intent_from_patterns,
    load_seed_patterns,
)

# ─── Fixtures ─────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def library():
    return load_seed_patterns()


@pytest.fixture
def request_full(library):
    """An IntentBuildRequest covering all 3 seed pattern categories."""
    return IntentBuildRequest(
        intent_id="test.full",
        title="Full coverage",
        context="Compilation test",
        goal="Compile to bundle",
        instances=[
            (
                "safety.no_x_amplifies_y",
                {
                    "forbidden_predicate": "has(quarantined)",
                    "required_predicate": "has(burst_detected)",
                },
            ),
            ("liveness.eventually_x_holds", {"goal_predicate": "has(discoverable)"}),
            (
                "fairness.bounded_x_across_actors",
                {
                    "enforcement_predicate": "has(quarantined)",
                    "mediation_predicate": "done(review)",
                },
            ),
        ],
    )


def _verify(label: str, body, body_raw, artifact_type: ArtifactType) -> None:
    envelope = ArtifactEnvelope(
        artifact_id=f"{label}.test",
        status=ArtifactStatus.CANDIDATE,
        artifact_type=artifact_type,
        version="v0",
        created_by="test",
        created_at="2026-05-18T00:00:00Z",
        body_hash=body_hash(body_raw),
        envelope_hash="",
        meta={},
    )
    candidate = Artifact(envelope=envelope, body=body, body_raw=body_raw)
    accepted, report = verify(candidate)
    assert not report.errors, f"{label} failed governance: {report.errors}"
    assert accepted.envelope.status == ArtifactStatus.ACCEPTED


# ─── Compilation chain ────────────────────────────────────────────


class TestCompileChain:
    def test_returns_compilation_result(self, library, request_full) -> None:
        intent, instances = build_intent_from_patterns(library, request_full)
        result = compile_intent_to_bundle(intent, instances)
        assert isinstance(result, CompilationResult)
        assert result.intent is intent
        assert result.plan is not None
        assert result.bundle is not None

    def test_default_ids_derive_from_intent(self, library, request_full) -> None:
        intent, instances = build_intent_from_patterns(library, request_full)
        result = compile_intent_to_bundle(intent, instances)
        assert result.plan.plan_id == "test.full.plan"
        assert result.bundle.bundle_id == "test.full.bundle"

    def test_explicit_ids_honored(self, library, request_full) -> None:
        intent, instances = build_intent_from_patterns(library, request_full)
        result = compile_intent_to_bundle(
            intent,
            instances,
            plan_id="custom.plan",
            bundle_id="custom.bundle",
        )
        assert result.plan.plan_id == "custom.plan"
        assert result.bundle.bundle_id == "custom.bundle"

    def test_property_specs_emitted(self, library, request_full) -> None:
        intent, instances = build_intent_from_patterns(library, request_full)
        result = compile_intent_to_bundle(intent, instances)
        assert len(result.properties) == 3
        formulas = {p.formula for p in result.properties}
        # Three distinct CTL formulas
        assert len(formulas) == 3
        # Each matches the pattern instance's resolved formula
        instance_formulas = {inst.resolved_formula for inst in instances}
        assert formulas == instance_formulas


# ─── Governance round-trip ────────────────────────────────────────


class TestGovernance:
    def test_compiled_intent_passes_verify(self, library, request_full) -> None:
        intent, instances = build_intent_from_patterns(library, request_full)
        result = compile_intent_to_bundle(intent, instances)
        _verify("intent", result.intent, _intent_to_dict(result.intent), ArtifactType.INTENT)

    def test_compiled_plan_passes_verify(self, library, request_full) -> None:
        intent, instances = build_intent_from_patterns(library, request_full)
        result = compile_intent_to_bundle(intent, instances)
        _verify("plan", result.plan, _plan_to_dict(result.plan), ArtifactType.PLAN)

    def test_compiled_bundle_passes_verify(self, library, request_full) -> None:
        intent, instances = build_intent_from_patterns(library, request_full)
        result = compile_intent_to_bundle(intent, instances)
        _verify("bundle", result.bundle, _bundle_to_dict(result.bundle), ArtifactType.AGENT_BUNDLE)


# ─── MAPE-K skeleton structure ────────────────────────────────────


class TestMapeKSkeleton:
    def test_four_agents_one_per_phase(self, library, request_full) -> None:
        intent, instances = build_intent_from_patterns(library, request_full)
        result = compile_intent_to_bundle(intent, instances)
        roles = [agent.role for agent in result.bundle.agents]
        assert roles == list(MAPE_PHASES)

    def test_plan_has_one_step_per_phase(self, library, request_full) -> None:
        intent, instances = build_intent_from_patterns(library, request_full)
        result = compile_intent_to_bundle(intent, instances)
        names = [step.name for step in result.plan.steps]
        for phase in MAPE_PHASES:
            assert phase in names

    def test_bundle_plan_phase_field_uses_lifecycle_phases(self, library, request_full) -> None:
        """The bundle-plan ``phase`` must be one of ``init/run/emit/finalize``."""
        intent, instances = build_intent_from_patterns(library, request_full)
        result = compile_intent_to_bundle(intent, instances)
        allowed = {"init", "run", "emit", "finalize"}
        for step in result.bundle.plan:
            assert step.phase in allowed

    def test_run_mape_cycle_rule_present(self, library, request_full) -> None:
        intent, instances = build_intent_from_patterns(library, request_full)
        result = compile_intent_to_bundle(intent, instances)
        rule = next(r for r in result.plan.rules if r.name == "run_mape_cycle")
        assert rule.steps == list(MAPE_PHASES)

    def test_observe_reads_intent_ref(self, library, request_full) -> None:
        intent, instances = build_intent_from_patterns(library, request_full)
        result = compile_intent_to_bundle(intent, instances)
        observe = next(s for s in result.plan.steps if s.name == "observe")
        assert observe.reads == ["intent.test.full"]


# ─── Pattern → stage routing ──────────────────────────────────────


class TestPatternRouting:
    def test_safety_routes_to_analyze(self, library) -> None:
        assert CATEGORY_TO_STAGE["safety"] == "analyze"

    def test_liveness_routes_to_decide(self, library) -> None:
        assert CATEGORY_TO_STAGE["liveness"] == "decide"

    def test_fairness_routes_to_act(self, library) -> None:
        assert CATEGORY_TO_STAGE["fairness"] == "act"

    def test_per_pattern_rule_emitted_for_each_instance(self, library, request_full) -> None:
        intent, instances = build_intent_from_patterns(library, request_full)
        result = compile_intent_to_bundle(intent, instances)
        rule_names = {r.name for r in result.plan.rules}
        for inst in instances:
            sanitized = inst.pattern_id.replace(".", "_")
            stage = CATEGORY_TO_STAGE[inst.category]
            expected = f"on_{stage}_check_{sanitized}"
            assert expected in rule_names

    def test_unknown_category_pattern_property_still_emitted(self, library, request_full) -> None:
        intent, instances = build_intent_from_patterns(library, request_full)
        # Inject an instance with an unknown category
        fake = PatternInstance(
            pattern_id="weird.unknown_category",
            category="weird",
            title="Weird pattern",
            resolved_formula="EF foo",
            slot_fills={},
            source_template="EF foo",
        )
        result = compile_intent_to_bundle(
            intent,
            list(instances) + [fake],
        )
        # The unknown-category instance should NOT add a stage rule
        for rule in result.plan.rules:
            assert "weird_unknown_category" not in rule.name
        # But its property must ship
        formulas = {p.formula for p in result.properties}
        assert "EF foo" in formulas


# ─── Coverage check ───────────────────────────────────────────────


class TestCoverageCheck:
    def test_missing_pattern_instance_raises(self, library) -> None:
        req = IntentBuildRequest(
            intent_id="missing.demo",
            title="X",
            context="X",
            goal="X",
            instances=[
                (
                    "safety.no_x_amplifies_y",
                    {
                        "forbidden_predicate": "has(a)",
                        "required_predicate": "has(b)",
                    },
                ),
            ],
        )
        intent, instances = build_intent_from_patterns(library, req)
        # Drop the instance — coverage check should fire
        with pytest.raises(ValueError, match="not covered"):
            compile_intent_to_bundle(intent, [])

    def test_extra_instance_is_allowed(self, library) -> None:
        req = IntentBuildRequest(
            intent_id="extra.demo",
            title="X",
            context="X",
            goal="X",
            instances=[
                (
                    "safety.no_x_amplifies_y",
                    {
                        "forbidden_predicate": "has(a)",
                        "required_predicate": "has(b)",
                    },
                ),
            ],
        )
        intent, instances = build_intent_from_patterns(library, req)
        extra = PatternInstance(
            pattern_id="liveness.eventually_x_holds",
            category="liveness",
            title="Extra liveness",
            resolved_formula="EF has(extra)",
            slot_fills={},
            source_template="EF {goal_predicate}",
        )
        # Should not raise — extras are allowed
        result = compile_intent_to_bundle(intent, list(instances) + [extra])
        assert any(p.formula == "EF has(extra)" for p in result.properties)


# ─── Determinism ──────────────────────────────────────────────────


class TestDeterminism:
    def test_repeated_compilation_yields_equal_dicts(self, library, request_full) -> None:
        intent, instances = build_intent_from_patterns(library, request_full)
        a = compile_intent_to_bundle(intent, instances).as_dict()
        b = compile_intent_to_bundle(intent, instances).as_dict()
        assert a == b

    def test_step_order_deterministic(self, library, request_full) -> None:
        intent, instances = build_intent_from_patterns(library, request_full)
        a = compile_intent_to_bundle(intent, instances)
        b = compile_intent_to_bundle(intent, instances)
        assert [s.name for s in a.plan.steps] == [s.name for s in b.plan.steps]
        assert [r.name for r in a.plan.rules] == [r.name for r in b.plan.rules]


# ─── Serialization ────────────────────────────────────────────────


class TestSerialization:
    def test_as_dict_contains_all_sections(self, library, request_full) -> None:
        intent, instances = build_intent_from_patterns(library, request_full)
        result = compile_intent_to_bundle(intent, instances)
        d = result.as_dict()
        assert set(d.keys()) >= {"intent", "plan", "bundle", "properties"}
        assert d["intent"]["intent_id"] == "test.full"
        assert d["plan"]["plan_id"] == "test.full.plan"
        assert d["bundle"]["bundle_id"] == "test.full.bundle"
        assert len(d["properties"]) == 3

    def test_as_dict_is_json_serializable(self, library, request_full) -> None:
        import json

        intent, instances = build_intent_from_patterns(library, request_full)
        result = compile_intent_to_bundle(intent, instances)
        text = json.dumps(result.as_dict(), sort_keys=True)
        # Round-trip
        assert json.loads(text)["plan"]["plan_id"] == "test.full.plan"


# ─── Runtime overrides hook ───────────────────────────────────────


class TestRuntimeOverrides:
    def test_custom_runtime_kind_propagates_to_agents(self, library, request_full) -> None:
        intent, instances = build_intent_from_patterns(library, request_full)
        result = compile_intent_to_bundle(
            intent,
            instances,
            runtime_kind="custom-runtime",
        )
        for agent in result.bundle.agents:
            assert agent.spec.runtime.kind == "custom-runtime"

    def test_custom_runtime_config_merges_into_agents(self, library, request_full) -> None:
        intent, instances = build_intent_from_patterns(library, request_full)
        result = compile_intent_to_bundle(
            intent,
            instances,
            runtime_config={"endpoint": "https://example.test"},
        )
        for agent in result.bundle.agents:
            assert agent.spec.runtime.config["endpoint"] == "https://example.test"
            assert "phase" in agent.spec.runtime.config
            assert "intent_id" in agent.spec.runtime.config
