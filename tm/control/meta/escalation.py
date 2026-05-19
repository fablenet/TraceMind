"""Escalation artifact generator.

When the ConvergenceDetector determines that L1 is stalled or worsening,
the Escalator produces a structured EscalationReport. This report is the
formal handoff to L2 (human + LLM meta-controller) and includes:

- What happened: KPI history + trend verdict
- Why it failed: gap analysis (which rules fired, what didn't work)
- What to try next: suggested countermeasures + affected intents

The report is designed to be consumed by both humans (readable YAML) and
machines (can drive automated bundle recompilation downstream).

Domain-neutral: the only domain-coupling point was the default ``intent_ref``,
which is now a neutral placeholder (``"intent.unspecified"``); concrete
control scenarios MUST supply their own ``intent_ref`` (or any non-empty
identifier) when constructing the ``Escalator``.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping, Sequence

from tm.control.meta.convergence import ConvergenceVerdict, Trend
from tm.control.meta.kpi_tracker import CycleRecord, KpiTracker

UNSPECIFIED_INTENT_REF = "intent.unspecified"


class Severity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class SuggestedAction(str, Enum):
    TIGHTEN_THRESHOLDS = "tighten_thresholds"
    ADD_NEW_RULE = "add_new_rule"
    UPDATE_KNOWLEDGE = "update_knowledge"
    RETRAIN_MODEL = "retrain_model"
    HUMAN_REVIEW = "human_review"
    RECOMPILE_BUNDLE = "recompile_bundle"
    ADJUST_KRIPKE_PROPERTIES = "adjust_kripke_properties"


@dataclass(frozen=True)
class EscalationReport:
    """Immutable L2 escalation artifact."""

    report_id: str
    timestamp: str
    severity: Severity
    intent_ref: str

    verdicts: tuple[ConvergenceVerdict, ...]
    kpi_history: tuple[CycleRecord, ...]
    recent_rules_fired: tuple[str, ...]
    recent_errors: tuple[str, ...]

    gap_summary: str
    suggested_actions: tuple[SuggestedAction, ...]
    counterexample: Mapping[str, Any] | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "report_id": self.report_id,
            "timestamp": self.timestamp,
            "severity": self.severity.value,
            "intent_ref": self.intent_ref,
            "verdicts": [
                {
                    "kpi": v.kpi_name,
                    "trend": v.trend.value,
                    "converged": v.converged,
                    "delta": v.delta,
                    "values": list(v.values),
                    "reason": v.reason,
                }
                for v in self.verdicts
            ],
            "kpi_history_count": len(self.kpi_history),
            "recent_rules_fired": list(self.recent_rules_fired),
            "recent_errors": list(self.recent_errors),
            "gap_summary": self.gap_summary,
            "suggested_actions": [a.value for a in self.suggested_actions],
            "counterexample": dict(self.counterexample) if self.counterexample else None,
        }


class Escalator:
    """Produces EscalationReports from convergence verdicts.

    Parameters
    ----------
    intent_ref:
        The intent this escalator monitors. Defaults to
        ``UNSPECIFIED_INTENT_REF`` (``"intent.unspecified"``); domain-specific
        scenarios MUST pass a concrete identifier from their own ontology.
    """

    def __init__(self, intent_ref: str = UNSPECIFIED_INTENT_REF):
        self.intent_ref = intent_ref

    def evaluate(
        self,
        tracker: KpiTracker,
        verdicts: Sequence[ConvergenceVerdict],
    ) -> EscalationReport | None:
        """Return an EscalationReport if any verdict needs escalation, else None."""
        needs = [v for v in verdicts if v.needs_escalation]
        if not needs:
            return None

        worst = needs[0]
        severity = self._classify_severity(worst, tracker)
        gap_summary = self._build_gap_summary(needs, tracker)
        actions = self._suggest_actions(worst, tracker)
        counterexample = self._build_counterexample(worst, tracker)

        records = tracker.records
        rules_fired: list[str] = []
        errors: list[str] = []
        for rec in records:
            if rec.policy_rule_fired:
                rules_fired.append(rec.policy_rule_fired)
            errors.extend(rec.errors)

        return EscalationReport(
            report_id=f"esc-{self.intent_ref}-{int(time.time())}",
            timestamp=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            severity=severity,
            intent_ref=self.intent_ref,
            verdicts=tuple(needs),
            kpi_history=tuple(records),
            recent_rules_fired=tuple(dict.fromkeys(rules_fired)),
            recent_errors=tuple(dict.fromkeys(errors)),
            gap_summary=gap_summary,
            suggested_actions=tuple(actions),
            counterexample=counterexample,
        )

    def _classify_severity(self, worst: ConvergenceVerdict, tracker: KpiTracker) -> Severity:
        if worst.trend == Trend.WORSENING:
            if tracker.count >= tracker.window_size:
                return Severity.CRITICAL
            return Severity.WARNING
        if worst.trend == Trend.STALLED:
            return Severity.WARNING
        return Severity.INFO

    def _build_gap_summary(
        self,
        needs: Sequence[ConvergenceVerdict],
        tracker: KpiTracker,
    ) -> str:
        parts: list[str] = []
        for v in needs:
            parts.append(f"[{v.trend.value}] {v.kpi_name}: {v.reason}")

        fired = set()
        for rec in tracker.records:
            if rec.policy_rule_fired:
                fired.add(rec.policy_rule_fired)
        if fired:
            parts.append(f"Rules fired without effect: {', '.join(sorted(fired))}")

        error_set = set()
        for rec in tracker.records:
            error_set.update(rec.errors)
        if error_set:
            parts.append(f"Execution errors: {'; '.join(sorted(error_set))}")

        return " | ".join(parts)

    def _suggest_actions(
        self,
        worst: ConvergenceVerdict,
        tracker: KpiTracker,
    ) -> list[SuggestedAction]:
        actions: list[SuggestedAction] = []
        if worst.trend == Trend.WORSENING:
            actions.append(SuggestedAction.HUMAN_REVIEW)
            actions.append(SuggestedAction.ADD_NEW_RULE)
            actions.append(SuggestedAction.UPDATE_KNOWLEDGE)
            if tracker.count >= tracker.window_size:
                actions.append(SuggestedAction.RECOMPILE_BUNDLE)
        elif worst.trend == Trend.STALLED:
            actions.append(SuggestedAction.TIGHTEN_THRESHOLDS)
            actions.append(SuggestedAction.ADD_NEW_RULE)
        return actions

    def _build_counterexample(
        self,
        worst: ConvergenceVerdict,
        tracker: KpiTracker,
    ) -> dict[str, Any] | None:
        """Build a counterexample showing the adversary's likely adaptation."""
        if worst.trend != Trend.WORSENING:
            return None
        records = tracker.records
        if len(records) < 2:
            return None

        early = records[0]
        late = records[-1]

        ce: dict[str, Any] = {
            "description": (
                f"{worst.kpi_name} worsened from "
                f"{worst.values[0]:.4f} to {worst.values[-1]:.4f} "
                f"despite L1 interventions"
            ),
            "before": {
                "cycle_id": early.cycle_id,
                "kpis": dict(early.kpis),
                "rule_fired": early.policy_rule_fired,
            },
            "after": {
                "cycle_id": late.cycle_id,
                "kpis": dict(late.kpis),
                "rule_fired": late.policy_rule_fired,
            },
            "hypothesis": (
                "Adversary may have adapted to current countermeasures. Consider new detection signals or policy rules."
            ),
        }
        return ce


__all__ = [
    "EscalationReport",
    "Escalator",
    "Severity",
    "SuggestedAction",
    "UNSPECIFIED_INTENT_REF",
]
