"""Tests for ``tm.kb.case_corpus`` (Stage 5-4 task 4.2).

Covers:
- Case aggregation by intent_id (primary index)
- Pattern → cases secondary index
- Evidence extraction from ProofReport / EscalationReport / ProposedChangePlan
- Unattached evidence surfaces (e.g. report whose intent isn't in registry)
- has_failures() flag
- Empty registry / missing-file robustness
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

import pytest
import yaml

from tm.artifacts import (
    Artifact,
    ArtifactEnvelope,
    ArtifactStatus,
    ArtifactType,
    EscalationReportBody,
    IntentBody,
    ProofReportBody,
    ProposedChangePlanBody,
    body_hash,
)
from tm.artifacts.registry import ArtifactRegistry
from tm.artifacts.storage import RegistryStorage
from tm.kb import build_case_corpus

# ─── Fixture helpers ──────────────────────────────────────────────


def _write_yaml(path: Path, payload: Mapping[str, Any]) -> None:
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")


def _envelope(artifact_id: str, artifact_type: ArtifactType, body_raw: Mapping[str, Any]) -> ArtifactEnvelope:
    return ArtifactEnvelope(
        artifact_id=artifact_id,
        status=ArtifactStatus.ACCEPTED,
        artifact_type=artifact_type,
        version="v0",
        created_by="test",
        created_at="2026-05-18T00:00:00Z",
        body_hash=body_hash(body_raw),
        envelope_hash="",
        meta={},
    )


def _register(
    registry: ArtifactRegistry,
    tmp_path: Path,
    artifact_id: str,
    artifact_type: ArtifactType,
    body_raw: Mapping[str, Any],
    body_cls,
) -> Path:
    body = body_cls.from_mapping(body_raw)
    envelope = _envelope(artifact_id, artifact_type, body_raw)
    artifact = Artifact(envelope=envelope, body=body, body_raw=body_raw)
    path = tmp_path / f"{artifact_id.replace('.', '_')}.yaml"
    _write_yaml(
        path,
        {
            "envelope": {
                "artifact_id": envelope.artifact_id,
                "status": envelope.status.value,
                "artifact_type": envelope.artifact_type.value,
                "version": envelope.version,
                "created_by": envelope.created_by,
                "created_at": envelope.created_at,
                "body_hash": envelope.body_hash,
                "envelope_hash": envelope.envelope_hash,
                "meta": dict(envelope.meta),
            },
            "body": dict(body_raw),
        },
    )
    registry.add(artifact, str(path))
    return path


def _intent_body(intent_id: str = "demo.intent", pattern_refs: list[str] | None = None) -> dict[str, Any]:
    return {
        "intent_id": intent_id,
        "title": f"Intent {intent_id}",
        "context": "test fixture",
        "goal": "fixture goal",
        "property_pattern_refs": pattern_refs or [],
        "slot_fills": {},
    }


def _proof_report_body(
    report_id: str,
    intent_id: str,
    verdict: str = "pass",
    *,
    counterexample_event: bool = False,
) -> dict[str, Any]:
    chain: list[dict[str, Any]] = [
        {"source": "kripke.verifier", "event_type": "invariant_check", "data": {"count": 5}},
    ]
    if counterexample_event:
        chain.append(
            {
                "source": "kripke.verifier",
                "event_type": "counterexample.found",
                "data": {"trace": ["s0", "s1", "s2"]},
            }
        )
    return {
        "report_id": report_id,
        "intent_id": intent_id,
        "cycle_id": f"cycle.{report_id}",
        "overall_verdict": verdict,
        "evidence_chain": chain,
    }


def _escalation_body(report_id: str, intent_ref: str, severity: str = "warning") -> dict[str, Any]:
    return {
        "report_id": report_id,
        "timestamp": "2026-05-18T00:00:02Z",
        "severity": severity,
        "intent_ref": intent_ref,
        "recent_rules_fired": ["rule.a", "rule.b"],
        "suggested_actions": ["tune.threshold"],
        "counterexample": {"trace": ["s0", "s1"]},
    }


def _proposal_body(plan_id: str, intent_id: str) -> dict[str, Any]:
    return {
        "plan_id": plan_id,
        "intent_id": intent_id,
        "summary": f"proposal {plan_id} for {intent_id}",
        "decisions": [
            {
                "effect_ref": "tm/some/target.yaml",
                "target_state": {"operation": "update"},
                "idempotency_key": f"{plan_id}.v1",
            }
        ],
        "llm_metadata": {
            "model": "none",
            "prompt_hash": "n/a",
            "determinism_hint": "deterministic",
        },
        "policy_requirements": ["all existing tests pass"],
    }


@pytest.fixture
def registry(tmp_path: Path) -> ArtifactRegistry:
    return ArtifactRegistry(storage=RegistryStorage(tmp_path / "registry.jsonl"))


# ─── Basic aggregation ────────────────────────────────────────────


class TestCaseCorpusBasicAggregation:
    def test_empty_registry_produces_empty_corpus(self, registry: ArtifactRegistry) -> None:
        corpus = build_case_corpus(registry)
        assert corpus.cases() == []
        assert dict(corpus.by_intent_id) == {}
        assert dict(corpus.by_pattern_id) == {}

    def test_single_intent_creates_one_case(self, registry: ArtifactRegistry, tmp_path: Path) -> None:
        _register(
            registry,
            tmp_path,
            "intent.demo",
            ArtifactType.INTENT,
            _intent_body("demo.intent", pattern_refs=["safety.no_x_amplifies_y"]),
            IntentBody,
        )
        corpus = build_case_corpus(registry)
        cases = corpus.cases()
        assert len(cases) == 1
        case = cases[0]
        assert case.intent_id == "demo.intent"
        assert case.intent_ref == "intent.demo"
        assert case.pattern_refs == ["safety.no_x_amplifies_y"]

    def test_intent_with_multiple_patterns(self, registry: ArtifactRegistry, tmp_path: Path) -> None:
        _register(
            registry,
            tmp_path,
            "intent.multi",
            ArtifactType.INTENT,
            _intent_body(
                "multi.intent",
                pattern_refs=[
                    "safety.no_x_amplifies_y",
                    "liveness.eventually_x_holds",
                    "fairness.bounded_x_across_actors",
                ],
            ),
            IntentBody,
        )
        corpus = build_case_corpus(registry)
        case = corpus.case_for_intent("multi.intent")
        assert case is not None
        assert len(case.pattern_refs) == 3

    def test_multiple_intents_produce_multiple_cases(self, registry: ArtifactRegistry, tmp_path: Path) -> None:
        _register(registry, tmp_path, "i1", ArtifactType.INTENT, _intent_body("intent.a"), IntentBody)
        _register(registry, tmp_path, "i2", ArtifactType.INTENT, _intent_body("intent.b"), IntentBody)
        corpus = build_case_corpus(registry)
        assert {c.intent_id for c in corpus.cases()} == {"intent.a", "intent.b"}


# ─── Evidence handlers ────────────────────────────────────────────


class TestCaseCorpusEvidence:
    def test_proof_report_attaches_to_intent(self, registry: ArtifactRegistry, tmp_path: Path) -> None:
        _register(registry, tmp_path, "i1", ArtifactType.INTENT, _intent_body("intent.a"), IntentBody)
        _register(
            registry,
            tmp_path,
            "pr.1",
            ArtifactType.PROOF_REPORT,
            _proof_report_body("pr.1", "intent.a"),
            ProofReportBody,
        )
        corpus = build_case_corpus(registry)
        case = corpus.case_for_intent("intent.a")
        assert case is not None
        proof_evs = case.evidence_of_kind("proof_report")
        assert len(proof_evs) == 1
        assert proof_evs[0].details["overall_verdict"] == "pass"
        assert proof_evs[0].ref == "pr.1"

    def test_proof_report_extracts_counterexamples(self, registry: ArtifactRegistry, tmp_path: Path) -> None:
        _register(registry, tmp_path, "i1", ArtifactType.INTENT, _intent_body("intent.a"), IntentBody)
        _register(
            registry,
            tmp_path,
            "pr.1",
            ArtifactType.PROOF_REPORT,
            _proof_report_body("pr.1", "intent.a", verdict="fail", counterexample_event=True),
            ProofReportBody,
        )
        corpus = build_case_corpus(registry)
        case = corpus.case_for_intent("intent.a")
        assert case is not None
        ev = case.evidence_of_kind("proof_report")[0]
        cxs = ev.details["counterexamples"]
        assert len(cxs) == 1
        assert "trace" in cxs[0]["data"]

    def test_escalation_report_attaches_via_intent_ref(self, registry: ArtifactRegistry, tmp_path: Path) -> None:
        _register(registry, tmp_path, "i1", ArtifactType.INTENT, _intent_body("intent.a"), IntentBody)
        _register(
            registry,
            tmp_path,
            "esc.1",
            ArtifactType.ESCALATION_REPORT,
            _escalation_body("esc.1", "intent.a", severity="critical"),
            EscalationReportBody,
        )
        corpus = build_case_corpus(registry)
        case = corpus.case_for_intent("intent.a")
        assert case is not None
        esc_evs = case.evidence_of_kind("escalation_report")
        assert len(esc_evs) == 1
        assert esc_evs[0].details["severity"] == "critical"
        assert esc_evs[0].details.get("counterexample") is not None

    def test_proposal_attaches(self, registry: ArtifactRegistry, tmp_path: Path) -> None:
        _register(registry, tmp_path, "i1", ArtifactType.INTENT, _intent_body("intent.a"), IntentBody)
        _register(
            registry,
            tmp_path,
            "pcp.1",
            ArtifactType.PROPOSED_CHANGE_PLAN,
            _proposal_body("pcp.1", "intent.a"),
            ProposedChangePlanBody,
        )
        corpus = build_case_corpus(registry)
        case = corpus.case_for_intent("intent.a")
        assert case is not None
        proposals = case.evidence_of_kind("proposed_change_plan")
        assert len(proposals) == 1
        assert "decisions" in proposals[0].details

    def test_multiple_evidence_pieces_aggregate(self, registry: ArtifactRegistry, tmp_path: Path) -> None:
        _register(registry, tmp_path, "i1", ArtifactType.INTENT, _intent_body("intent.a"), IntentBody)
        _register(
            registry,
            tmp_path,
            "pr.1",
            ArtifactType.PROOF_REPORT,
            _proof_report_body("pr.1", "intent.a"),
            ProofReportBody,
        )
        _register(
            registry,
            tmp_path,
            "pr.2",
            ArtifactType.PROOF_REPORT,
            _proof_report_body("pr.2", "intent.a", verdict="fail"),
            ProofReportBody,
        )
        _register(
            registry,
            tmp_path,
            "esc.1",
            ArtifactType.ESCALATION_REPORT,
            _escalation_body("esc.1", "intent.a"),
            EscalationReportBody,
        )
        corpus = build_case_corpus(registry)
        case = corpus.case_for_intent("intent.a")
        assert case is not None
        assert len(case.evidence_of_kind("proof_report")) == 2
        assert len(case.evidence_of_kind("escalation_report")) == 1


# ─── Failure detection ────────────────────────────────────────────


class TestCaseFailureDetection:
    def test_has_failures_false_when_clean(self, registry: ArtifactRegistry, tmp_path: Path) -> None:
        _register(registry, tmp_path, "i1", ArtifactType.INTENT, _intent_body("intent.a"), IntentBody)
        _register(
            registry,
            tmp_path,
            "pr.1",
            ArtifactType.PROOF_REPORT,
            _proof_report_body("pr.1", "intent.a", verdict="pass"),
            ProofReportBody,
        )
        corpus = build_case_corpus(registry)
        case = corpus.case_for_intent("intent.a")
        assert case is not None
        assert case.has_failures() is False

    def test_has_failures_true_on_failed_proof(self, registry: ArtifactRegistry, tmp_path: Path) -> None:
        _register(registry, tmp_path, "i1", ArtifactType.INTENT, _intent_body("intent.a"), IntentBody)
        _register(
            registry,
            tmp_path,
            "pr.1",
            ArtifactType.PROOF_REPORT,
            _proof_report_body("pr.1", "intent.a", verdict="fail"),
            ProofReportBody,
        )
        corpus = build_case_corpus(registry)
        case = corpus.case_for_intent("intent.a")
        assert case is not None
        assert case.has_failures() is True

    def test_has_failures_true_on_critical_escalation(self, registry: ArtifactRegistry, tmp_path: Path) -> None:
        _register(registry, tmp_path, "i1", ArtifactType.INTENT, _intent_body("intent.a"), IntentBody)
        _register(
            registry,
            tmp_path,
            "esc.1",
            ArtifactType.ESCALATION_REPORT,
            _escalation_body("esc.1", "intent.a", severity="critical"),
            EscalationReportBody,
        )
        corpus = build_case_corpus(registry)
        case = corpus.case_for_intent("intent.a")
        assert case is not None
        assert case.has_failures() is True

    def test_has_failures_false_on_info_escalation(self, registry: ArtifactRegistry, tmp_path: Path) -> None:
        _register(registry, tmp_path, "i1", ArtifactType.INTENT, _intent_body("intent.a"), IntentBody)
        _register(
            registry,
            tmp_path,
            "esc.1",
            ArtifactType.ESCALATION_REPORT,
            _escalation_body("esc.1", "intent.a", severity="info"),
            EscalationReportBody,
        )
        corpus = build_case_corpus(registry)
        case = corpus.case_for_intent("intent.a")
        assert case is not None
        assert case.has_failures() is False


# ─── Pattern → cases secondary index ──────────────────────────────


class TestPatternIndex:
    def test_pattern_index_groups_cases(self, registry: ArtifactRegistry, tmp_path: Path) -> None:
        _register(
            registry,
            tmp_path,
            "i1",
            ArtifactType.INTENT,
            _intent_body("intent.a", ["safety.no_x_amplifies_y"]),
            IntentBody,
        )
        _register(
            registry,
            tmp_path,
            "i2",
            ArtifactType.INTENT,
            _intent_body("intent.b", ["safety.no_x_amplifies_y", "liveness.eventually_x_holds"]),
            IntentBody,
        )
        corpus = build_case_corpus(registry)
        safety_cases = corpus.cases_for_pattern("safety.no_x_amplifies_y")
        liveness_cases = corpus.cases_for_pattern("liveness.eventually_x_holds")
        assert {c.intent_id for c in safety_cases} == {"intent.a", "intent.b"}
        assert {c.intent_id for c in liveness_cases} == {"intent.b"}

    def test_pattern_with_no_cases_returns_empty(self, registry: ArtifactRegistry) -> None:
        corpus = build_case_corpus(registry)
        assert corpus.cases_for_pattern("nope.unknown") == []


# ─── Unattached evidence (orphans) ────────────────────────────────


class TestUnattachedEvidence:
    def test_proof_report_with_no_intent_is_unattached(self, registry: ArtifactRegistry, tmp_path: Path) -> None:
        # No intent registered for "ghost.intent"
        _register(
            registry,
            tmp_path,
            "pr.1",
            ArtifactType.PROOF_REPORT,
            _proof_report_body("pr.1", "ghost.intent"),
            ProofReportBody,
        )
        corpus = build_case_corpus(registry)
        assert corpus.cases() == []
        unattached = corpus.unattached_evidence
        assert len(unattached) == 1
        assert unattached[0].kind == "proof_report"


# ─── Determinism / idempotency ────────────────────────────────────


class TestCorpusDeterminism:
    def test_cases_returned_sorted(self, registry: ArtifactRegistry, tmp_path: Path) -> None:
        for name in ["intent.zeta", "intent.alpha", "intent.middle"]:
            _register(registry, tmp_path, f"i_{name}", ArtifactType.INTENT, _intent_body(name), IntentBody)
        corpus = build_case_corpus(registry)
        ids = [c.intent_id for c in corpus.cases()]
        assert ids == sorted(ids)

    def test_rebuild_is_idempotent(self, registry: ArtifactRegistry, tmp_path: Path) -> None:
        _register(registry, tmp_path, "i1", ArtifactType.INTENT, _intent_body("intent.a"), IntentBody)
        _register(
            registry,
            tmp_path,
            "pr.1",
            ArtifactType.PROOF_REPORT,
            _proof_report_body("pr.1", "intent.a"),
            ProofReportBody,
        )
        corpus = build_case_corpus(registry)
        first = [(c.intent_id, len(c.evidence)) for c in corpus.cases()]
        corpus.build()
        second = [(c.intent_id, len(c.evidence)) for c in corpus.cases()]
        assert first == second
