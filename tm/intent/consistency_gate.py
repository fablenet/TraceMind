"""Design-time consistency gate (Stage 7-0.8, plan §5b).

Composes the three "防矛盾 / 防不完整 / 防重复" concerns into a *single*
deterministic gate that the IntentSession state machine (Stage 7-2) runs on
every ``advance``, so they become invariants threaded through the whole
iteration rather than one-off checks::

    advance gate = 5W1H complete (7-0)
                 ∧ CTL no-contradiction (7-V / verify)
                 ∧ no exact duplicate (body_hash)            ← HARD (deterministic, blocks)
                 + semantic-duplicate / ambiguity warnings    ← SOFT (heuristic, triggers clarify)

Honest layering
---------------
* **Hard gate = deterministic guarantee.** Structural completeness, logical
  no-contradiction *relative to declared properties*, and no exact duplicate.
  Any one failing → ``passed == False`` → caller must refuse ``advance``.
* **Soft warnings = heuristic.** Semantic equivalence / ambiguity can only be
  *hinted*, never guaranteed. They never flip ``passed``; they are surfaced for
  a human ``clarify`` turn (Stage 7-2.7) and recorded in the journal.

Boundary declaration
---------------------
Completeness is **relative to a profile**; contradiction is **relative to the
declared CTL properties**. The report carries an explicit :class:`Boundary` so
``passed`` is never misread as "absolutely complete / absolutely consistent".

This module is intentionally **zero-LLM and deterministic** (guarded by
``scripts/check_no_llm_in_completeness.py``): all heuristic/RAG signals enter as
caller-provided :class:`SoftWarning` inputs, never imported here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

CHECK_COMPLETENESS = "completeness"
CHECK_NO_CONTRADICTION = "no_contradiction"
CHECK_NO_EXACT_DUPLICATE = "no_exact_duplicate"


@dataclass(frozen=True)
class Boundary:
    """Explicit "relative to what" scope of the gate's guarantees."""

    completeness_profile: str | None
    contradiction_basis: str = "declared CTL properties"

    def to_dict(self) -> dict[str, Any]:
        return {
            "completeness_relative_to_profile": self.completeness_profile,
            "contradiction_relative_to": self.contradiction_basis,
            "note": (
                "Completeness is relative to the named profile and contradiction "
                "is relative to the declared properties; this is NOT a claim of "
                "absolute completeness or absolute consistency."
            ),
        }


@dataclass(frozen=True)
class GateCheck:
    """One hard-gate check result."""

    name: str
    passed: bool
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "passed": self.passed, "detail": self.detail}


@dataclass(frozen=True)
class SoftWarning:
    """A non-blocking heuristic signal (e.g. RAG semantic-dup / ambiguity)."""

    kind: str
    message: str
    ref: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "message": self.message, "ref": self.ref}


@dataclass(frozen=True)
class GateReport:
    """Unified design-time consistency report (deterministic for hard checks)."""

    passed: bool
    hard_checks: tuple[GateCheck, ...]
    soft_warnings: tuple[SoftWarning, ...]
    boundary: Boundary
    blocking_reasons: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "hard_checks": [c.to_dict() for c in self.hard_checks],
            "soft_warnings": [w.to_dict() for w in self.soft_warnings],
            "boundary": self.boundary.to_dict(),
            "blocking_reasons": list(self.blocking_reasons),
        }


def _completeness_check(
    report: Mapping[str, Any] | None,
    *,
    profile: str | None,
) -> GateCheck:
    rel = profile or (str(report.get("profile")) if report else None)
    if report is None:
        return GateCheck(
            CHECK_COMPLETENESS,
            False,
            "no 5W1H completeness report provided",
        )
    summary = report.get("summary", {})
    errors = int(summary.get("errors", 0)) if isinstance(summary, Mapping) else 0
    rel_txt = f" relative to profile '{rel}'" if rel else ""
    if errors == 0:
        return GateCheck(
            CHECK_COMPLETENESS,
            True,
            f"5W1H complete (0 errors){rel_txt}",
        )
    return GateCheck(
        CHECK_COMPLETENESS,
        False,
        f"5W1H incomplete: {errors} error dimension(s){rel_txt}",
    )


def _no_contradiction_check(
    verify_passed: bool | None,
    *,
    require_verify: bool,
) -> GateCheck:
    if verify_passed is True:
        return GateCheck(
            CHECK_NO_CONTRADICTION,
            True,
            "CTL verification passed (no contradiction relative to declared properties)",
        )
    if verify_passed is False:
        return GateCheck(
            CHECK_NO_CONTRADICTION,
            False,
            "CTL verification failed: contradiction relative to declared properties",
        )
    # verify_passed is None → not yet run. Be honest: do NOT claim consistency.
    if require_verify:
        return GateCheck(
            CHECK_NO_CONTRADICTION,
            False,
            "CTL verification required but not run",
        )
    return GateCheck(
        CHECK_NO_CONTRADICTION,
        True,
        "CTL verification deferred; no contradiction asserted (not yet checked)",
    )


def _no_exact_duplicate_check(
    body_hash: str | None,
    existing_hashes: Iterable[str],
) -> GateCheck:
    if not body_hash:
        return GateCheck(
            CHECK_NO_EXACT_DUPLICATE,
            True,
            "no body_hash provided; exact-duplicate check skipped",
        )
    if body_hash in {h for h in existing_hashes if h}:
        return GateCheck(
            CHECK_NO_EXACT_DUPLICATE,
            False,
            f"exact duplicate of an existing artifact (body_hash {body_hash[:12]}…)",
        )
    return GateCheck(
        CHECK_NO_EXACT_DUPLICATE,
        True,
        f"no exact duplicate (body_hash {body_hash[:12]}…)",
    )


def evaluate_consistency(
    *,
    completeness_report: Mapping[str, Any] | None = None,
    completeness_profile: str | None = None,
    verify_passed: bool | None = None,
    require_verify: bool = False,
    body_hash: str | None = None,
    existing_hashes: Iterable[str] = (),
    soft_warnings: Iterable[SoftWarning] = (),
) -> GateReport:
    """Run the unified design-time consistency gate.

    Hard checks (deterministic, block ``advance`` if any fails):
      * ``completeness`` — ``summary.errors == 0`` in the supplied 5W1H report.
      * ``no_contradiction`` — ``verify_passed`` from CTL/verify (7-V). ``None``
        means "not run": tolerated unless ``require_verify`` (e.g. at seal),
        and never falsely claims consistency (see detail text).
      * ``no_exact_duplicate`` — ``body_hash`` does not appear in
        ``existing_hashes`` (the hashes of the *other* pre-existing artifacts;
        the candidate itself must not be included by the caller).

    Soft warnings (heuristic, never flip ``passed``) are passed through for a
    human ``clarify`` turn. The :class:`Boundary` records "relative to what".
    """
    existing = tuple(existing_hashes)
    hard = (
        _completeness_check(completeness_report, profile=completeness_profile),
        _no_contradiction_check(verify_passed, require_verify=require_verify),
        _no_exact_duplicate_check(body_hash, existing),
    )
    blocking = tuple(c.name for c in hard if not c.passed)
    boundary = Boundary(
        completeness_profile=(
            completeness_profile
            or (str(completeness_report.get("profile")) if completeness_report else None)
        )
    )
    return GateReport(
        passed=not blocking,
        hard_checks=hard,
        soft_warnings=tuple(soft_warnings),
        boundary=boundary,
        blocking_reasons=blocking,
    )


__all__ = [
    "CHECK_COMPLETENESS",
    "CHECK_NO_CONTRADICTION",
    "CHECK_NO_EXACT_DUPLICATE",
    "Boundary",
    "GateCheck",
    "GateReport",
    "SoftWarning",
    "evaluate_consistency",
]
