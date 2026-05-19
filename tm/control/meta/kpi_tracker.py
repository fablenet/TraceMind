"""Cross-cycle KPI tracker.

Records the outcome of each L1 Controller Cycle and maintains a sliding
window of recent observations for trend analysis.

Domain-neutral: the set of KPI keys to extract from a snapshot is supplied
by the caller via the ``kpi_keys`` parameter on ``CycleRecord.from_cycle_outputs``.
When ``kpi_keys`` is ``None`` (default), every numeric value under
``snapshot.environment.metrics`` is captured.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from numbers import Real
from typing import Any, Mapping, Sequence


def _coerce_numeric(value: Any) -> float | None:
    """Best-effort coercion to a Python ``float``; returns ``None`` if not numeric.

    A separate helper because ``bool`` is a subclass of ``int`` in Python — we
    deliberately exclude booleans (a True/False status is *not* a KPI).
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Real):
        try:
            return float(value)
        except (ValueError, TypeError, OverflowError):
            return None
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


@dataclass(frozen=True)
class CycleRecord:
    """Immutable snapshot of one L1 cycle's key metrics."""

    cycle_id: str
    timestamp: float
    kpis: Mapping[str, float]
    policy_rule_fired: str
    act_status: str
    errors: tuple[str, ...]

    @classmethod
    def from_cycle_outputs(
        cls,
        cycle_id: str,
        snapshot: Mapping[str, Any],
        report: Mapping[str, Any],
        policy_result: Mapping[str, Any] | None = None,
        kpi_keys: Sequence[str] | None = None,
    ) -> "CycleRecord":
        """Construct a CycleRecord from raw cycle outputs.

        Parameters
        ----------
        cycle_id:
            Identifier for the cycle being recorded.
        snapshot:
            Environment snapshot mapping; KPIs are read from
            ``snapshot["environment"]["metrics"]`` when present.
        report:
            Execution report mapping; ``status`` and ``errors`` are read.
        policy_result:
            Optional policy evaluation result; the first applied action's
            ``rule_id`` is captured as ``policy_rule_fired``.
        kpi_keys:
            Allowlist of KPI keys to extract. When ``None`` (the default),
            every numeric value found under ``environment.metrics`` is
            captured. Pass an explicit sequence (e.g.
            ``["sybil_score", "burst_ratio"]``) to restrict extraction
            to a domain-specific subset.
        """
        env = snapshot.get("environment", {}) if isinstance(snapshot, Mapping) else {}
        metrics = env.get("metrics", {}) if isinstance(env, Mapping) else {}
        kpis: dict[str, float] = {}
        if isinstance(metrics, Mapping):
            if kpi_keys is None:
                for key, raw in metrics.items():
                    coerced = _coerce_numeric(raw)
                    if coerced is not None:
                        kpis[str(key)] = coerced
            else:
                for key in kpi_keys:
                    raw = metrics.get(key)
                    coerced = _coerce_numeric(raw)
                    if coerced is not None:
                        kpis[str(key)] = coerced

        rule_fired = ""
        if policy_result and isinstance(policy_result, Mapping):
            actions = policy_result.get("actions", [])
            if isinstance(actions, Sequence) and not isinstance(actions, (str, bytes)):
                for action in actions:
                    if isinstance(action, Mapping) and action.get("applied"):
                        rule_fired = str(action.get("rule_id", ""))
                        break

        report_map = report if isinstance(report, Mapping) else {}
        return cls(
            cycle_id=cycle_id,
            timestamp=time.time(),
            kpis=kpis,
            policy_rule_fired=rule_fired,
            act_status=str(report_map.get("status", "")),
            errors=tuple(report_map.get("errors", [])),
        )


class KpiTracker:
    """Sliding-window store for recent CycleRecords."""

    def __init__(self, window_size: int = 10):
        self._window: deque[CycleRecord] = deque(maxlen=window_size)
        self._window_size = window_size

    @property
    def window_size(self) -> int:
        return self._window_size

    def record(self, rec: CycleRecord) -> None:
        self._window.append(rec)

    @property
    def records(self) -> Sequence[CycleRecord]:
        return list(self._window)

    @property
    def count(self) -> int:
        return len(self._window)

    def kpi_series(self, kpi_name: str) -> list[float]:
        """Return the time-ordered values for ``kpi_name`` across all records."""
        return [rec.kpis[kpi_name] for rec in self._window if kpi_name in rec.kpis]

    def latest(self) -> CycleRecord | None:
        return self._window[-1] if self._window else None

    def clear(self) -> None:
        self._window.clear()


__all__ = ["CycleRecord", "KpiTracker"]
