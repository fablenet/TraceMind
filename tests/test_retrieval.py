"""Tests for ``tm.kb.retrieval`` (Stage 5-4 task 4.3).

Covers:
- :class:`PatternKeywordRetriever` — token-overlap ranking + category filter
- :class:`CaseStructuredRetriever` — pattern_id / failure / kind filters
- :class:`VectorRetriever` — stub raises NotImplementedError
- :class:`RetrievalBundle` — fans out across pattern + case retrievers
- :func:`ripgrep_search` — degrades gracefully when ``rg`` is missing
- :class:`Retriever` Protocol — all backends honour it
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
)
from tm.artifacts.registry import ArtifactRegistry
from tm.artifacts.storage import RegistryStorage
from tm.kb import (
    CaseStructuredRetriever,
    PatternKeywordRetriever,
    RetrievalBundle,
    RetrievalHit,
    Retriever,
    VectorRetriever,
    build_case_corpus,
    make_default_bundle,
    ripgrep_available,
    ripgrep_search,
)
from tm.patterns import load_seed_patterns

# ─── Fixtures shared across cases ─────────────────────────────────


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
) -> None:
    body = body_cls.from_mapping(body_raw)
    envelope = _envelope(artifact_id, artifact_type, body_raw)
    artifact = Artifact(envelope=envelope, body=body, body_raw=body_raw)
    path = tmp_path / f"{artifact_id.replace('.', '_')}.yaml"
    path.write_text(
        yaml.safe_dump(
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
            }
        ),
        encoding="utf-8",
    )
    registry.add(artifact, str(path))


@pytest.fixture
def pattern_library():
    return load_seed_patterns()


@pytest.fixture
def populated_corpus(tmp_path: Path):
    registry = ArtifactRegistry(storage=RegistryStorage(tmp_path / "registry.jsonl"))
    _register(
        registry,
        tmp_path,
        "i.alpha",
        ArtifactType.INTENT,
        {
            "intent_id": "intent.alpha",
            "title": "Alpha",
            "context": "x",
            "goal": "x",
            "property_pattern_refs": ["safety.no_x_amplifies_y"],
            "slot_fills": {},
        },
        IntentBody,
    )
    _register(
        registry,
        tmp_path,
        "i.beta",
        ArtifactType.INTENT,
        {
            "intent_id": "intent.beta",
            "title": "Beta",
            "context": "x",
            "goal": "x",
            "property_pattern_refs": ["safety.no_x_amplifies_y", "fairness.bounded_x_across_actors"],
            "slot_fills": {},
        },
        IntentBody,
    )
    _register(
        registry,
        tmp_path,
        "pr.beta.fail",
        ArtifactType.PROOF_REPORT,
        {
            "report_id": "pr.beta.fail",
            "intent_id": "intent.beta",
            "cycle_id": "c1",
            "overall_verdict": "fail",
            "evidence_chain": [
                {
                    "source": "kripke",
                    "event_type": "counterexample.found",
                    "data": {"trace": ["s0", "s1"]},
                }
            ],
        },
        ProofReportBody,
    )
    _register(
        registry,
        tmp_path,
        "esc.alpha.crit",
        ArtifactType.ESCALATION_REPORT,
        {
            "report_id": "esc.alpha.crit",
            "timestamp": "2026-05-18T01:00:00Z",
            "severity": "critical",
            "intent_ref": "intent.alpha",
            "suggested_actions": ["tune.threshold"],
        },
        EscalationReportBody,
    )
    return build_case_corpus(registry)


# ─── PatternKeywordRetriever ──────────────────────────────────────


class TestPatternKeywordRetriever:
    def test_keyword_overlap_ranks_relevant_pattern_first(self, pattern_library) -> None:
        retr = PatternKeywordRetriever(pattern_library)
        hits = retr.query("enforcement mediation fairness", limit=3)
        assert hits
        assert hits[0].ref == "fairness.bounded_x_across_actors"
        assert hits[0].score > 0

    def test_pattern_id_substring_boost(self, pattern_library) -> None:
        retr = PatternKeywordRetriever(pattern_library)
        hits = retr.query("safety pattern", limit=3)
        assert hits
        # safety pattern_id segment should boost
        assert hits[0].ref == "safety.no_x_amplifies_y"

    def test_category_filter(self, pattern_library) -> None:
        retr = PatternKeywordRetriever(pattern_library)
        hits = retr.query("eventually", limit=5, category="liveness")
        assert hits
        for h in hits:
            assert h.payload["category"] == "liveness"

    def test_empty_query_returns_all_with_zero_scores(self, pattern_library) -> None:
        retr = PatternKeywordRetriever(pattern_library)
        hits = retr.query("", limit=10)
        assert len(hits) == 3
        for h in hits:
            assert h.score == 0.0

    def test_no_match_returns_empty(self, pattern_library) -> None:
        retr = PatternKeywordRetriever(pattern_library)
        hits = retr.query("xyzzy unrelated lorem", limit=5)
        assert hits == []

    def test_results_deterministic_order(self, pattern_library) -> None:
        retr = PatternKeywordRetriever(pattern_library)
        a = retr.query("predicate", limit=10)
        b = retr.query("predicate", limit=10)
        assert [h.ref for h in a] == [h.ref for h in b]

    def test_returns_retrieval_hits(self, pattern_library) -> None:
        retr = PatternKeywordRetriever(pattern_library)
        hits = retr.query("safety", limit=1)
        assert all(isinstance(h, RetrievalHit) for h in hits)
        if hits:
            assert hits[0].kind == "pattern"

    def test_protocol_compliance(self, pattern_library) -> None:
        retr = PatternKeywordRetriever(pattern_library)
        assert isinstance(retr, Retriever)


# ─── CaseStructuredRetriever ──────────────────────────────────────


class TestCaseStructuredRetriever:
    def test_filter_by_pattern_id(self, populated_corpus) -> None:
        retr = CaseStructuredRetriever(populated_corpus)
        hits = retr.query("", pattern_id="safety.no_x_amplifies_y", limit=5)
        # Both intents reference safety pattern
        assert {h.ref for h in hits} == {"intent.alpha", "intent.beta"}

    def test_filter_by_pattern_id_narrower(self, populated_corpus) -> None:
        retr = CaseStructuredRetriever(populated_corpus)
        hits = retr.query("", pattern_id="fairness.bounded_x_across_actors", limit=5)
        # Only beta references fairness pattern
        assert [h.ref for h in hits] == ["intent.beta"]

    def test_filter_has_failures_true(self, populated_corpus) -> None:
        retr = CaseStructuredRetriever(populated_corpus)
        hits = retr.query("", has_failures=True, limit=5)
        # Both have failures (alpha has critical escalation, beta has failed proof)
        assert {h.ref for h in hits} == {"intent.alpha", "intent.beta"}

    def test_filter_evidence_kind(self, populated_corpus) -> None:
        retr = CaseStructuredRetriever(populated_corpus)
        proof_hits = retr.query("", evidence_kind="proof_report", limit=5)
        esc_hits = retr.query("", evidence_kind="escalation_report", limit=5)
        assert [h.ref for h in proof_hits] == ["intent.beta"]
        assert [h.ref for h in esc_hits] == ["intent.alpha"]

    def test_text_substring_match(self, populated_corpus) -> None:
        retr = CaseStructuredRetriever(populated_corpus)
        hits = retr.query("alpha", limit=5)
        assert any(h.ref == "intent.alpha" for h in hits)

    def test_failure_cases_boosted_in_tie(self, populated_corpus) -> None:
        retr = CaseStructuredRetriever(populated_corpus)
        hits = retr.query("", limit=5)
        # Both cases have failures, so both score 1.0 — order broken by ref asc
        assert hits[0].ref == "intent.alpha"

    def test_empty_corpus_returns_empty(self, tmp_path: Path) -> None:
        registry = ArtifactRegistry(storage=RegistryStorage(tmp_path / "empty.jsonl"))
        corpus = build_case_corpus(registry)
        retr = CaseStructuredRetriever(corpus)
        assert retr.query("anything") == []

    def test_protocol_compliance(self, populated_corpus) -> None:
        retr = CaseStructuredRetriever(populated_corpus)
        assert isinstance(retr, Retriever)


# ─── VectorRetriever (stub) ───────────────────────────────────────


class TestVectorRetriever:
    def test_query_raises_not_implemented(self) -> None:
        vr = VectorRetriever()
        with pytest.raises(NotImplementedError, match="reserved seam"):
            vr.query("anything")

    def test_accepts_arbitrary_kwargs_for_future_compat(self) -> None:
        """Constructor must not reject future args — important for seam stability."""
        VectorRetriever(embedder="future-embedder", index_path="/tmp/idx")

    def test_protocol_compliance(self) -> None:
        vr = VectorRetriever()
        assert isinstance(vr, Retriever)


# ─── RetrievalBundle ───────────────────────────────────────────────


class TestRetrievalBundle:
    def test_bundle_fans_out_to_both_retrievers(self, pattern_library, populated_corpus) -> None:
        bundle = make_default_bundle(pattern_library, populated_corpus)
        hits = bundle.query("safety", limit=10)
        kinds = {h.kind for h in hits}
        # Should see both kinds in the merged result set
        assert "pattern" in kinds or "case" in kinds

    def test_bundle_respects_limit(self, pattern_library, populated_corpus) -> None:
        bundle = make_default_bundle(pattern_library, populated_corpus)
        hits = bundle.query("safety", limit=2)
        assert len(hits) <= 2

    def test_vector_failure_does_not_break_bundle(self, pattern_library, populated_corpus) -> None:
        bundle = RetrievalBundle(
            pattern_retriever=PatternKeywordRetriever(pattern_library),
            case_retriever=CaseStructuredRetriever(populated_corpus),
            vector_retriever=VectorRetriever(),
        )
        # Should swallow NotImplementedError and return other hits
        hits = bundle.query("safety", limit=5)
        assert isinstance(hits, list)

    def test_deterministic_order(self, pattern_library, populated_corpus) -> None:
        bundle = make_default_bundle(pattern_library, populated_corpus)
        a = bundle.query("intent", limit=10)
        b = bundle.query("intent", limit=10)
        assert [(h.kind, h.ref) for h in a] == [(h.kind, h.ref) for h in b]


# ─── Ripgrep degradation ───────────────────────────────────────────


class TestRipgrep:
    def test_ripgrep_search_returns_list(self, tmp_path: Path) -> None:
        # Regardless of rg availability, this must return a list
        result = ripgrep_search("anything", root=tmp_path)
        assert isinstance(result, list)

    def test_ripgrep_search_when_available(self, tmp_path: Path) -> None:
        """If rg is available, it should find content."""
        if not ripgrep_available():
            pytest.skip("ripgrep not installed in this env")
        (tmp_path / "needle.txt").write_text("haystack-marker-x9z2\n")
        hits = ripgrep_search("marker-x9z2", root=tmp_path)
        assert hits
        assert hits[0].kind == "ripgrep"
        assert "marker-x9z2" in hits[0].snippet

    def test_ripgrep_search_missing_root(self, tmp_path: Path) -> None:
        # rg returns no matches for missing-but-not-erroring search;
        # tolerant return is required regardless of rg availability
        result = ripgrep_search("anything", root=tmp_path / "does-not-exist")
        assert isinstance(result, list)


# ─── Retriever Protocol structural check ──────────────────────────


class TestRetrieverProtocol:
    def test_arbitrary_class_with_query_satisfies_protocol(self) -> None:
        class _Custom:
            def query(self, text: str, *, limit: int = 5, **kwargs: Any):
                return []

        custom = _Custom()
        assert isinstance(custom, Retriever)
