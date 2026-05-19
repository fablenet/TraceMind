"""Feedback loop — Kripke counterexample → Case archive → KB patch.

Stage 5-4 task 4.4.

This module closes the loop from runtime evidence back into the Pattern
Library:

```
ProofReport(fail) / EscalationReport(critical)
        │
        ▼
   CaseCorpus           (task 4.2: aggregation as a virtual view)
        │
        ▼
   FeedbackSignals      (this module: filtering + extraction)
        │
        ▼
   PatchProposal        (this module: synthesis as a draft artifact)
        │
        ▼
   governance.verify    (existing pipeline: human-in-the-loop approval)
        │
        ▼
   PatternLibrary       (only after explicit approval)
```

## What this module is NOT

- It does **not** write to disk on its own. The output is a draft
  :class:`ProposedChangePlanBody` artifact — a candidate that must walk
  through ``candidate → verify → accept`` like any other K-Ontology
  artifact (Phase 5 invariant 3).
- It does **not** mutate the :class:`PatternLibrary`. Library updates
  require an approved proposal to apply.
- It does **not** invent CTL formulas. The Phase 5 mantra ("AI proposes,
  governance disposes") applies — the proposal flags *what changed* and
  *why*, never *how*.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence

from tm.controllers.models import (
    LlmMetadata,
    ProposedChangeDecision,
    ProposedChangePlanBody,
)

from .case_corpus import Case, CaseCorpus, CaseEvidence

# ─── Feedback signal dataclass ────────────────────────────────────


@dataclass
class FeedbackSignal:
    """One piece of evidence that something in the pattern library is
    under-specified or wrong.

    Sourced from a failed :class:`ProofReportBody` or a critical
    :class:`EscalationReportBody`. The signal carries:

    - Which artifact produced it (``source_kind``, ``source_ref``)
    - Which intent / pattern it targets (so the patch proposal can be
      scoped correctly)
    - The counterexample payload, copied verbatim so the human reviewer
      sees the exact runtime evidence
    """

    source_kind: str
    source_ref: str
    intent_id: str
    pattern_ids: List[str] = field(default_factory=list)
    counterexample: Dict[str, Any] = field(default_factory=dict)
    severity: str = "warning"
    summary: str = ""

    def fingerprint(self) -> str:
        """Stable hash for deduplication / idempotency."""
        material = (
            f"{self.source_kind}|{self.source_ref}|{self.intent_id}|"
            f"{sorted(self.pattern_ids)}|"
            f"{sorted(self.counterexample.items())}"
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


# ─── Collection ───────────────────────────────────────────────────


def collect_feedback_signals(corpus: CaseCorpus) -> List[FeedbackSignal]:
    """Walk the corpus and extract one :class:`FeedbackSignal` per
    failed proof / critical escalation.

    Ordering is deterministic — signals are sorted by
    ``(intent_id, source_kind, source_ref)`` so consumers get reproducible
    proposal_ids on re-runs.
    """
    signals: List[FeedbackSignal] = []
    for case in corpus.cases():
        for ev in case.evidence:
            signal = _evidence_to_signal(case, ev)
            if signal is not None:
                signals.append(signal)
    signals.sort(key=lambda s: (s.intent_id, s.source_kind, s.source_ref))
    return signals


def _evidence_to_signal(case: Case, ev: CaseEvidence) -> Optional[FeedbackSignal]:
    """Promote a :class:`CaseEvidence` item to a :class:`FeedbackSignal`
    if it represents a failure.

    The rules:

    - ``proof_report`` with ``overall_verdict in {fail, inconclusive}``
      → signal; each counterexample entry copied verbatim
    - ``escalation_report`` with ``severity in {warning, critical}``
      → signal; ``recent_rules_fired`` provides extra context
    - everything else → ``None``
    """
    if ev.kind == "proof_report":
        verdict = ev.details.get("overall_verdict")
        if verdict not in {"fail", "inconclusive"}:
            return None
        cxs = ev.details.get("counterexamples") or []
        counterexample: Dict[str, Any] = dict(cxs[0]) if cxs else {"verdict": verdict}
        return FeedbackSignal(
            source_kind="proof_report",
            source_ref=ev.ref,
            intent_id=case.intent_id,
            pattern_ids=list(case.pattern_refs),
            counterexample=counterexample,
            severity="critical" if verdict == "fail" else "warning",
            summary=(f"proof '{ev.ref}' for intent '{case.intent_id}' returned verdict={verdict}"),
        )
    if ev.kind == "escalation_report":
        severity = ev.details.get("severity")
        if severity not in {"warning", "critical"}:
            return None
        counterexample = dict(ev.details.get("counterexample") or {})
        if not counterexample:
            counterexample = {"recent_rules_fired": list(ev.details.get("recent_rules_fired") or [])}
        return FeedbackSignal(
            source_kind="escalation_report",
            source_ref=ev.ref,
            intent_id=case.intent_id,
            pattern_ids=list(case.pattern_refs),
            counterexample=counterexample,
            severity=severity,
            summary=(f"escalation '{ev.ref}' for intent '{case.intent_id}' with severity={severity}"),
        )
    return None


# ─── Synthesis ────────────────────────────────────────────────────


def synthesize_kb_patch_proposal(
    signals: Sequence[FeedbackSignal],
    *,
    proposal_id: str | None = None,
    library_root: str = "tm/patterns/seed",
    intent_id: str | None = None,
) -> Optional[ProposedChangePlanBody]:
    """Synthesize a draft KB-update :class:`ProposedChangePlanBody`.

    Returns ``None`` if ``signals`` is empty — refusing to manufacture
    spurious proposals when there's nothing to react to.

    The proposal is deliberately conservative:

    - It does **not** propose new CTL formulas (those require domain
      judgement; the human reviewer + a follow-up
      ``ai.propose_pattern_instances`` run handle that).
    - It **does** propose ``review_pattern`` decisions, one per
      pattern_id that appears in the signals, pointing the reviewer at
      the pattern definition file and attaching the counterexample as
      ``reasoning_trace``.
    - It marks ``llm_metadata.determinism_hint == "deterministic"`` —
      this proposal is generated by a deterministic procedure, not an
      LLM (Phase 5 invariant 3: AI never produces ``accepted`` status,
      but a deterministic synthesizer must still declare itself
      faithfully).
    """
    if not signals:
        return None
    intent = intent_id or _pick_dominant_intent(signals)
    decisions = _build_decisions(signals, library_root=library_root)
    if not decisions:
        return None
    resolved_id = proposal_id or _deterministic_proposal_id(intent, signals)

    return ProposedChangePlanBody(
        plan_id=resolved_id,
        intent_id=intent,
        decisions=decisions,
        llm_metadata=LlmMetadata(
            model="kb.feedback.deterministic",
            prompt_hash="n/a",
            determinism_hint="deterministic",
        ),
        summary=_build_summary(signals),
        policy_requirements=[
            "all existing tests pass",
            "pattern library remains additive (no breaking schema changes)",
            "every modified pattern re-passes verify against seed corpus",
        ],
    )


def _pick_dominant_intent(signals: Sequence[FeedbackSignal]) -> str:
    """Pick the intent referenced by the largest set of signals.

    Ties are broken alphabetically for determinism. Used when callers
    don't pass an explicit ``intent_id`` — the proposal still needs one
    field because the K-Ontology schema requires it.
    """
    counts: Dict[str, int] = {}
    for s in signals:
        counts[s.intent_id] = counts.get(s.intent_id, 0) + 1
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]


def _build_decisions(signals: Sequence[FeedbackSignal], *, library_root: str) -> List[ProposedChangeDecision]:
    """Group signals by pattern_id and emit one ``review_pattern``
    decision per pattern.

    Patterns that are *not* yet in the library (signals where no
    pattern_id is attributable, e.g. an intent without
    ``property_pattern_refs``) generate a synthetic ``review_unattributed``
    decision so the reviewer is still alerted.
    """
    by_pattern: Dict[str, List[FeedbackSignal]] = {}
    unattributed: List[FeedbackSignal] = []
    for signal in signals:
        if not signal.pattern_ids:
            unattributed.append(signal)
            continue
        for pid in signal.pattern_ids:
            by_pattern.setdefault(pid, []).append(signal)

    decisions: List[ProposedChangeDecision] = []
    for pid in sorted(by_pattern.keys()):
        related = by_pattern[pid]
        ref_path = f"{library_root}/{pid.split('.', 1)[0]}/{pid.split('.', 1)[1]}.yaml"
        decisions.append(
            ProposedChangeDecision(
                effect_ref=ref_path,
                target_state={
                    "operation": "review_pattern",
                    "pattern_id": pid,
                    "source_signals": [s.fingerprint() for s in related],
                    "counterexamples": [dict(s.counterexample) for s in related],
                },
                idempotency_key=_decision_idempotency_key(f"review_pattern:{pid}", related),
                reasoning_trace=_reasoning_trace_for(related),
            )
        )
    if unattributed:
        decisions.append(
            ProposedChangeDecision(
                effect_ref=f"{library_root}/UNATTRIBUTED.yaml",
                target_state={
                    "operation": "review_unattributed",
                    "source_signals": [s.fingerprint() for s in unattributed],
                    "intents": sorted({s.intent_id for s in unattributed}),
                },
                idempotency_key=_decision_idempotency_key("review_unattributed", unattributed),
                reasoning_trace=_reasoning_trace_for(unattributed),
            )
        )
    return decisions


def _decision_idempotency_key(operation: str, signals: Sequence[FeedbackSignal]) -> str:
    """Hash signals into a stable key so re-running the feedback loop
    on the same evidence does not generate spurious new decisions."""
    fps = sorted(s.fingerprint() for s in signals)
    material = f"{operation}|{','.join(fps)}"
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:12]
    return f"{operation}:{digest}"


def _reasoning_trace_for(signals: Sequence[FeedbackSignal]) -> str:
    """Build a compact human-readable trace summarizing every signal.

    Order is preserved as given (callers sort upstream for determinism)
    so the trace text is stable across runs with the same input.
    """
    lines = []
    for s in signals:
        lines.append(f"- [{s.severity}] {s.source_kind}={s.source_ref}: {s.summary}")
    return "\n".join(lines) if lines else "no signals"


def _build_summary(signals: Sequence[FeedbackSignal]) -> str:
    by_severity: Dict[str, int] = {}
    for s in signals:
        by_severity[s.severity] = by_severity.get(s.severity, 0) + 1
    pieces = [f"{count}× {sev}" for sev, count in sorted(by_severity.items())]
    intents = sorted({s.intent_id for s in signals})
    return (
        f"KB feedback synthesizer: {len(signals)} signal(s) across "
        f"{len(intents)} intent(s); breakdown: {', '.join(pieces)}"
    )


def _deterministic_proposal_id(intent_id: str, signals: Sequence[FeedbackSignal]) -> str:
    fps = sorted(s.fingerprint() for s in signals)
    digest = hashlib.sha256(f"{intent_id}|{','.join(fps)}".encode("utf-8")).hexdigest()[:12]
    return f"kb-feedback.{intent_id}.{digest}"


# ─── Convenience: end-to-end loop ─────────────────────────────────


def run_feedback_loop(
    corpus: CaseCorpus,
    *,
    proposal_id: str | None = None,
    library_root: str = "tm/patterns/seed",
) -> tuple[List[FeedbackSignal], Optional[ProposedChangePlanBody]]:
    """Collect signals + synthesize a draft proposal in one call.

    Returns the signals list (possibly empty) and a draft proposal
    (or ``None`` when no failures are present). The proposal is
    intentionally returned as a body — wrapping it in an envelope and
    submitting through ``tm.artifacts.verify`` is the caller's
    responsibility (see :mod:`tests.test_kb_feedback` for the recipe).
    """
    signals = collect_feedback_signals(corpus)
    proposal = synthesize_kb_patch_proposal(signals, proposal_id=proposal_id, library_root=library_root)
    return signals, proposal


__all__ = [
    "FeedbackSignal",
    "collect_feedback_signals",
    "run_feedback_loop",
    "synthesize_kb_patch_proposal",
]


# Re-exports for callers that prefer flat imports
_ = Mapping  # quiet unused-import linter for the type alias context above
