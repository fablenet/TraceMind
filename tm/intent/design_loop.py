"""Design-loop contract — Phase 7 Task 7-5.0 (codified).

This is the **single source of truth** for the design workbench contract that
both the IntentSession state machine (Stage 7-2) and the workbench / parity CI
(Stage 7-5) import. It freezes, in code:

- ``DesignStep`` — the design-loop step enum (``current_step``), resolving
  workbench-api §8 Open #3.
- ``TurnAction`` — the session journal action vocabulary (Stage 7-2 §2).
- ``SessionStatus`` — working / sealed.
- ``ENTRY_GATES`` — deterministic preconditions to *enter* gated steps.
- ``HUMAN_ONLY_STEPS`` — steps that must be human-triggered (invariant 5).
- ``PARITY_MATRIX`` / ``SESSION_LIFECYCLE`` — every workbench action mapped to
  one API + one CLI, so the Parity Rule is machine-checkable (Stage 7-5.2).

Hard rules: this module is **pure, deterministic, zero-LLM** — only constants
and dataclasses. It declares the contract; it does not implement the steps.

Spec: ``docs/specs/design-workbench-api-v0_1.md``.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class DesignStep(str, Enum):
    """Where a design session is parked in the loop (``current_step``).

    Frozen vocabulary (workbench-api §8 Open #3 / Stage 7-2 §3).
    """

    DRAFT = "draft"
    CHECK_5W1H = "check_5w1h"
    PROPOSE = "propose"
    REFINE = "refine"
    VERIFY = "verify"
    ACCEPT = "accept"
    SEALED = "sealed"


#: Recommended forward order of the design loop (revert may jump backwards).
DESIGN_STEP_ORDER: tuple[DesignStep, ...] = (
    DesignStep.DRAFT,
    DesignStep.CHECK_5W1H,
    DesignStep.PROPOSE,
    DesignStep.REFINE,
    DesignStep.VERIFY,
    DesignStep.ACCEPT,
    DesignStep.SEALED,
)


class TurnAction(str, Enum):
    """Journal action vocabulary for ``IntentSession.turns[].action`` (7-2 §2)."""

    PROPOSE = "propose"
    REFINE = "refine"
    CHECK_5W1H = "check_5w1h"
    VERIFY = "verify"
    ACCEPT = "accept"
    CLARIFY = "clarify"
    NOTE = "note"


class SessionStatus(str, Enum):
    """IntentSession lifecycle status (7-2 §2)."""

    WORKING = "working"
    SEALED = "sealed"


@dataclass(frozen=True)
class StepGate:
    """A deterministic precondition that must hold to *enter* ``step``.

    ``requirement`` is a stable machine token the session advance-gate (7-2)
    and the consistency gate (7-0.8) resolve against deterministic checks.
    """

    step: DesignStep
    requirement: str
    rationale: str


#: Entry gates (7-2 §3). Deterministic; advance is refused unless satisfied.
ENTRY_GATES: tuple[StepGate, ...] = (
    StepGate(
        DesignStep.VERIFY,
        "completeness_no_error",
        "5W1H check (7-0) has no error-severity gaps before formal verification",
    ),
    StepGate(
        DesignStep.ACCEPT,
        "verify_passed",
        "formal verification verdict (7-V) holds before human accept",
    ),
    StepGate(
        DesignStep.SEALED,
        "human_signoff_and_all_dims_closed",
        "human sign_off + every required dim resolved/waived/dynamic (5w1h §3b)",
    ),
)


#: Steps that MUST be triggered by a human (invariant 5: AI only proposes).
HUMAN_ONLY_STEPS: frozenset[DesignStep] = frozenset(
    {DesignStep.ACCEPT, DesignStep.SEALED}
)


@dataclass(frozen=True)
class ParityEntry:
    """One workbench action ⇒ exactly one API + one CLI (Parity Rule).

    ``equivalence`` states what ``test_workbench_cli_parity.py`` (7-5.2) must
    assert for this row. ``human_gate=True`` rows only parity-check the *action*
    surface (API/CLI exist), never the human decision itself.
    """

    action: str
    api: str
    cli: str
    equivalence: str
    step: DesignStep | None = None
    human_gate: bool = False


#: §4 deliverable rows: artifact-producing design-loop actions.
PARITY_MATRIX: tuple[ParityEntry, ...] = (
    ParityEntry(
        "draft intent",
        "POST /api/v1/intents",
        "tm intents new",
        "produced IntentBody is byte-identical",
        DesignStep.DRAFT,
    ),
    ParityEntry(
        "check 5w1h",
        "POST /api/v1/intents/{id}:check-5w1h",
        "tm intents check-5w1h",
        "CompletenessReport canonical-json equal",
        DesignStep.CHECK_5W1H,
    ),
    ParityEntry(
        "propose candidates",
        "POST /api/v1/patterns:propose",
        "tm pattern propose",
        "candidates equal for same provider/seed",
        DesignStep.PROPOSE,
    ),
    ParityEntry(
        "refine candidates",
        "POST /api/v1/patterns:refine",
        "tm intent chat",
        "same input sequence ⇒ same proposal",
        DesignStep.REFINE,
    ),
    ParityEntry(
        "instantiate + compile",
        "POST /api/v1/patterns:instantiate",
        "tm pattern instantiate",
        "PatternInstance byte-identical",
        DesignStep.PROPOSE,
    ),
    ParityEntry(
        "verify",
        "POST /api/v1/verify:network",
        "tm verify network",
        "VerifyReport verdict equal",
        DesignStep.VERIFY,
    ),
    ParityEntry(
        "accept",
        "POST /api/v1/artifacts/{id}:accept",
        "tm proposal / tm artifacts verify",
        "accepted artifact equal (action surface only; human decision exempt)",
        DesignStep.ACCEPT,
        human_gate=True,
    ),
    ParityEntry(
        "seal",
        "POST /api/v1/sessions/{id}:seal",
        "tm intent session seal",
        "sealed session: sign_off present + all dims closed (action surface only)",
        DesignStep.SEALED,
        human_gate=True,
    ),
)


#: §3 session-lifecycle actions (orchestration; state lives in the artifact).
SESSION_LIFECYCLE: tuple[ParityEntry, ...] = (
    ParityEntry(
        "new session",
        "POST /api/v1/sessions",
        "tm intent chat --new --intent {id}",
        "session bound to a root intent",
    ),
    ParityEntry(
        "show / current step",
        "GET /api/v1/sessions/{id}",
        "tm intent session show {id}",
        "returns current_step + completeness snapshot",
    ),
    ParityEntry(
        "advance",
        "POST /api/v1/sessions/{id}:advance",
        "tm intent session advance",
        "advances current_step iff entry gate satisfied",
    ),
    ParityEntry(
        "revert",
        "POST /api/v1/sessions/{id}:revert",
        "tm intent session revert",
        "moves current_step back with a logged reason",
    ),
    ParityEntry(
        "clarify",
        "POST /api/v1/sessions/{id}:clarify",
        "tm intent session clarify",
        "soft-warning disposition recorded in journal (action=clarify)",
    ),
    ParityEntry(
        "resume",
        "GET /api/v1/sessions/{id}",
        "tm intent session resume {id}",
        "resumes from artifact state (no frontend persistence)",
    ),
)


def entry_gate_for(step: DesignStep) -> StepGate | None:
    """Return the deterministic entry gate for ``step``, or None if ungated."""
    for gate in ENTRY_GATES:
        if gate.step is step:
            return gate
    return None


__all__ = [
    "DESIGN_STEP_ORDER",
    "ENTRY_GATES",
    "HUMAN_ONLY_STEPS",
    "PARITY_MATRIX",
    "SESSION_LIFECYCLE",
    "DesignStep",
    "ParityEntry",
    "SessionStatus",
    "StepGate",
    "TurnAction",
    "entry_gate_for",
]
