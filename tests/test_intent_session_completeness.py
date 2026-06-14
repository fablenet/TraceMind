"""5W1H completeness embedded into IntentSession (Stage 7-2.3).

``tm/intent/session.py`` now runs the deterministic 5W1H check (``mode=design``)
and embeds the canonical report into ``session.completeness``. The
``completeness_no_error`` entry-gate fact for advancing into ``verify`` is then
*derived from the embedded report* — no longer hand-fed, still zero-LLM.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tm.artifacts import Artifact, ArtifactEnvelope, ArtifactStatus, ArtifactType, verify
from tm.artifacts.models import IntentSessionBody
from tm.intent.design_loop import DesignStep
from tm.intent.session import (
    GateFacts,
    SessionTransitionError,
    advance,
    completeness_no_error,
    embed_completeness,
    gate_facts_for,
    new_session,
)


def _intent(**overrides) -> dict:
    base = {
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
    base.update(overrides)
    return base


def _write_intent(tmp_path: Path, data: dict) -> Path:
    p = tmp_path / "intent.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


def _at_refine_with_completeness(tmp_path: Path, intent: dict) -> IntentSessionBody:
    s = new_session("session.demo", "intent.demo")
    for _ in range(3):  # draft → check_5w1h → propose → refine
        s = advance(s, GateFacts(), role="agent")
    intent_path = _write_intent(tmp_path, intent)
    return embed_completeness(s, intent_path=intent_path, profile="base")


# ─── embedding ──────────────────────────────────────────────────────


def test_embed_writes_report_into_session(tmp_path: Path) -> None:
    s = new_session("session.demo", "intent.demo")
    s = embed_completeness(s, intent_path=_write_intent(tmp_path, _intent()), profile="base")
    assert isinstance(s.completeness, dict)
    assert s.completeness["mode"] == "design"
    assert s.completeness["dimensions"]["who"]["status"] == "satisfied"


def test_embed_does_not_mutate_input(tmp_path: Path) -> None:
    s = new_session("session.demo", "intent.demo")
    s2 = embed_completeness(s, intent_path=_write_intent(tmp_path, _intent()), profile="base")
    assert s.completeness is None
    assert s2 is not s


def test_embed_is_deterministic(tmp_path: Path) -> None:
    p = _write_intent(tmp_path, _intent())
    a = embed_completeness(new_session("s", "i"), intent_path=p, profile="base")
    b = embed_completeness(new_session("s", "i"), intent_path=p, profile="base")
    assert a.completeness == b.completeness


def test_embed_refused_on_sealed(tmp_path: Path) -> None:
    sealed = IntentSessionBody(
        session_id="s",
        root_intent_ref="i",
        status="sealed",
        current_step="sealed",
        turns=[],
    )
    with pytest.raises(SessionTransitionError, match="read-only"):
        embed_completeness(sealed, intent_path=_write_intent(tmp_path, _intent()), profile="base")


# ─── derived gate fact ──────────────────────────────────────────────


def test_no_report_means_gate_not_satisfied() -> None:
    assert completeness_no_error(new_session("s", "i")) is False


def test_complete_intent_opens_gate(tmp_path: Path) -> None:
    s = _at_refine_with_completeness(tmp_path, _intent())
    assert completeness_no_error(s) is True
    # advancing into verify now succeeds using the *derived* fact
    s = advance(s, gate_facts_for(s), role="agent")
    assert s.current_step == DesignStep.VERIFY.value


def test_missing_error_dimension_blocks_gate(tmp_path: Path) -> None:
    # actors=[] → Who (error severity) is missing → blocking error in design mode
    s = _at_refine_with_completeness(tmp_path, _intent(actors=[]))
    assert completeness_no_error(s) is False
    with pytest.raises(SessionTransitionError) as exc:
        advance(s, gate_facts_for(s), role="agent")
    assert exc.value.requirement == "completeness_no_error"


def test_partial_error_dim_tolerated_in_design_opens_gate(tmp_path: Path) -> None:
    # context="" → Why partial; design mode tolerates partials (warning, not error)
    s = _at_refine_with_completeness(tmp_path, _intent(context=""))
    assert s.completeness["dimensions"]["why"]["status"] == "partial"
    assert completeness_no_error(s) is True


# ─── artifact-level sanity ──────────────────────────────────────────


def test_session_with_embedded_completeness_verifies(tmp_path: Path) -> None:
    s = _at_refine_with_completeness(tmp_path, _intent())
    raw = {
        "session_id": s.session_id,
        "root_intent_ref": s.root_intent_ref,
        "status": s.status,
        "current_step": s.current_step,
        "turns": [
            {"seq": t.seq, "role": t.role, "action": t.action}
            for t in s.turns
        ],
        "completeness": s.completeness,
    }
    env = ArtifactEnvelope(
        artifact_id="session.demo",
        status=ArtifactStatus.CANDIDATE,
        artifact_type=ArtifactType.INTENT_SESSION,
        version="v0.4",
        created_by="tester",
        created_at="2026-06-13T00:00:00Z",
        body_hash="",
        envelope_hash="",
        meta={},
    )
    artifact = Artifact(envelope=env, body=IntentSessionBody.from_mapping(raw), body_raw=raw)
    accepted, report = verify(artifact)
    assert accepted is not None, report.errors
