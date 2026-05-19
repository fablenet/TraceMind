"""MetaController — L2 orchestrator that runs L1 cycles in a loop.

The MetaController:

1. Runs L1 Controller Cycles repeatedly (observe → analyze → decide → act)
2. After each cycle, records KPIs and checks convergence
3. If the strategy is working → continues
4. If the strategy has stalled or is worsening → escalates to L2

L2 escalation produces an ``EscalationReport``. In the current stage this
report is returned to the caller (human-in-the-loop). Downstream stages
can use it to drive automated bundle recompilation.

Domain-neutral: the only domain coupling points (``intent_ref`` and the
KPI key allowlist) are constructor parameters with neutral defaults.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

from tm.control.meta.convergence import ConvergenceDetector, ConvergenceVerdict
from tm.control.meta.escalation import (
    UNSPECIFIED_INTENT_REF,
    EscalationReport,
    Escalator,
)
from tm.control.meta.kpi_tracker import CycleRecord, KpiTracker

log = logging.getLogger(__name__)


@dataclass
class L1CycleResult:
    """Structured output of a single L1 cycle execution."""

    cycle_id: str
    snapshot: Mapping[str, Any]
    report: Mapping[str, Any]
    policy_result: Mapping[str, Any] | None = None


@dataclass
class MetaControllerResult:
    """Final result of the MetaController run."""

    total_cycles: int
    converged: bool
    final_verdict: ConvergenceVerdict | None
    escalation: EscalationReport | None
    cycle_records: list[CycleRecord] = field(default_factory=list)


L1Runner = Callable[[int], L1CycleResult]


class MetaController:
    """L2 meta-controller that wraps L1 cycles with convergence checking.

    Parameters
    ----------
    l1_runner:
        A callable ``(cycle_number) -> L1CycleResult`` that executes one
        L1 controller cycle. This is intentionally a callback so the
        MetaController is decoupled from how L1 runs (ControllerCycle,
        direct agent calls, mock, etc).
    detector:
        Convergence detector for the primary KPI.
    intent_ref:
        The intent being controlled (for escalation reports). Defaults to
        the neutral placeholder ``"intent.unspecified"``; concrete control
        scenarios should pass a real identifier.
    max_cycles:
        Maximum L1 cycles before forced escalation.
    tracker:
        Optional pre-populated KpiTracker (for resumption).
    kpi_keys:
        Optional allowlist of KPI keys to extract from each cycle's
        snapshot. When ``None`` (the default), every numeric value under
        ``environment.metrics`` is captured. Pass a sequence to restrict
        to a domain-specific subset (e.g. for the FableNet anti-sybil
        scenario: ``["sybil_score", "burst_ratio", "false_positive_rate"]``).
    """

    def __init__(
        self,
        l1_runner: L1Runner,
        detector: ConvergenceDetector,
        intent_ref: str = UNSPECIFIED_INTENT_REF,
        max_cycles: int = 10,
        tracker: KpiTracker | None = None,
        kpi_keys: Sequence[str] | None = None,
    ):
        self._l1_runner = l1_runner
        self._detector = detector
        self._escalator = Escalator(intent_ref=intent_ref)
        self._max_cycles = max_cycles
        self._tracker = tracker or KpiTracker(window_size=max_cycles)
        self._kpi_keys: tuple[str, ...] | None = tuple(kpi_keys) if kpi_keys is not None else None

    @property
    def tracker(self) -> KpiTracker:
        return self._tracker

    def run(self) -> MetaControllerResult:
        """Execute the L1 → check → escalate loop."""
        records: list[CycleRecord] = []

        for cycle_num in range(1, self._max_cycles + 1):
            cycle_id = f"cycle-{cycle_num}-{int(time.time())}"

            l1_result = self._l1_runner(cycle_num)

            record = CycleRecord.from_cycle_outputs(
                cycle_id=cycle_id,
                snapshot=l1_result.snapshot,
                report=l1_result.report,
                policy_result=l1_result.policy_result,
                kpi_keys=self._kpi_keys,
            )
            self._tracker.record(record)
            records.append(record)

            log.info(
                "L1 cycle %d/%d: %s kpis=%s rule=%s",
                cycle_num,
                self._max_cycles,
                record.act_status,
                dict(record.kpis),
                record.policy_rule_fired,
            )

            verdict = self._detector.evaluate(self._tracker)

            if verdict.converged:
                log.info("Converged: %s", verdict.reason)
                return MetaControllerResult(
                    total_cycles=cycle_num,
                    converged=True,
                    final_verdict=verdict,
                    escalation=None,
                    cycle_records=records,
                )

            if verdict.needs_escalation:
                log.warning("Escalation triggered: %s", verdict.reason)
                escalation = self._escalator.evaluate(self._tracker, [verdict])
                return MetaControllerResult(
                    total_cycles=cycle_num,
                    converged=False,
                    final_verdict=verdict,
                    escalation=escalation,
                    cycle_records=records,
                )

        log.warning(
            "Max cycles (%d) reached without convergence",
            self._max_cycles,
        )
        final_verdict = self._detector.evaluate(self._tracker)
        escalation = self._escalator.evaluate(self._tracker, [final_verdict])
        return MetaControllerResult(
            total_cycles=self._max_cycles,
            converged=False,
            final_verdict=final_verdict,
            escalation=escalation,
            cycle_records=records,
        )


__all__ = ["L1CycleResult", "L1Runner", "MetaController", "MetaControllerResult"]
