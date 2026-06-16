"""Tests for ``tm.steps.ai_propose_pattern`` (Stage 5-4 task 4.1).

Covers:
- Pure-function core (:func:`propose_pattern_instances`):
  - Returns ranked :class:`PatternProposal` list
  - Honours ``slot_hints`` (no missing slots when hints complete)
  - Honours ``category`` filter
  - Empty query / no matches
  - Determinism between calls
- Async step entry (:func:`run`):
  - Successful run on the ``fake`` provider
  - Validation errors → BAD_REQUEST
  - Unsupported provider → PROVIDER_NOT_SUPPORTED
- **Non-LLM invariant**: ``ai_propose_pattern.py`` does not import any
  LLM client / provider symbols (Phase 5 invariant 4 must be provable
  in CI for the fake path).
- End-to-end glue:
  - A proposal's ``(pattern_id, slot_fills)`` plugs straight into
    :func:`tm.patterns.build_intent_from_patterns`
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest

from tm.patterns import (
    IntentBuildRequest,
    build_intent_from_patterns,
    load_seed_patterns,
)
from tm.steps.ai_propose_pattern import (
    PatternProposal,
    propose_pattern_instances,
    run,
)

# ─── Fixtures ─────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def library():
    return load_seed_patterns()


@pytest.fixture
def safety_hints():
    return {
        "safety.no_x_amplifies_y": {
            "forbidden_predicate": "has(quarantined)",
            "required_predicate": "has(burst_detected)",
        }
    }


# ─── Pure-function core ───────────────────────────────────────────


class TestProposePatternInstancesCore:
    def test_returns_pattern_proposals(self, library) -> None:
        proposals = propose_pattern_instances("safety check", library, limit=3)
        assert all(isinstance(p, PatternProposal) for p in proposals)
        # Safety pattern should be in the result for "safety" query
        assert any(p.pattern_id == "safety.no_x_amplifies_y" for p in proposals)

    def test_results_sorted_by_score_desc(self, library) -> None:
        proposals = propose_pattern_instances("safety enforcement gate", library, limit=5)
        scores = [p.score for p in proposals]
        assert scores == sorted(scores, reverse=True)

    def test_limit_respected(self, library) -> None:
        proposals = propose_pattern_instances("predicate", library, limit=1)
        assert len(proposals) <= 1

    def test_slot_hints_filled_in_when_complete(self, library, safety_hints) -> None:
        proposals = propose_pattern_instances("safety check", library, slot_hints=safety_hints, limit=5)
        target = next(p for p in proposals if p.pattern_id == "safety.no_x_amplifies_y")
        assert target.slot_fills == safety_hints["safety.no_x_amplifies_y"]
        assert target.missing_slots == []

    def test_partial_hints_surface_missing_slots(self, library) -> None:
        partial_hints = {"safety.no_x_amplifies_y": {"forbidden_predicate": "has(x)"}}
        proposals = propose_pattern_instances("safety", library, slot_hints=partial_hints)
        target = next(p for p in proposals if p.pattern_id == "safety.no_x_amplifies_y")
        assert target.missing_slots == ["required_predicate"]
        assert target.slot_fills == {"forbidden_predicate": "has(x)"}

    def test_no_hints_lists_all_slots_as_missing(self, library) -> None:
        proposals = propose_pattern_instances("safety", library)
        target = next(p for p in proposals if p.pattern_id == "safety.no_x_amplifies_y")
        assert set(target.missing_slots) == {"forbidden_predicate", "required_predicate"}
        assert target.slot_fills == {}

    def test_category_filter_restricts_results(self, library) -> None:
        proposals = propose_pattern_instances("predicate", library, category="liveness", limit=5)
        assert proposals
        for p in proposals:
            entry = library.get(p.pattern_id)
            assert entry.category == "liveness"

    def test_no_match_returns_empty(self, library) -> None:
        proposals = propose_pattern_instances("xyzzy unrelated lorem ipsum", library)
        assert proposals == []

    def test_determinism(self, library, safety_hints) -> None:
        a = propose_pattern_instances("safety", library, slot_hints=safety_hints)
        b = propose_pattern_instances("safety", library, slot_hints=safety_hints)
        assert [p.pattern_id for p in a] == [p.pattern_id for p in b]
        assert [p.score for p in a] == [p.score for p in b]

    def test_rationale_contains_pattern_id(self, library) -> None:
        proposals = propose_pattern_instances("safety", library, limit=1)
        assert proposals
        assert "safety.no_x_amplifies_y" in proposals[0].rationale

    def test_unsupported_provider_raises(self, library) -> None:
        with pytest.raises(NotImplementedError, match="reserved"):
            propose_pattern_instances("foo", library, provider="openai")


# ─── End-to-end glue ─────────────────────────────────────────────-


class TestProposalsFeedDownstream:
    def test_complete_proposals_build_intent(self, library) -> None:
        proposals = propose_pattern_instances(
            "safety gate check then verify reachability",
            library,
            slot_hints={
                "safety.no_x_amplifies_y": {
                    "forbidden_predicate": "has(x)",
                    "required_predicate": "has(y)",
                },
                "liveness.eventually_x_holds": {"goal_predicate": "has(z)"},
            },
            limit=5,
        )
        complete = [p for p in proposals if not p.missing_slots]
        assert complete

        # Pull only unique pattern_ids (Stage 5-3 IntentBody constraint)
        seen: set[str] = set()
        instances_for_intent = []
        for p in complete:
            if p.pattern_id in seen:
                continue
            seen.add(p.pattern_id)
            instances_for_intent.append((p.pattern_id, p.slot_fills))

        req = IntentBuildRequest(
            intent_id="propose.demo",
            title="Demo from proposals",
            context="end-to-end test",
            goal="prove the chain",
            instances=instances_for_intent,
        )
        intent, resolved_instances = build_intent_from_patterns(library, req)
        assert intent.intent_id == "propose.demo"
        assert len(resolved_instances) == len(instances_for_intent)


# ─── Async step entry ─────────────────────────────────────────────


class TestAsyncRunEntry:
    def test_successful_run_returns_ok(self, safety_hints) -> None:
        result = asyncio.run(
            run(
                {
                    "nl_text": "safety check",
                    "provider": "fake",
                    "slot_hints": safety_hints,
                    "limit": 3,
                }
            )
        )
        assert result["status"] == "ok"
        assert result["provider"] == "fake"
        assert isinstance(result["candidates"], list)
        assert "candidates_json" in result
        assert "duration_ms" in result

    def test_run_without_hints_still_succeeds(self) -> None:
        result = asyncio.run(run({"nl_text": "safety", "provider": "fake"}))
        assert result["status"] == "ok"
        # candidates should exist but missing_slots non-empty
        if result["candidates"]:
            assert any(c["missing_slots"] for c in result["candidates"])

    def test_missing_nl_text_returns_bad_request(self) -> None:
        result = asyncio.run(run({"provider": "fake"}))
        assert result["status"] == "error"
        assert result["error_code"] == "BAD_REQUEST"

    def test_empty_nl_text_returns_bad_request(self) -> None:
        result = asyncio.run(run({"nl_text": "  ", "provider": "fake"}))
        assert result["status"] == "error"
        assert result["error_code"] == "BAD_REQUEST"

    def test_invalid_limit_returns_bad_request(self) -> None:
        result = asyncio.run(run({"nl_text": "x", "limit": 0}))
        assert result["status"] == "error"
        assert result["error_code"] == "BAD_REQUEST"

    def test_invalid_slot_hints_shape_returns_bad_request(self) -> None:
        result = asyncio.run(run({"nl_text": "x", "slot_hints": "not-a-dict"}))
        assert result["status"] == "error"
        assert result["error_code"] == "BAD_REQUEST"

    def test_openai_without_transport_degrades_to_rag(self) -> None:
        # Stage 7-1: a non-fake provider with no transport configured is
        # *unavailable*, not unsupported — the step degrades gracefully to the
        # deterministic RAG baseline (invariant 2) instead of erroring out.
        result = asyncio.run(run({"nl_text": "foo", "provider": "openai", "model": "m"}))
        assert result["status"] == "ok"
        assert result["source"] == "rag_fallback"
        assert result["fallback"] == "unreachable"

    def test_candidates_json_round_trips(self) -> None:
        import json

        result = asyncio.run(
            run(
                {
                    "nl_text": "liveness reachable",
                    "provider": "fake",
                    "limit": 2,
                }
            )
        )
        assert result["status"] == "ok"
        parsed = json.loads(result["candidates_json"])
        assert parsed == result["candidates"]


# ─── Custom library_root ──────────────────────────────────────────


class TestCustomLibraryRoot:
    def test_custom_library_root_is_honoured(self, tmp_path: Path) -> None:
        # Make a minimal library with 1 fake pattern
        cat_dir = tmp_path / "safety"
        cat_dir.mkdir()
        (cat_dir / "fake.yaml").write_text(
            (
                "pattern_id: safety.fake_only\n"
                "category: safety\n"
                "title: Fake-only safety\n"
                "description: A test-only pattern not in the seed library.\n"
                'formula_template: "AG {p}"\n'
                "slots:\n"
                "  - name: p\n"
                "    type: ctl_predicate\n"
                "    description: predicate slot\n"
                "    required: true\n"
            ),
            encoding="utf-8",
        )
        result = asyncio.run(
            run(
                {
                    "nl_text": "safety predicate",
                    "provider": "fake",
                    "library_root": str(tmp_path),
                }
            )
        )
        assert result["status"] == "ok"
        # Only the fake pattern is in the custom library
        candidate_ids = {c["pattern_id"] for c in result["candidates"]}
        if candidate_ids:
            assert candidate_ids.issubset({"safety.fake_only"})


# ─── Non-LLM invariant (behavioral) ───────────────────────────────


class TestNonLLMPathInvariants:
    """Stage 7-1 adds an LLM path to this step, so the Phase-5 *static* "no LLM
    import anywhere" check no longer holds. The genuine invariant (2/4) — the
    ``provider=fake`` path stays LLM-free — is now asserted **behaviorally**:
    the fake path must run to completion without ever constructing an LLM client.
    """

    def test_fake_path_does_not_construct_llm_client(self, monkeypatch) -> None:
        import tm.ai.llm_client as llm_client_mod

        def _boom(*args, **kwargs):  # pragma: no cover - must never be hit
            raise AssertionError("the fake path must not construct an LLM client (invariant 2/4)")

        monkeypatch.setattr(llm_client_mod, "make_client", _boom)
        result = asyncio.run(run({"nl_text": "safety predicate", "provider": "fake"}))
        assert result["status"] == "ok"
        assert result["source"] == "rag"
        assert result["evidence"] is None

    def test_llm_imports_are_deferred_not_module_level(self) -> None:
        """``make_client`` is imported lazily *inside* ``run`` so importing the
        step module never pulls in client machinery for the non-LLM baseline."""
        path = Path(__file__).resolve().parents[1] / "tm" / "steps" / "ai_propose_pattern.py"
        source = path.read_text(encoding="utf-8")
        assert "from tm.ai.llm_client import make_client" in source
        # the client import must not be a module-level (col-0) import
        assert not re.search(r"(?m)^from tm\.ai\.llm_client", source)
