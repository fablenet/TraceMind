"""Task 7-5.0 — design-loop contract is frozen + Parity Rule is machine-checkable.

This pins the single source of truth that Stage 7-2 (IntentSession) and Stage
7-5 (workbench / parity CI) build on. Changing the frozen vocabularies here is
a deliberate contract change, not an accident.
"""

from __future__ import annotations

import itertools

from tm.intent.design_loop import (
    DESIGN_STEP_ORDER,
    ENTRY_GATES,
    HUMAN_ONLY_STEPS,
    PARITY_MATRIX,
    SESSION_LIFECYCLE,
    DesignStep,
    SessionStatus,
    TurnAction,
    entry_gate_for,
)


def test_design_step_vocabulary_frozen() -> None:
    # workbench-api §8 Open #3 / Stage 7-2 §3
    assert {s.value for s in DesignStep} == {
        "draft",
        "check_5w1h",
        "propose",
        "refine",
        "verify",
        "accept",
        "sealed",
    }


def test_turn_action_vocabulary_frozen() -> None:
    # Stage 7-2 §2 Turn.action enum
    assert {a.value for a in TurnAction} == {
        "propose",
        "refine",
        "check_5w1h",
        "verify",
        "accept",
        "clarify",
        "note",
    }


def test_session_status_frozen() -> None:
    assert {s.value for s in SessionStatus} == {"working", "sealed"}


def test_step_order_covers_every_step_once() -> None:
    assert set(DESIGN_STEP_ORDER) == set(DesignStep)
    assert len(DESIGN_STEP_ORDER) == len(set(DESIGN_STEP_ORDER))


def test_human_only_steps_are_accept_and_seal() -> None:
    # invariant 5: AI only proposes; accept & seal must be human-triggered
    assert HUMAN_ONLY_STEPS == frozenset({DesignStep.ACCEPT, DesignStep.SEALED})


def test_entry_gates_reference_valid_steps_and_are_unique() -> None:
    gated_steps = [g.step for g in ENTRY_GATES]
    assert len(gated_steps) == len(set(gated_steps)), "duplicate gate for a step"
    for gate in ENTRY_GATES:
        assert gate.step in DesignStep
        assert gate.requirement, "gate requirement token must be non-empty"
        assert gate.rationale
    # verify/accept/sealed are the gated steps
    assert set(gated_steps) == {DesignStep.VERIFY, DesignStep.ACCEPT, DesignStep.SEALED}
    assert entry_gate_for(DesignStep.VERIFY) is not None
    assert entry_gate_for(DesignStep.DRAFT) is None


def test_parity_rows_have_api_cli_and_equivalence() -> None:
    # Parity Rule: every workbench action ⇒ one API + one CLI + an equivalence
    for entry in itertools.chain(PARITY_MATRIX, SESSION_LIFECYCLE):
        assert entry.api.strip(), f"{entry.action}: missing API"
        assert entry.cli.strip(), f"{entry.action}: missing CLI"
        assert entry.equivalence.strip(), f"{entry.action}: missing equivalence"
        assert entry.api.startswith("GET ") or entry.api.startswith("POST "), entry.api


def test_parity_actions_unique() -> None:
    actions = [e.action for e in itertools.chain(PARITY_MATRIX, SESSION_LIFECYCLE)]
    assert len(actions) == len(set(actions))


def test_human_gate_rows_match_human_only_steps() -> None:
    # accept + seal parity rows are the human-gated ones
    human_gate_steps = {e.step for e in PARITY_MATRIX if e.human_gate}
    assert human_gate_steps == HUMAN_ONLY_STEPS


def test_every_non_meta_turn_action_has_a_design_step_or_lifecycle() -> None:
    # propose/refine/check_5w1h/verify/accept map to steps; clarify is lifecycle;
    # note is free-form journal. Guards against vocabulary drift.
    step_values = {s.value for s in DesignStep}
    for action in TurnAction:
        if action in (TurnAction.NOTE, TurnAction.CLARIFY):
            continue
        assert action.value in step_values, f"{action} has no corresponding DesignStep"
