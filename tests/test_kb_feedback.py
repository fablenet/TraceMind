"""Tests for ``tm.kb.feedback`` (Stage 5-4 task 4.4).

Covers:
- Signal collection from CaseCorpus (failed proofs + critical/warning
  escalations only; passing proofs / info escalations ignored)
- :func:`synthesize_kb_patch_proposal` produces a valid
  ProposedChangePlanBody
- Deterministic IDs (idempotent re-runs)
- Empty corpus / no failures → ``proposal=None``
- The proposal passes K-Ontology v0.2 governance (``verify``)
- Pattern-attribution: signals with no pattern_ids generate a
  ``review_unattributed`` decision
- Multiple patterns → one decision per pattern, sorted deterministically
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
    body_hash,
    verify,
)
from tm.artifacts.registry import ArtifactRegistry
from tm.artifacts.storage import RegistryStorage
from tm.kb import (
    build_case_corpus,
    collect_feedback_signals,
    run_feedback_loop,
    synthesize_kb_patch_proposal,
)
from tm.kb.feedback import FeedbackSignal

# ─── Helpers ──────────────────────────────────────────────────────


def _envelope(aid: str, atype: ArtifactType, body_raw: Mapping[str, Any]) -> ArtifactEnvelope:
    return ArtifactEnvelope(
        artifact_id=aid,
        status=ArtifactStatus.ACCEPTED,
        artifact_type=atype,
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
    aid: str,
    atype: ArtifactType,
    body_raw: Mapping[str, Any],
    body_cls,
) -> None:
    body = body_cls.from_mapping(body_raw)
    env = _envelope(aid, atype, body_raw)
    artifact = Artifact(envelope=env, body=body, body_raw=body_raw)
    p = tmp_path / f"{aid.replace('.', '_')}.yaml"
    p.write_text(
        yaml.safe_dump(
            {
                "envelope": {
                    "artifact_id": env.artifact_id,
                    "status": env.status.value,
                    "artifact_type": env.artifact_type.value,
                    "version": env.version,
                    "created_by": env.created_by,
                    "created_at": env.created_at,
                    "body_hash": env.body_hash,
                    "envelope_hash": env.envelope_hash,
                    "meta": dict(env.meta),
                },
                "body": dict(body_raw),
            }
        ),
        encoding="utf-8",
    )
    registry.add(artifact, str(p))


@pytest.fixture
def registry(tmp_path: Path) -> ArtifactRegistry:
    return ArtifactRegistry(storage=RegistryStorage(tmp_path / "registry.jsonl"))


def _proof_report(report_id: str, intent_id: str, *, verdict: str = "fail", with_counterexample: bool = True) -> dict:
    chain = []
    if with_counterexample:
        chain.append(
            {
                "source": "kripke",
                "event_type": "counterexample.found",
                "data": {"trace": ["s0", "s1"]},
            }
        )
    return {
        "report_id": report_id,
        "intent_id": intent_id,
        "cycle_id": f"c.{report_id}",
        "overall_verdict": verdict,
        "evidence_chain": chain,
    }


def _escalation(report_id: str, intent_ref: str, *, severity: str = "critical") -> dict:
    return {
        "report_id": report_id,
        "timestamp": "2026-05-18T01:00:00Z",
        "severity": severity,
        "intent_ref": intent_ref,
        "recent_rules_fired": ["rule.a", "rule.b"],
        "suggested_actions": ["tune.threshold"],
        "counterexample": {"state": "violated"},
    }


def _intent(intent_id: str, pattern_refs: list[str] | None = None) -> dict:
    return {
        "intent_id": intent_id,
        "title": "X",
        "context": "X",
        "goal": "X",
        "property_pattern_refs": pattern_refs or [],
        "slot_fills": {},
    }


# ─── Signal collection ────────────────────────────────────────────


class TestCollectSignals:
    def test_no_failures_returns_empty(self, registry: ArtifactRegistry, tmp_path: Path) -> None:
        _register(
            registry,
            tmp_path,
            "i.demo",
            ArtifactType.INTENT,
            _intent("demo.intent", ["safety.no_x_amplifies_y"]),
            IntentBody,
        )
        _register(
            registry,
            tmp_path,
            "pr.demo.pass",
            ArtifactType.PROOF_REPORT,
            _proof_report("pr.demo.pass", "demo.intent", verdict="pass", with_counterexample=False),
            ProofReportBody,
        )
        corpus = build_case_corpus(registry)
        signals = collect_feedback_signals(corpus)
        assert signals == []

    def test_failed_proof_produces_signal(self, registry: ArtifactRegistry, tmp_path: Path) -> None:
        _register(
            registry,
            tmp_path,
            "i.demo",
            ArtifactType.INTENT,
            _intent("demo.intent", ["safety.no_x_amplifies_y"]),
            IntentBody,
        )
        _register(
            registry,
            tmp_path,
            "pr.demo.fail",
            ArtifactType.PROOF_REPORT,
            _proof_report("pr.demo.fail", "demo.intent"),
            ProofReportBody,
        )
        corpus = build_case_corpus(registry)
        signals = collect_feedback_signals(corpus)
        assert len(signals) == 1
        signal = signals[0]
        assert signal.source_kind == "proof_report"
        assert signal.source_ref == "pr.demo.fail"
        assert signal.severity == "critical"
        assert signal.pattern_ids == ["safety.no_x_amplifies_y"]
        assert "trace" in signal.counterexample.get("data", {})

    def test_inconclusive_proof_produces_warning_signal(self, registry: ArtifactRegistry, tmp_path: Path) -> None:
        _register(registry, tmp_path, "i.demo", ArtifactType.INTENT, _intent("demo.intent"), IntentBody)
        _register(
            registry,
            tmp_path,
            "pr.demo.maybe",
            ArtifactType.PROOF_REPORT,
            _proof_report("pr.demo.maybe", "demo.intent", verdict="inconclusive"),
            ProofReportBody,
        )
        corpus = build_case_corpus(registry)
        signals = collect_feedback_signals(corpus)
        assert len(signals) == 1
        assert signals[0].severity == "warning"

    def test_critical_escalation_produces_signal(self, registry: ArtifactRegistry, tmp_path: Path) -> None:
        _register(registry, tmp_path, "i.demo", ArtifactType.INTENT, _intent("demo.intent"), IntentBody)
        _register(
            registry,
            tmp_path,
            "esc.demo",
            ArtifactType.ESCALATION_REPORT,
            _escalation("esc.demo", "demo.intent", severity="critical"),
            EscalationReportBody,
        )
        corpus = build_case_corpus(registry)
        signals = collect_feedback_signals(corpus)
        assert len(signals) == 1
        assert signals[0].source_kind == "escalation_report"
        assert signals[0].severity == "critical"

    def test_info_escalation_does_not_produce_signal(self, registry: ArtifactRegistry, tmp_path: Path) -> None:
        _register(registry, tmp_path, "i.demo", ArtifactType.INTENT, _intent("demo.intent"), IntentBody)
        _register(
            registry,
            tmp_path,
            "esc.demo",
            ArtifactType.ESCALATION_REPORT,
            _escalation("esc.demo", "demo.intent", severity="info"),
            EscalationReportBody,
        )
        corpus = build_case_corpus(registry)
        signals = collect_feedback_signals(corpus)
        assert signals == []

    def test_signals_are_deterministically_sorted(self, registry: ArtifactRegistry, tmp_path: Path) -> None:
        _register(registry, tmp_path, "i.alpha", ArtifactType.INTENT, _intent("intent.alpha"), IntentBody)
        _register(registry, tmp_path, "i.zeta", ArtifactType.INTENT, _intent("intent.zeta"), IntentBody)
        _register(
            registry,
            tmp_path,
            "pr.zeta.fail",
            ArtifactType.PROOF_REPORT,
            _proof_report("pr.zeta.fail", "intent.zeta"),
            ProofReportBody,
        )
        _register(
            registry,
            tmp_path,
            "pr.alpha.fail",
            ArtifactType.PROOF_REPORT,
            _proof_report("pr.alpha.fail", "intent.alpha"),
            ProofReportBody,
        )
        corpus = build_case_corpus(registry)
        signals = collect_feedback_signals(corpus)
        ids = [s.intent_id for s in signals]
        assert ids == sorted(ids)


class TestSignalFingerprint:
    def test_fingerprint_stable(self) -> None:
        s = FeedbackSignal(
            source_kind="proof_report",
            source_ref="pr.1",
            intent_id="intent.a",
            pattern_ids=["safety.no_x_amplifies_y"],
            counterexample={"trace": ["s0", "s1"]},
        )
        assert s.fingerprint() == s.fingerprint()

    def test_fingerprint_differs_for_different_signals(self) -> None:
        s1 = FeedbackSignal(
            source_kind="proof_report",
            source_ref="pr.1",
            intent_id="intent.a",
            pattern_ids=["safety.x"],
            counterexample={"trace": ["s0"]},
        )
        s2 = FeedbackSignal(
            source_kind="proof_report",
            source_ref="pr.2",
            intent_id="intent.a",
            pattern_ids=["safety.x"],
            counterexample={"trace": ["s0"]},
        )
        assert s1.fingerprint() != s2.fingerprint()


# ─── Proposal synthesis ───────────────────────────────────────────


class TestSynthesizePatchProposal:
    def test_no_signals_returns_none(self) -> None:
        assert synthesize_kb_patch_proposal([]) is None

    def test_single_pattern_signal_produces_one_decision(self) -> None:
        signal = FeedbackSignal(
            source_kind="proof_report",
            source_ref="pr.1",
            intent_id="demo.intent",
            pattern_ids=["safety.no_x_amplifies_y"],
            counterexample={"trace": ["s0", "s1"]},
            severity="critical",
            summary="failed",
        )
        proposal = synthesize_kb_patch_proposal([signal])
        assert proposal is not None
        assert proposal.intent_id == "demo.intent"
        assert len(proposal.decisions) == 1
        d = proposal.decisions[0]
        assert d.target_state["operation"] == "review_pattern"
        assert d.target_state["pattern_id"] == "safety.no_x_amplifies_y"
        assert "safety/no_x_amplifies_y" in d.effect_ref
        assert d.reasoning_trace is not None
        assert "pr.1" in d.reasoning_trace

    def test_multiple_patterns_produce_sorted_decisions(self) -> None:
        signals = [
            FeedbackSignal(
                source_kind="proof_report",
                source_ref="pr.1",
                intent_id="demo.intent",
                pattern_ids=["safety.no_x_amplifies_y", "liveness.eventually_x_holds"],
                counterexample={"trace": ["s0"]},
            ),
        ]
        proposal = synthesize_kb_patch_proposal(signals)
        assert proposal is not None
        # Both patterns get a decision, sorted alphabetically
        pids = [d.target_state["pattern_id"] for d in proposal.decisions]
        assert pids == ["liveness.eventually_x_holds", "safety.no_x_amplifies_y"]

    def test_signal_without_pattern_id_produces_unattributed(self) -> None:
        signal = FeedbackSignal(
            source_kind="escalation_report",
            source_ref="esc.1",
            intent_id="demo.intent",
            pattern_ids=[],
            counterexample={"state": "stuck"},
            severity="critical",
        )
        proposal = synthesize_kb_patch_proposal([signal])
        assert proposal is not None
        ops = [d.target_state["operation"] for d in proposal.decisions]
        assert "review_unattributed" in ops

    def test_idempotent_re_runs(self) -> None:
        signal = FeedbackSignal(
            source_kind="proof_report",
            source_ref="pr.1",
            intent_id="demo.intent",
            pattern_ids=["safety.no_x_amplifies_y"],
            counterexample={"trace": ["s0"]},
        )
        a = synthesize_kb_patch_proposal([signal])
        b = synthesize_kb_patch_proposal([signal])
        assert a is not None and b is not None
        assert a.plan_id == b.plan_id
        for da, db in zip(a.decisions, b.decisions):
            assert da.idempotency_key == db.idempotency_key

    def test_different_signals_produce_different_proposal_ids(self) -> None:
        s1 = FeedbackSignal(
            source_kind="proof_report",
            source_ref="pr.1",
            intent_id="demo.intent",
            pattern_ids=["safety.x"],
            counterexample={"trace": ["s0"]},
        )
        s2 = FeedbackSignal(
            source_kind="proof_report",
            source_ref="pr.2",
            intent_id="demo.intent",
            pattern_ids=["safety.x"],
            counterexample={"trace": ["s1"]},
        )
        a = synthesize_kb_patch_proposal([s1])
        b = synthesize_kb_patch_proposal([s2])
        assert a.plan_id != b.plan_id

    def test_proposal_marks_deterministic_llm_metadata(self) -> None:
        signal = FeedbackSignal(
            source_kind="proof_report",
            source_ref="pr.1",
            intent_id="demo.intent",
            pattern_ids=["safety.no_x_amplifies_y"],
            counterexample={"trace": []},
        )
        proposal = synthesize_kb_patch_proposal([signal])
        assert proposal is not None
        assert proposal.llm_metadata.determinism_hint == "deterministic"
        # No real LLM
        assert (
            "deterministic" in proposal.llm_metadata.model.lower() or "feedback" in proposal.llm_metadata.model.lower()
        )

    def test_policy_requirements_present(self) -> None:
        signal = FeedbackSignal(
            source_kind="proof_report",
            source_ref="pr.1",
            intent_id="demo.intent",
            pattern_ids=["safety.x"],
            counterexample={},
        )
        proposal = synthesize_kb_patch_proposal([signal])
        assert proposal is not None
        assert proposal.policy_requirements
        assert any("test" in r for r in proposal.policy_requirements)


# ─── Governance round-trip ────────────────────────────────────────


class TestProposalGovernance:
    def test_synthesized_proposal_passes_verify(self, registry: ArtifactRegistry, tmp_path: Path) -> None:
        _register(
            registry,
            tmp_path,
            "i.demo",
            ArtifactType.INTENT,
            _intent("demo.intent", ["safety.no_x_amplifies_y"]),
            IntentBody,
        )
        _register(
            registry,
            tmp_path,
            "pr.demo.fail",
            ArtifactType.PROOF_REPORT,
            _proof_report("pr.demo.fail", "demo.intent"),
            ProofReportBody,
        )
        corpus = build_case_corpus(registry)
        signals, proposal = run_feedback_loop(corpus)
        assert proposal is not None

        body_raw = _proposal_to_dict(proposal)
        env = ArtifactEnvelope(
            artifact_id=proposal.plan_id,
            status=ArtifactStatus.CANDIDATE,
            artifact_type=ArtifactType.PROPOSED_CHANGE_PLAN,
            version="v0",
            created_by="kb.feedback",
            created_at="2026-05-18T02:00:00Z",
            body_hash=body_hash(body_raw),
            envelope_hash="",
            meta={},
        )
        candidate = Artifact(envelope=env, body=proposal, body_raw=body_raw)
        accepted, report = verify(candidate)
        assert not report.errors, f"governance errors: {report.errors}"
        assert accepted.envelope.status == ArtifactStatus.ACCEPTED


def _proposal_to_dict(proposal) -> dict[str, Any]:
    return {
        "plan_id": proposal.plan_id,
        "intent_id": proposal.intent_id,
        "decisions": [
            {
                "effect_ref": d.effect_ref,
                "target_state": dict(d.target_state),
                "idempotency_key": d.idempotency_key,
                "reasoning_trace": d.reasoning_trace,
            }
            for d in proposal.decisions
        ],
        "llm_metadata": {
            "model": proposal.llm_metadata.model,
            "prompt_hash": proposal.llm_metadata.prompt_hash,
            "determinism_hint": proposal.llm_metadata.determinism_hint,
        },
        "summary": proposal.summary,
        "policy_requirements": list(proposal.policy_requirements),
    }


# ─── End-to-end loop ──────────────────────────────────────────────


class TestRunFeedbackLoop:
    def test_empty_corpus_yields_none_proposal(self, registry: ArtifactRegistry) -> None:
        corpus = build_case_corpus(registry)
        signals, proposal = run_feedback_loop(corpus)
        assert signals == []
        assert proposal is None

    def test_passes_signals_and_proposal_consistently(self, registry: ArtifactRegistry, tmp_path: Path) -> None:
        _register(
            registry,
            tmp_path,
            "i.demo",
            ArtifactType.INTENT,
            _intent("demo.intent", ["safety.no_x_amplifies_y"]),
            IntentBody,
        )
        _register(
            registry,
            tmp_path,
            "pr.demo.fail",
            ArtifactType.PROOF_REPORT,
            _proof_report("pr.demo.fail", "demo.intent"),
            ProofReportBody,
        )
        _register(
            registry,
            tmp_path,
            "esc.demo",
            ArtifactType.ESCALATION_REPORT,
            _escalation("esc.demo", "demo.intent"),
            EscalationReportBody,
        )
        corpus = build_case_corpus(registry)
        signals, proposal = run_feedback_loop(corpus)
        assert len(signals) == 2
        assert proposal is not None
        # Both signals reference the same pattern → 1 decision
        assert len(proposal.decisions) == 1
        assert proposal.decisions[0].target_state["pattern_id"] == "safety.no_x_amplifies_y"
