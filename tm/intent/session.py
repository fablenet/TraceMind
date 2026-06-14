"""IntentSession state machine + entry gates — Phase 7 Stage 7-2.2.

A **pure, deterministic, zero-LLM** transition layer over the
``IntentSessionBody`` artifact (K-Ontology v0.4, Stage 7-2.1). It advances /
reverts the design-loop ``current_step`` and appends the append-only ``turns``
journal, refusing any transition whose deterministic entry gate is not
satisfied.

It does **not** redefine the design-loop vocabulary: ``DesignStep`` /
``TurnAction`` / ``SessionStatus`` / ``ENTRY_GATES`` / ``HUMAN_ONLY_STEPS`` are
imported from the frozen contract in :mod:`tm.intent.design_loop` (Task 7-5.0).

Phase 7 pause-condition invariant
---------------------------------
Every "advance to the next step" transition MUST be drivable by the *fake* /
rule-based path alone — the LLM only improves candidate quality, it is never
required to make progress. Concretely: :func:`advance` consumes a deterministic
:class:`GateFacts` (booleans the caller computes from 5W1H / verify / closure)
and never imports or calls any LLM machinery. ``tests/test_intent_session_state_machine.py``
asserts the full ``draft → … → sealed`` loop completes with provider-free turns.

The *computation* of those facts (5W1H integration → 7-2.3, verify verdict →
7-V, full uncertainty closure → 7-2.8) lives elsewhere; this module only
resolves and enforces the gates against the facts it is given.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Dict, Mapping

from tm.artifacts.hash import body_hash
from tm.artifacts.models import (
    IntentSessionBody,
    IntentSessionSignOff,
    IntentSessionTurn,
)
from tm.intent.clarify import (
    CLARIFY_DISPOSITIONS,
    ClarificationQuestion,
    disposition_requires_reason,
)
from tm.intent.consistency_gate import (
    GateReport,
    SoftWarning,
    evaluate_consistency,
)

from .completeness import Dimension, Mode, Profile, compute_5w1h_completeness
from .uncertainty import Disposition, DispositionKind
from .design_loop import (
    DESIGN_STEP_ORDER,
    HUMAN_ONLY_STEPS,
    DesignStep,
    SessionStatus,
    StepGate,
    TurnAction,
    entry_gate_for,
)


class SessionTransitionError(ValueError):
    """Raised when a requested transition violates the design-loop contract.

    ``requirement`` carries the unsatisfied gate token (when the refusal is a
    gate failure) so callers / API layers can surface a stable machine reason.
    """

    def __init__(self, message: str, *, requirement: str | None = None) -> None:
        super().__init__(message)
        self.requirement = requirement


@dataclass(frozen=True)
class GateFacts:
    """Deterministic truth values the advance-gate resolves against.

    The field names are exactly the ``StepGate.requirement`` tokens declared in
    :data:`tm.intent.design_loop.ENTRY_GATES`, so :meth:`satisfies` is a plain
    attribute lookup — keeping the gate machine-checkable and drift-proof.
    """

    completeness_no_error: bool = False
    verify_passed: bool = False
    human_signoff_and_all_dims_closed: bool = False

    def satisfies(self, gate: StepGate) -> bool:
        return bool(getattr(self, gate.requirement, False))


#: Which journal action a transition *into* a step records. Steps with no
#: natural design action (``draft`` is the initial state; ``sealed`` is a
#: lifecycle marker carried by status + sign_off) are logged as ``note``.
_STEP_ENTRY_ACTION: dict[DesignStep, TurnAction] = {
    DesignStep.CHECK_5W1H: TurnAction.CHECK_5W1H,
    DesignStep.PROPOSE: TurnAction.PROPOSE,
    DesignStep.REFINE: TurnAction.REFINE,
    DesignStep.VERIFY: TurnAction.VERIFY,
    DesignStep.ACCEPT: TurnAction.ACCEPT,
    DesignStep.SEALED: TurnAction.NOTE,
}

_HUMAN = "human"
_AGENT = "agent"
_VALID_ROLES = frozenset({_HUMAN, _AGENT})


def new_session(session_id: str, root_intent_ref: str) -> IntentSessionBody:
    """Create a fresh ``working`` session parked at ``draft`` with no turns."""
    return IntentSessionBody(
        session_id=session_id,
        root_intent_ref=root_intent_ref,
        status=SessionStatus.WORKING.value,
        current_step=DesignStep.DRAFT.value,
        turns=[],
    )


def _current_step(body: IntentSessionBody) -> DesignStep:
    return DesignStep(body.current_step)


def next_step(step: DesignStep) -> DesignStep | None:
    """Return the next step in :data:`DESIGN_STEP_ORDER`, or None at the end."""
    idx = DESIGN_STEP_ORDER.index(step)
    if idx + 1 >= len(DESIGN_STEP_ORDER):
        return None
    return DESIGN_STEP_ORDER[idx + 1]


def _next_seq(body: IntentSessionBody) -> int:
    if not body.turns:
        return 0
    return max(turn.seq for turn in body.turns) + 1


#: Genesis link of the per-session journal hash chain (Stage 7-2.6).
GENESIS_PREV_HASH = ""


def _turn_payload(
    *,
    seq: int,
    role: str,
    action: str,
    input_ref: str | None,
    output_ref: str | None,
    provider: str | None,
    prev_hash: str,
) -> Dict[str, Any]:
    """Canonical content of a turn that the chain hash commits to.

    Includes ``prev_hash`` so the journal is a tamper-evident hash chain: every
    turn binds to its predecessor. ``turn_hash`` itself is excluded (it *is* the
    commitment). Deterministic — same content + same predecessor ⇒ same hash.
    """
    return {
        "seq": seq,
        "role": role,
        "action": action,
        "input_ref": input_ref,
        "output_ref": output_ref,
        "provider": provider,
        "prev_hash": prev_hash,
    }


def turn_content_hash(turn: IntentSessionTurn, prev_hash: str) -> str:
    """Canonical hash of ``turn``'s content chained to ``prev_hash``."""
    return body_hash(
        _turn_payload(
            seq=turn.seq,
            role=turn.role,
            action=turn.action,
            input_ref=turn.input_ref,
            output_ref=turn.output_ref,
            provider=turn.provider,
            prev_hash=prev_hash,
        )
    )


def _append_turn(
    body: IntentSessionBody,
    *,
    role: str,
    action: TurnAction,
    input_ref: str | None = None,
    output_ref: str | None = None,
    provider: str | None = None,
) -> list[IntentSessionTurn]:
    seq = _next_seq(body)
    prev_hash = (body.turns[-1].turn_hash if body.turns else None) or GENESIS_PREV_HASH
    turn_hash = body_hash(
        _turn_payload(
            seq=seq,
            role=role,
            action=action.value,
            input_ref=input_ref,
            output_ref=output_ref,
            provider=provider,
            prev_hash=prev_hash,
        )
    )
    turn = IntentSessionTurn(
        seq=seq,
        role=role,
        action=action.value,
        input_ref=input_ref,
        output_ref=output_ref,
        provider=provider,
        turn_hash=turn_hash,
    )
    return [*body.turns, turn]


def verify_journal(body: IntentSessionBody) -> list[str]:
    """Recompute the journal hash chain and report any break (Stage 7-2.6).

    Returns a list of human-readable issues; an empty list means the journal is
    intact (every ``turn_hash`` matches its canonical content chained to its
    predecessor). Tampering with any turn's content — or reordering / dropping a
    turn — surfaces here and propagates to all later turns, which is exactly the
    audit-chain property Phase 8 zero-trust signatures will sign over (the chain
    head ``turns[-1].turn_hash`` is the Phase-8 signing target).
    """
    issues: list[str] = []
    prev_hash = GENESIS_PREV_HASH
    for idx, turn in enumerate(body.turns):
        expected = turn_content_hash(turn, prev_hash)
        if turn.turn_hash is None:
            issues.append(f"turns[{idx}] (seq={turn.seq}) missing turn_hash")
        elif turn.turn_hash != expected:
            issues.append(
                f"turns[{idx}] (seq={turn.seq}) hash mismatch: "
                f"expected {expected[:12]}…, got {str(turn.turn_hash)[:12]}…"
            )
        prev_hash = turn.turn_hash if turn.turn_hash is not None else expected
    return issues


def _require_role(role: str) -> None:
    if role not in _VALID_ROLES:
        raise SessionTransitionError(f"role must be one of {sorted(_VALID_ROLES)}, got '{role}'")


def _require_working(body: IntentSessionBody) -> None:
    if body.status != SessionStatus.WORKING.value:
        raise SessionTransitionError("session is sealed and read-only; transitions are not allowed")


def advance(
    body: IntentSessionBody,
    facts: GateFacts,
    *,
    role: str,
    sign_off: IntentSessionSignOff | None = None,
    note: str | None = None,
) -> IntentSessionBody:
    """Advance ``current_step`` to the next step iff its entry gate is satisfied.

    Refuses (raising :class:`SessionTransitionError`) when:

    - the session is already ``sealed`` (read-only),
    - the loop is already at the terminal step,
    - the target step is human-only (``accept`` / ``seal``) and ``role != 'human'``
      (invariant 5: AI only proposes),
    - the target step's deterministic entry gate is not satisfied by ``facts``,
    - the target is ``sealed`` without an accompanying ``sign_off`` (the tool
      does not seal on its own; the human signs off — full closure in 7-2.8).

    Returns a **new** ``IntentSessionBody`` (the input is never mutated) with the
    advanced step, an appended journal turn, and — when sealing — ``status=sealed``
    plus the attached ``sign_off``.
    """
    _require_role(role)
    _require_working(body)

    current = _current_step(body)
    target = next_step(current)
    if target is None:
        raise SessionTransitionError(f"already at terminal step '{current.value}'; cannot advance")

    if target in HUMAN_ONLY_STEPS and role != _HUMAN:
        raise SessionTransitionError(
            f"step '{target.value}' is human-only and must be triggered by a human (invariant 5), "
            f"got role='{role}'"
        )

    gate = entry_gate_for(target)
    if gate is not None and not facts.satisfies(gate):
        raise SessionTransitionError(
            f"cannot advance to '{target.value}': entry gate '{gate.requirement}' not satisfied "
            f"({gate.rationale})",
            requirement=gate.requirement,
        )

    new_sign_off = body.sign_off
    new_status = body.status
    if target is DesignStep.SEALED:
        if sign_off is None and body.sign_off is None:
            raise SessionTransitionError(
                "sealing requires a human 'sign_off'; the tool does not seal on its own",
                requirement="human_signoff_and_all_dims_closed",
            )
        if sign_off is not None:
            new_sign_off = sign_off
        new_status = SessionStatus.SEALED.value

    action = _STEP_ENTRY_ACTION[target]
    provider = None  # transitions are provider-free; LLM never required to advance
    turns = _append_turn(body, role=role, action=action, output_ref=note, provider=provider)
    return replace(body, current_step=target.value, status=new_status, sign_off=new_sign_off, turns=turns)


def revert(
    body: IntentSessionBody,
    to_step: DesignStep,
    *,
    role: str,
    reason: str,
) -> IntentSessionBody:
    """Move ``current_step`` back to an earlier step, logging the reason.

    Reverts are always allowed within a ``working`` session (to any strictly
    earlier step) and require a non-empty ``reason`` recorded in the journal.
    Sealed sessions are read-only.
    """
    _require_role(role)
    _require_working(body)
    if not reason or not reason.strip():
        raise SessionTransitionError("revert requires a non-empty 'reason' (recorded in the journal)")

    current = _current_step(body)
    current_idx = DESIGN_STEP_ORDER.index(current)
    target_idx = DESIGN_STEP_ORDER.index(to_step)
    if target_idx >= current_idx:
        raise SessionTransitionError(
            f"revert target '{to_step.value}' must be strictly earlier than current '{current.value}'"
        )

    turns = _append_turn(body, role=role, action=TurnAction.NOTE, output_ref=f"revert: {reason}")
    return replace(body, current_step=to_step.value, turns=turns)


# ─── ambiguity clarification (Stage 7-2.7, §4b) ─────────────────────


def _clarifications(body: IntentSessionBody) -> list[dict[str, Any]]:
    existing = body.metadata.get("clarifications") if isinstance(body.metadata, dict) else None
    return list(existing) if isinstance(existing, list) else []


def clarify(
    body: IntentSessionBody,
    *,
    disposition: str,
    role: str = _HUMAN,
    question: "ClarificationQuestion | Mapping[str, Any] | None" = None,
    warning_ref: str | None = None,
    reason: str = "",
    resolution: str = "",
) -> IntentSessionBody:
    """Record a human disposition of a soft warning into the journal (§4b).

    Appends an ``action=clarify`` turn and stores the full accountable record
    (warning, question candidate, disposition, reason, resolution) under
    ``metadata.clarifications`` with a content ``record_hash``. ``clarify`` does
    **not** change ``current_step`` — it annotates the current step so a soft
    warning is never silently swallowed: it is either consciously confirmed
    harmless (``confirmed_distinct`` requires a ``reason``) or turned into a real
    supplement / constraint amendment. Returns a new body; sealed sessions refuse.
    """
    _require_role(role)
    _require_working(body)
    if disposition not in CLARIFY_DISPOSITIONS:
        raise SessionTransitionError(
            f"unknown clarify disposition '{disposition}'; one of {sorted(CLARIFY_DISPOSITIONS)}"
        )
    if disposition_requires_reason(disposition) and not reason.strip():
        raise SessionTransitionError(
            f"clarify disposition '{disposition}' requires a recorded reason (accountability)"
        )

    if isinstance(question, ClarificationQuestion):
        q_dict: dict[str, Any] | None = question.to_dict()
    elif isinstance(question, Mapping):
        q_dict = dict(question)
    else:
        q_dict = None

    clars = _clarifications(body)
    clar_id = f"clar.{len(clars)}"
    record: dict[str, Any] = {
        "clar_id": clar_id,
        "turn_seq": _next_seq(body),
        "warning_ref": warning_ref,
        "question": q_dict,
        "disposition": disposition,
        "reason": reason or None,
        "resolution": resolution or None,
    }
    record["record_hash"] = body_hash({k: v for k, v in record.items() if k != "record_hash"})

    new_meta = dict(body.metadata) if isinstance(body.metadata, dict) else {}
    new_meta["clarifications"] = [*clars, record]

    turns = _append_turn(
        body,
        role=role,
        action=TurnAction.CLARIFY,
        input_ref=warning_ref,
        output_ref=clar_id,
    )
    return replace(body, metadata=new_meta, turns=turns)


def verify_clarifications(body: IntentSessionBody) -> list[str]:
    """Audit the clarification records: content hashes intact + journal-linked.

    Returns a list of issues (empty = sound): each record's ``record_hash`` must
    match its content, and each record must be referenced by a ``clarify`` turn
    (``output_ref == clar_id``). Complements :func:`verify_journal`.
    """
    issues: list[str] = []
    clar_turn_refs = {
        t.output_ref for t in body.turns if t.action == TurnAction.CLARIFY.value
    }
    for idx, record in enumerate(_clarifications(body)):
        clar_id = record.get("clar_id")
        expected = body_hash({k: v for k, v in record.items() if k != "record_hash"})
        if record.get("record_hash") != expected:
            issues.append(f"clarifications[{idx}] ({clar_id}) record_hash mismatch")
        if clar_id not in clar_turn_refs:
            issues.append(f"clarifications[{idx}] ({clar_id}) not linked to any clarify turn")
    return issues


# ─── 5W1H completeness integration (Stage 7-2.3) ────────────────────


def embed_completeness(
    body: IntentSessionBody,
    *,
    intent_path: Path,
    profile: str | Path | Profile = "base",
    plan_path: Path | None = None,
    network_path: Path | None = None,
    patterns_dir: Path | None = None,
) -> IntentSessionBody:
    """Run the deterministic 5W1H check and embed the report into the session.

    Uses ``mode="design"`` — the exploratory phase that tolerates partials and
    only counts hard ``missing`` on error-severity dimensions as blocking
    errors. The resulting canonical :class:`CompletenessOutcome.report` is
    written verbatim to ``body.completeness`` (a new body is returned; the input
    is never mutated). Refuses on a sealed (read-only) session.

    This closes the loop the state machine relies on: after embedding,
    :func:`completeness_no_error` derives the ``completeness_no_error`` entry-gate
    fact for advancing into ``verify`` straight from the embedded report — no
    LLM, fully deterministic.
    """
    _require_working(body)
    outcome = compute_5w1h_completeness(
        intent_path=Path(intent_path),
        profile=profile,
        plan_path=plan_path,
        network_path=network_path,
        mode=Mode.DESIGN,
        patterns_dir=patterns_dir,
    )
    return replace(body, completeness=dict(outcome.report))


def completeness_no_error(body: IntentSessionBody) -> bool:
    """Return the ``completeness_no_error`` entry-gate fact for this session.

    True iff a completeness report has been embedded (:func:`embed_completeness`)
    and it carries zero error-severity gaps. A session with no embedded report
    is conservatively *not* gate-satisfied (returns False).
    """
    report = body.completeness
    if not isinstance(report, dict):
        return False
    summary = report.get("summary")
    if not isinstance(summary, dict):
        return False
    errors = summary.get("errors")
    return isinstance(errors, int) and errors == 0


def gate_facts_for(
    body: IntentSessionBody,
    *,
    verify_passed: bool = False,
    human_signoff_and_all_dims_closed: bool = False,
) -> GateFacts:
    """Build :class:`GateFacts` deriving ``completeness_no_error`` from the
    embedded report; the verify/closure facts come from 7-V / 7-2.8 callers."""
    return GateFacts(
        completeness_no_error=completeness_no_error(body),
        verify_passed=verify_passed,
        human_signoff_and_all_dims_closed=human_signoff_and_all_dims_closed,
    )


def consistency_report_for(
    body: IntentSessionBody,
    *,
    verify_passed: bool | None = None,
    require_verify: bool = False,
    candidate_hash: str | None = None,
    existing_hashes: tuple[str, ...] = (),
    soft_warnings: tuple[SoftWarning, ...] = (),
) -> GateReport:
    """Build the unified design-time consistency report for this session.

    Bridges the embedded 5W1H report (:func:`embed_completeness`) into the
    deterministic gate (:func:`tm.intent.consistency_gate.evaluate_consistency`),
    deriving the completeness profile from the report. The verify verdict (7-V),
    candidate artifact hash + peer hashes (exact-duplicate), and any RAG soft
    warnings are caller-supplied — this module imports no LLM/heuristic machinery.
    """
    report = body.completeness if isinstance(body.completeness, dict) else None
    profile = str(report.get("profile")) if report else None
    return evaluate_consistency(
        completeness_report=report,
        completeness_profile=profile,
        verify_passed=verify_passed,
        require_verify=require_verify,
        body_hash=candidate_hash,
        existing_hashes=existing_hashes,
        soft_warnings=soft_warnings,
    )


# ─── accountable sign-off / seal closure (Stage 7-2.8, §4c) ─────────


def seal(
    body: IntentSessionBody,
    *,
    signer: str,
    intent_path: Path,
    profile: str | Path | Profile = "base",
    dispositions: Mapping[Dimension, Disposition] | None = None,
    verify_passed: bool = False,
    scope: list[str] | None = None,
    candidate_hash: str | None = None,
    existing_hashes: tuple[str, ...] = (),
    plan_path: Path | None = None,
    network_path: Path | None = None,
    patterns_dir: Path | None = None,
    signed_at: str | None = None,
) -> IntentSessionBody:
    """Seal the session with an accountable human ``sign_off`` (closure invariant).

    Enforces ``status=sealed`` ⟺ a human ``sign_off`` exists ∧ the consistency
    gate's **hard checks all pass** (5W1H seal-mode complete ∧ CTL ``verify_passed``
    ∧ no exact duplicate) ∧ **every required dimension is closed** — structurally
    ``resolved`` or accounted for in ``dispositions`` as ``waived`` / ``dynamic``
    (validated by :mod:`tm.intent.uncertainty`). The closure is *computed
    deterministically here* from a fresh ``mode=seal`` completeness recompute —
    it is never asserted by the caller (invariant 5 strengthened). The tool does
    not seal on its own; the human signs the requirement.

    Refuses (``SessionTransitionError``) when not at ``accept``, the signer is
    missing, or any part of the closure invariant is unmet. On success builds the
    ``sign_off`` (seal completeness snapshot incl. profile, ``waived``/``dynamic``
    closure records, ``gate_report_hash`` as hard-gate-pass evidence, ``sign_hash``
    Phase-8 placeholder) and advances into ``sealed``. Returns a new body.
    """
    _require_working(body)
    if not signer or not str(signer).strip():
        raise SessionTransitionError(
            "seal requires a human 'signer'; the tool does not seal on its own",
            requirement="human_signoff_and_all_dims_closed",
        )
    current = _current_step(body)
    if next_step(current) is not DesignStep.SEALED:
        raise SessionTransitionError(
            f"seal requires current step '{DesignStep.ACCEPT.value}', got '{current.value}'"
        )

    disp = dict(dispositions or {})
    outcome = compute_5w1h_completeness(
        intent_path=Path(intent_path),
        profile=profile,
        plan_path=plan_path,
        network_path=network_path,
        mode=Mode.SEAL,
        dispositions=disp,
        patterns_dir=patterns_dir,
    )
    seal_report = dict(outcome.report)
    all_dims_closed = seal_report.get("sealed") is True

    gate = evaluate_consistency(
        completeness_report=seal_report,
        completeness_profile=str(seal_report.get("profile")),
        verify_passed=verify_passed,
        require_verify=True,
        body_hash=candidate_hash,
        existing_hashes=existing_hashes,
    )

    if not (all_dims_closed and gate.passed):
        reasons = list(gate.blocking_reasons)
        if not all_dims_closed and "completeness" not in reasons:
            reasons.append("uncertainty_closure")
        raise SessionTransitionError(
            "cannot seal: closure invariant not met (" + ", ".join(reasons) + ")",
            requirement="human_signoff_and_all_dims_closed",
        )

    closure_dispositions = {
        dim.value: d.to_dict()
        for dim, d in disp.items()
        if d.kind in (DispositionKind.WAIVED, DispositionKind.DYNAMIC)
    }
    resolved_scope = list(scope) if scope is not None else list(body.produced_refs)
    gate_report_hash = body_hash(gate.to_dict())
    sign_off_core = {
        "signer": str(signer),
        "scope": resolved_scope,
        "completeness_snapshot": seal_report,
        "dispositions": closure_dispositions,
        "gate_report_hash": gate_report_hash,
        "signed_at": signed_at,
    }
    sign_off = IntentSessionSignOff(
        signer=str(signer),
        scope=resolved_scope,
        completeness_snapshot=seal_report,
        dispositions=closure_dispositions,
        gate_report_hash=gate_report_hash,
        signed_at=signed_at,
        sign_hash=body_hash(sign_off_core),
    )

    facts = GateFacts(
        completeness_no_error=completeness_no_error(body),
        verify_passed=verify_passed,
        human_signoff_and_all_dims_closed=True,
    )
    return advance(body, facts, role=_HUMAN, sign_off=sign_off)


__all__ = [
    "GENESIS_PREV_HASH",
    "GateFacts",
    "SessionTransitionError",
    "advance",
    "clarify",
    "completeness_no_error",
    "consistency_report_for",
    "embed_completeness",
    "gate_facts_for",
    "new_session",
    "next_step",
    "revert",
    "seal",
    "turn_content_hash",
    "verify_clarifications",
    "verify_journal",
]
