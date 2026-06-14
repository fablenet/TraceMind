"""Design-time consistency gate (Stage 7-0.8, plan §5b).

Hard gate (deterministic, blocks advance) = 5W1H complete ∧ CTL no-contradiction
∧ no exact duplicate. Soft warnings (RAG semantic-dup / ambiguity) never flip the
verdict. The report always carries an explicit "relative to what" boundary.
"""

from __future__ import annotations

import json
from pathlib import Path

from tm.intent.consistency_gate import (
    CHECK_COMPLETENESS,
    CHECK_NO_CONTRADICTION,
    CHECK_NO_EXACT_DUPLICATE,
    SoftWarning,
    evaluate_consistency,
)
from tm.intent.session import (
    GateFacts,
    advance,
    consistency_report_for,
    embed_completeness,
    new_session,
)


def _complete_report(profile: str = "base", errors: int = 0):
    return {"profile": profile, "summary": {"errors": errors, "warnings": 0}}


def _check(report, name):
    return next(c for c in report.hard_checks if c.name == name)


# ─── all-green ──────────────────────────────────────────────────────


def test_all_hard_checks_pass():
    r = evaluate_consistency(
        completeness_report=_complete_report(),
        verify_passed=True,
        body_hash="a" * 64,
        existing_hashes=("b" * 64,),
    )
    assert r.passed is True
    assert r.blocking_reasons == ()
    assert all(c.passed for c in r.hard_checks)


# ─── completeness hard check ────────────────────────────────────────


def test_incomplete_blocks_and_names_profile():
    r = evaluate_consistency(
        completeness_report=_complete_report(profile="fablenet.anonymity.v1", errors=2),
        verify_passed=True,
    )
    assert r.passed is False
    assert CHECK_COMPLETENESS in r.blocking_reasons
    assert "fablenet.anonymity.v1" in _check(r, CHECK_COMPLETENESS).detail


def test_missing_completeness_report_blocks():
    r = evaluate_consistency(completeness_report=None, verify_passed=True)
    assert r.passed is False
    assert CHECK_COMPLETENESS in r.blocking_reasons


# ─── contradiction hard check (CTL / verify) ────────────────────────


def test_verify_failed_blocks():
    r = evaluate_consistency(completeness_report=_complete_report(), verify_passed=False)
    assert r.passed is False
    assert CHECK_NO_CONTRADICTION in r.blocking_reasons


def test_verify_deferred_is_tolerated_but_honest():
    r = evaluate_consistency(completeness_report=_complete_report(), verify_passed=None)
    chk = _check(r, CHECK_NO_CONTRADICTION)
    assert chk.passed is True
    assert "not yet checked" in chk.detail  # does not falsely claim consistency
    assert r.passed is True


def test_verify_required_blocks_when_not_run():
    r = evaluate_consistency(
        completeness_report=_complete_report(),
        verify_passed=None,
        require_verify=True,
    )
    assert r.passed is False
    assert CHECK_NO_CONTRADICTION in r.blocking_reasons


# ─── exact-duplicate hard check ─────────────────────────────────────


def test_exact_duplicate_blocks():
    h = "c" * 64
    r = evaluate_consistency(
        completeness_report=_complete_report(),
        verify_passed=True,
        body_hash=h,
        existing_hashes=(h,),
    )
    assert r.passed is False
    assert CHECK_NO_EXACT_DUPLICATE in r.blocking_reasons


def test_no_hash_skips_duplicate_check():
    r = evaluate_consistency(
        completeness_report=_complete_report(),
        verify_passed=True,
        body_hash=None,
    )
    assert _check(r, CHECK_NO_EXACT_DUPLICATE).passed is True


# ─── soft warnings never flip the verdict ───────────────────────────


def test_soft_warnings_do_not_block():
    warns = (
        SoftWarning(kind="semantic_duplicate", message="looks like intent X", ref="i-x"),
        SoftWarning(kind="ambiguity", message="'fair' is ambiguous"),
    )
    r = evaluate_consistency(
        completeness_report=_complete_report(),
        verify_passed=True,
        soft_warnings=warns,
    )
    assert r.passed is True
    assert len(r.soft_warnings) == 2
    assert r.blocking_reasons == ()


# ─── boundary declaration ───────────────────────────────────────────


def test_boundary_declares_relative_to_what():
    r = evaluate_consistency(
        completeness_report=_complete_report(profile="k8s.hpa_fairness.v1"),
        verify_passed=True,
    )
    b = r.boundary.to_dict()
    assert b["completeness_relative_to_profile"] == "k8s.hpa_fairness.v1"
    assert b["contradiction_relative_to"] == "declared CTL properties"
    assert "NOT a claim of" in b["note"]


def test_explicit_profile_overrides_report_profile():
    r = evaluate_consistency(
        completeness_report=_complete_report(profile="from_report"),
        completeness_profile="explicit",
        verify_passed=True,
    )
    assert r.boundary.completeness_profile == "explicit"


# ─── determinism + serializability ─────────────────────────────────


def test_deterministic_and_serializable():
    kwargs = dict(
        completeness_report=_complete_report(),
        verify_passed=True,
        body_hash="d" * 64,
        existing_hashes=("e" * 64,),
        soft_warnings=(SoftWarning(kind="ambiguity", message="m"),),
    )
    a = evaluate_consistency(**kwargs)
    b = evaluate_consistency(**kwargs)
    assert a.to_dict() == b.to_dict()


# ─── session bridge (consistency_report_for) ───────────────────────


_COMPLETE_INTENT = {
    "intent_id": "intent.demo",
    "title": "demo",
    "context": "we operate an anonymous feed",
    "goal": "fairly disseminate viewpoints",
    "non_goals": [],
    "actors": ["reader", "author"],
    "inputs": ["content"],
    "outputs": ["ranked_feed"],
    "constraints": [],
    "success_metrics": [],
    "risks": [],
    "assumptions": [],
    "trace_links": {"parent_intent": None, "related_intents": []},
    "property_pattern_refs": ["fairness.bounded_x_across_actors"],
    "slot_fills": {},
}


def _session_with_completeness(tmp_path: Path):
    s = new_session("session.demo", "intent.demo")
    for _ in range(3):  # draft → check_5w1h → propose → refine
        s = advance(s, GateFacts(), role="agent")
    intent_path = tmp_path / "intent.json"
    intent_path.write_text(json.dumps(_COMPLETE_INTENT), encoding="utf-8")
    return embed_completeness(s, intent_path=intent_path, profile="base")


def test_bridge_derives_completeness_from_embedded_report(tmp_path: Path):
    s = _session_with_completeness(tmp_path)
    r = consistency_report_for(s, verify_passed=True)
    assert _check(r, CHECK_COMPLETENESS).passed is True
    assert r.boundary.completeness_profile == "base"
    assert r.passed is True


def test_bridge_blocks_when_no_report_embedded():
    s = new_session("session.demo", "intent.demo")
    r = consistency_report_for(s, verify_passed=True)
    assert CHECK_COMPLETENESS in r.blocking_reasons
    assert r.passed is False
