"""Tests for the deterministic ``ai.refine_pattern`` step (Stage 7-2.4).

Covers the rule-based (fake) refine core, the provider-degrade contract, and
the Phase 7 pause-condition invariant: a multi-turn refine loop completes with
the fake path alone (no LLM), and the same input sequence yields byte-identical
candidates (invariant 2 equivalence).
"""

from __future__ import annotations

import asyncio

import pytest

from tm.patterns import load_seed_patterns
from tm.steps.ai_propose_pattern import PatternProposal
from tm.steps.ai_refine_pattern import (
    RefineAction,
    RefineResult,
    refine_candidates,
    run,
)

_FAIRNESS = "fairness.bounded_x_across_actors"


def _library():
    return load_seed_patterns()


def _fresh(library, pattern_id: str, score: float) -> PatternProposal:
    slots = [s.name for s in library.get(pattern_id).body.slots]
    return PatternProposal(pattern_id=pattern_id, slot_fills={}, score=score, missing_slots=slots)


def _two_candidates(library):
    ids = [i for i in ("fairness.bounded_x_across_actors", "liveness.eventually_x_holds") if i in library]
    # fall back to any two seed ids if the expected ones are renamed
    if len(ids) < 2:
        ids = library.ids()[:2]
    return [_fresh(library, ids[0], 0.9), _fresh(library, ids[1], 0.5)]


# ─── fill_slot ──────────────────────────────────────────────────────


def test_fill_slot_fills_and_reduces_missing() -> None:
    lib = _library()
    cands = [_fresh(lib, _FAIRNESS, 0.9)]
    action = RefineAction(kind="fill_slot", slot_values={"enforcement_predicate": "has(quarantined)"})
    result = refine_candidates(cands, action, lib)
    top = result.candidates[0]
    assert top.slot_fills["enforcement_predicate"] == "has(quarantined)"
    assert "enforcement_predicate" not in top.missing_slots
    assert "mediation_predicate" in top.missing_slots
    assert result.suggestions == ["mediation_predicate"]


def test_fill_slot_rejects_unknown_slot() -> None:
    lib = _library()
    cands = [_fresh(lib, _FAIRNESS, 0.9)]
    action = RefineAction(kind="fill_slot", slot_values={"not_a_slot": "x"})
    with pytest.raises(ValueError, match="not declared"):
        refine_candidates(cands, action, lib)


def test_fill_slot_does_not_mutate_input() -> None:
    lib = _library()
    cands = [_fresh(lib, _FAIRNESS, 0.9)]
    refine_candidates(cands, RefineAction(kind="fill_slot", slot_values={"enforcement_predicate": "has(x)"}), lib)
    assert cands[0].slot_fills == {}  # original untouched


def test_multi_turn_fill_completes_without_llm() -> None:
    """Phase 7 pause condition: the fake path drives the loop to a fully-filled
    candidate with no provider involvement."""
    lib = _library()
    cands = [_fresh(lib, _FAIRNESS, 0.9)]
    r1 = refine_candidates(
        cands,
        RefineAction(kind="fill_slot", slot_values={"enforcement_predicate": "has(quarantined)"}),
        lib,
        provider="fake",
    )
    r2 = refine_candidates(
        r1.candidates,
        RefineAction(kind="fill_slot", slot_values={"mediation_predicate": "done(human_review)"}),
        lib,
        provider="fake",
    )
    top = r2.candidates[0]
    assert top.missing_slots == []
    assert r2.suggestions == []
    assert top.slot_fills == {
        "enforcement_predicate": "has(quarantined)",
        "mediation_predicate": "done(human_review)",
    }


# ─── reject / select ────────────────────────────────────────────────


def test_reject_drops_top_and_promotes_next() -> None:
    lib = _library()
    cands = _two_candidates(lib)
    first, second = cands[0].pattern_id, cands[1].pattern_id
    result = refine_candidates(cands, RefineAction(kind="reject"), lib)
    assert [c.pattern_id for c in result.candidates] == [second]
    assert result.applied == f"reject:{first}"


def test_select_promotes_named_to_front() -> None:
    lib = _library()
    cands = _two_candidates(lib)
    second = cands[1].pattern_id
    result = refine_candidates(cands, RefineAction(kind="select", target_pattern_id=second), lib)
    assert result.candidates[0].pattern_id == second


def test_reject_unknown_target_raises() -> None:
    lib = _library()
    cands = _two_candidates(lib)
    with pytest.raises(ValueError, match="not among"):
        refine_candidates(cands, RefineAction(kind="reject", target_pattern_id="nope.nope"), lib)


def test_refine_empty_candidates_raises() -> None:
    lib = _library()
    with pytest.raises(ValueError, match="no candidates"):
        refine_candidates([], RefineAction(kind="reject"), lib)


def test_note_is_noop() -> None:
    lib = _library()
    cands = [_fresh(lib, _FAIRNESS, 0.9)]
    result = refine_candidates(cands, RefineAction(kind="note"), lib)
    assert result.applied == "note"
    assert [c.pattern_id for c in result.candidates] == [_FAIRNESS]


# ─── determinism + provider degrade (invariant 2) ───────────────────


def _drive_sequence(lib) -> RefineResult:
    cands = _two_candidates(lib)
    r = refine_candidates(cands, RefineAction(kind="reject"), lib, provider="fake")
    r = refine_candidates(r.candidates, RefineAction(kind="note"), lib, provider="fake")
    return r


def test_same_sequence_is_byte_identical() -> None:
    lib = _library()
    a = _drive_sequence(lib)
    b = _drive_sequence(lib)
    assert a.to_dict() == b.to_dict()


def test_nonfake_provider_degrades_to_fake() -> None:
    lib = _library()
    cands = [_fresh(lib, _FAIRNESS, 0.9)]
    action = RefineAction(kind="fill_slot", slot_values={"enforcement_predicate": "has(q)"})
    fake = refine_candidates(cands, action, lib, provider="fake")
    openai = refine_candidates(cands, action, lib, provider="openai")
    assert openai.degraded is True
    assert fake.degraded is False
    # identical candidate output regardless of (unavailable) provider
    assert openai.to_dict()["candidates"] == fake.to_dict()["candidates"]


# ─── action parsing ─────────────────────────────────────────────────


def test_action_from_mapping_rejects_bad_kind() -> None:
    with pytest.raises(ValueError, match="kind"):
        RefineAction.from_mapping({"kind": "teleport"})


def test_action_from_mapping_rejects_non_str_slot_values() -> None:
    with pytest.raises(ValueError, match="str"):
        RefineAction.from_mapping({"kind": "fill_slot", "slot_values": {"a": 1}})


# ─── async run() entry ──────────────────────────────────────────────


def test_run_ok_path_is_deterministic() -> None:
    lib = _library()
    candidate = _fresh(lib, _FAIRNESS, 0.9).to_dict()
    params = {
        "candidates": [candidate],
        "action": {"kind": "fill_slot", "slot_values": {"enforcement_predicate": "has(q)"}},
        "provider": "fake",
    }
    out1 = asyncio.run(run(dict(params)))
    out2 = asyncio.run(run(dict(params)))
    assert out1["status"] == "ok"
    assert out1["candidates_json"] == out2["candidates_json"]
    assert out1["suggestions"] == ["mediation_predicate"]


def test_run_bad_request() -> None:
    out = asyncio.run(run({"action": {"kind": "note"}}))  # missing candidates
    assert out["status"] == "error"
    assert out["error_code"] == "BAD_REQUEST"


def test_run_refine_error_surfaces() -> None:
    lib = _library()
    candidate = _fresh(lib, _FAIRNESS, 0.9).to_dict()
    out = asyncio.run(
        run(
            {
                "candidates": [candidate],
                "action": {"kind": "fill_slot", "slot_values": {"bogus": "v"}},
            }
        )
    )
    assert out["status"] == "error"
    assert out["error_code"] == "REFINE_FAILED"
