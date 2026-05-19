"""Convergence / divergence detector for L1 Controller Cycles.

Analyses the KPI trend across a sliding window to decide whether the
current strategy is working (converging), plateaued (stalled), or failing
(diverging). This verdict drives the L2 escalation decision.

The detector is intentionally simple and deterministic — no ML, just
basic trend statistics over a configurable window. Adversarial adaptation
is detected when a KPI that *was* improving reverses direction.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Sequence

from tm.control.meta.kpi_tracker import KpiTracker


class Trend(str, Enum):
    IMPROVING = "improving"
    STALLED = "stalled"
    WORSENING = "worsening"
    INSUFFICIENT_DATA = "insufficient_data"


@dataclass(frozen=True)
class ConvergenceVerdict:
    """Result of a convergence check."""

    converged: bool
    trend: Trend
    kpi_name: str
    values: tuple[float, ...]
    delta: float
    reason: str

    @property
    def needs_escalation(self) -> bool:
        return self.trend in (Trend.WORSENING, Trend.STALLED)


class ConvergenceDetector:
    """Stateless detector: call ``evaluate`` with a KpiTracker to get a verdict.

    Parameters
    ----------
    target_kpi:
        The KPI name to track. Domain-neutral example: ``"error_rate"``.
    target_value:
        The value we want the KPI to reach.
    tolerance:
        If ``abs(current - target_value) < tolerance``, consider converged.
    min_window:
        Minimum number of records before we can judge a trend.
    stall_threshold:
        If the absolute change across the window is less than this,
        the trend is ``STALLED`` (adversary may have adapted).
    direction:
        ``"down"`` means lower is better (e.g. error → 0).
        ``"up"`` means higher is better (e.g. success_ratio → 1).
    """

    def __init__(
        self,
        target_kpi: str,
        target_value: float = 0.0,
        tolerance: float = 0.05,
        min_window: int = 3,
        stall_threshold: float = 0.02,
        direction: str = "down",
    ):
        self.target_kpi = target_kpi
        self.target_value = target_value
        self.tolerance = tolerance
        self.min_window = min_window
        self.stall_threshold = stall_threshold
        self.direction = direction

    def evaluate(self, tracker: KpiTracker) -> ConvergenceVerdict:
        series = tracker.kpi_series(self.target_kpi)
        if len(series) < self.min_window:
            return ConvergenceVerdict(
                converged=False,
                trend=Trend.INSUFFICIENT_DATA,
                kpi_name=self.target_kpi,
                values=tuple(series),
                delta=0.0,
                reason=(f"need {self.min_window} data points, have {len(series)}"),
            )

        recent = series[-self.min_window :]
        current = recent[-1]

        if abs(current - self.target_value) < self.tolerance:
            return ConvergenceVerdict(
                converged=True,
                trend=Trend.IMPROVING,
                kpi_name=self.target_kpi,
                values=tuple(recent),
                delta=current - self.target_value,
                reason=(f"{self.target_kpi}={current:.4f} within tolerance of target {self.target_value}"),
            )

        delta = recent[-1] - recent[0]
        abs_delta = abs(delta)

        if abs_delta < self.stall_threshold:
            return ConvergenceVerdict(
                converged=False,
                trend=Trend.STALLED,
                kpi_name=self.target_kpi,
                values=tuple(recent),
                delta=delta,
                reason=(
                    f"{self.target_kpi} stalled: "
                    f"Δ={delta:+.4f} over {len(recent)} cycles "
                    f"(threshold={self.stall_threshold})"
                ),
            )

        improving = (delta < 0) if self.direction == "down" else (delta > 0)
        if improving:
            return ConvergenceVerdict(
                converged=False,
                trend=Trend.IMPROVING,
                kpi_name=self.target_kpi,
                values=tuple(recent),
                delta=delta,
                reason=(f"{self.target_kpi} improving: Δ={delta:+.4f} over {len(recent)} cycles"),
            )

        return ConvergenceVerdict(
            converged=False,
            trend=Trend.WORSENING,
            kpi_name=self.target_kpi,
            values=tuple(recent),
            delta=delta,
            reason=(
                f"{self.target_kpi} worsening: Δ={delta:+.4f} over {len(recent)} cycles — adversary may have adapted"
            ),
        )

    def evaluate_multiple(self, tracker: KpiTracker, kpis: Sequence[str] | None = None) -> list[ConvergenceVerdict]:
        """Evaluate multiple KPIs; returns the worst-case verdict first."""
        targets = kpis or [self.target_kpi]
        verdicts: list[ConvergenceVerdict] = []
        for kpi in targets:
            detector = ConvergenceDetector(
                target_kpi=kpi,
                target_value=self.target_value,
                tolerance=self.tolerance,
                min_window=self.min_window,
                stall_threshold=self.stall_threshold,
                direction=self.direction,
            )
            verdicts.append(detector.evaluate(tracker))
        priority = {
            Trend.WORSENING: 0,
            Trend.STALLED: 1,
            Trend.INSUFFICIENT_DATA: 2,
            Trend.IMPROVING: 3,
        }
        verdicts.sort(key=lambda v: priority.get(v.trend, 99))
        return verdicts


__all__ = ["ConvergenceDetector", "ConvergenceVerdict", "Trend"]
