"""Tests for the IntentSession state machine + entry gates (Stage 7-2.2).

The state machine in ``tm/intent/session.py`` is a pure, deterministic,
zero-LLM transition layer over the v0.4 ``IntentSession`` artifact. It reuses
the frozen design-loop contract (``DesignStep`` / ``ENTRY_GATES`` /
``HUMAN_ONLY_STEPS``) from ``tm/intent/design_loop.py`` and must:

- advance only when the deterministic entry gate is satisfied,
- enforce human-only steps (accept / seal),
- require a human ``sign_off`` to seal,
- support reverts with a logged reason,
- treat sealed sessions as read-only,
- **complete the whole loop on the fake/rule-based path with provider-free
  turns** (Phase 7 pause-condition invariant: LLM never required to advance),
- be deterministic (same inputs ⇒ byte-identical body) and yield a body that
  passes ``tm.artifacts.verify``.
"""

from __future__ import annotations

import pytest

from tm.artifacts import Artifact, ArtifactEnvelope, ArtifactStatus, ArtifactType, verify
from tm.artifacts.hash import body_hash
from tm.artifacts.models import IntentSessionSignOff
from tm.artifacts.normalize import normalize_body
from tm.intent.design_loop import DESIGN_STEP_ORDER, DesignStep, SessionStatus
from tm.intent.session import (
    GateFacts,
    SessionTransitionError,
    advance,
    new_session,
    next_step,
    revert,
)

_ALL_OPEN = GateFacts(
    completeness_no_error=True,
    verify_passed=True,
    human_signoff_and_all_dims_closed=True,
)


def _sign_off() -> IntentSessionSignOff:
    return IntentSessionSignOff(signer="human:pm@example.com", scope=["pattern.cand.v1"])


def _to_dict(body) -> dict:
    """Round-trip the body through the artifact loader for verify()."""
    # The body dataclass mirrors the schema field-for-field; build the raw dict.
    raw: dict = {
        "session_id": body.session_id,
        "root_intent_ref": body.root_intent_ref,
        "status": body.status,
        "current_step": body.current_step,
        "turns": [
            {k: v for k, v in {
                "seq": t.seq,
                "role": t.role,
                "action": t.action,
                "input_ref": t.input_ref,
                "output_ref": t.output_ref,
                "provider": t.provider,
                "turn_hash": t.turn_hash,
            }.items() if v is not None}
            for t in body.turns
        ],
        "produced_refs": body.produced_refs,
        "metadata": body.metadata,
    }
    if body.completeness is not None:
        raw["completeness"] = body.completeness
    if body.sign_off is not None:
        so = body.sign_off
        raw["sign_off"] = {k: v for k, v in {
            "signer": so.signer,
            "scope": so.scope,
            "completeness_snapshot": so.completeness_snapshot,
            "dispositions": so.dispositions,
            "gate_report_hash": so.gate_report_hash,
            "signed_at": so.signed_at,
            "sign_hash": so.sign_hash,
        }.items() if v not in (None, [], {})}
    return raw


# ─── basic structure ────────────────────────────────────────────────


def test_new_session_starts_at_draft_working() -> None:
    s = new_session("session.demo", "intent.demo")
    assert s.current_step == DesignStep.DRAFT.value
    assert s.status == SessionStatus.WORKING.value
    assert s.turns == []


def test_next_step_follows_design_order_then_none() -> None:
    assert next_step(DesignStep.DRAFT) == DesignStep.CHECK_5W1H
    assert next_step(DESIGN_STEP_ORDER[-1]) is None


# ─── ungated early steps ────────────────────────────────────────────


def test_advance_through_ungated_steps_without_facts() -> None:
    s = new_session("session.demo", "intent.demo")
    # draft → check_5w1h → propose → refine are ungated (no ENTRY_GATES entry).
    for expected in (DesignStep.CHECK_5W1H, DesignStep.PROPOSE, DesignStep.REFINE):
        s = advance(s, GateFacts(), role="agent")
        assert s.current_step == expected.value
    # each advance appended exactly one turn; seqs are monotonic from 0
    assert [t.seq for t in s.turns] == [0, 1, 2]


# ─── gate enforcement ───────────────────────────────────────────────


def _at_refine() -> "object":
    s = new_session("session.demo", "intent.demo")
    for _ in range(3):  # draft→check_5w1h→propose→refine
        s = advance(s, GateFacts(), role="agent")
    assert s.current_step == DesignStep.REFINE.value
    return s


def test_verify_gate_blocks_without_completeness() -> None:
    s = _at_refine()
    with pytest.raises(SessionTransitionError) as exc:
        advance(s, GateFacts(completeness_no_error=False), role="agent")
    assert exc.value.requirement == "completeness_no_error"


def test_verify_gate_opens_with_completeness() -> None:
    s = _at_refine()
    s = advance(s, GateFacts(completeness_no_error=True), role="agent")
    assert s.current_step == DesignStep.VERIFY.value


def test_accept_is_human_only() -> None:
    s = advance(_at_refine(), GateFacts(completeness_no_error=True), role="agent")  # at verify
    with pytest.raises(SessionTransitionError, match="human-only"):
        advance(s, GateFacts(verify_passed=True), role="agent")


def test_accept_gate_requires_verify_passed() -> None:
    s = advance(_at_refine(), GateFacts(completeness_no_error=True), role="agent")  # at verify
    with pytest.raises(SessionTransitionError) as exc:
        advance(s, GateFacts(verify_passed=False), role="human")
    assert exc.value.requirement == "verify_passed"


def test_seal_requires_sign_off() -> None:
    s = advance(_at_refine(), GateFacts(completeness_no_error=True), role="agent")  # verify
    s = advance(s, GateFacts(verify_passed=True), role="human")  # accept
    with pytest.raises(SessionTransitionError) as exc:
        advance(s, _ALL_OPEN, role="human")  # seal, no sign_off
    assert exc.value.requirement == "human_signoff_and_all_dims_closed"


# ─── full deterministic (fake-path) loop: Phase 7 pause condition ───


def _drive_full_loop():
    s = new_session("session.fairness", "intent.fairness")
    s = advance(s, GateFacts(), role="agent")  # check_5w1h
    s = advance(s, GateFacts(), role="agent")  # propose
    s = advance(s, GateFacts(), role="agent")  # refine
    s = advance(s, GateFacts(completeness_no_error=True), role="agent")  # verify
    s = advance(s, GateFacts(verify_passed=True), role="human")  # accept
    s = advance(s, _ALL_OPEN, role="human", sign_off=_sign_off())  # sealed
    return s


def test_fake_path_drives_entire_loop_to_sealed() -> None:
    s = _drive_full_loop()
    assert s.current_step == DesignStep.SEALED.value
    assert s.status == SessionStatus.SEALED.value
    assert s.sign_off is not None
    # no turn was produced by an LLM provider — progress never depended on one
    assert all(t.provider is None for t in s.turns)


def test_full_loop_is_deterministic() -> None:
    a = _drive_full_loop()
    b = _drive_full_loop()
    assert _to_dict(a) == _to_dict(b)
    assert body_hash(_to_dict(a)) == body_hash(_to_dict(b))


def test_resulting_body_passes_artifact_verify() -> None:
    raw = _to_dict(_drive_full_loop())
    env = ArtifactEnvelope(
        artifact_id="session.fairness",
        status=ArtifactStatus.CANDIDATE,
        artifact_type=ArtifactType.INTENT_SESSION,
        version="v0.4",
        created_by="tester",
        created_at="2026-06-13T00:00:00Z",
        body_hash="",
        envelope_hash="",
        meta={},
    )
    from tm.artifacts.models import IntentSessionBody

    artifact = Artifact(envelope=env, body=IntentSessionBody.from_mapping(raw), body_raw=raw)
    accepted, report = verify(artifact)
    assert accepted is not None, report.errors


# ─── revert + read-only sealed ──────────────────────────────────────


def test_revert_moves_back_and_logs_reason() -> None:
    s = _at_refine()
    s = revert(s, DesignStep.CHECK_5W1H, role="human", reason="goal changed")
    assert s.current_step == DesignStep.CHECK_5W1H.value
    last = s.turns[-1]
    assert last.action == "note"
    assert "goal changed" in (last.output_ref or "")


def test_revert_forward_is_rejected() -> None:
    s = _at_refine()
    with pytest.raises(SessionTransitionError, match="strictly earlier"):
        revert(s, DesignStep.VERIFY, role="human", reason="x")


def test_revert_requires_reason() -> None:
    s = _at_refine()
    with pytest.raises(SessionTransitionError, match="reason"):
        revert(s, DesignStep.DRAFT, role="human", reason="  ")


def test_sealed_session_is_read_only() -> None:
    s = _drive_full_loop()
    with pytest.raises(SessionTransitionError, match="read-only"):
        advance(s, _ALL_OPEN, role="human")
    with pytest.raises(SessionTransitionError, match="read-only"):
        revert(s, DesignStep.DRAFT, role="human", reason="too late")


def test_invalid_role_rejected() -> None:
    s = new_session("session.demo", "intent.demo")
    with pytest.raises(SessionTransitionError, match="role"):
        advance(s, GateFacts(), role="robot")


def test_advance_does_not_mutate_input() -> None:
    s = new_session("session.demo", "intent.demo")
    s2 = advance(s, GateFacts(), role="agent")
    assert s.current_step == DesignStep.DRAFT.value  # original untouched
    assert s.turns == []
    assert s2 is not s


def test_normalize_body_accepts_session_dict() -> None:
    # sanity: the produced raw dict normalizes (canonical form) without error
    raw = _to_dict(_drive_full_loop())
    assert normalize_body(raw) is not None
