"""Bridge MetaController's l1_runner to a real ControllerCycle.

Provides ``make_l1_runner()`` which creates a callable that:

1. Instantiates a :class:`tm.controllers.cycle.ControllerCycle` per run
2. Extracts snapshot/report as dicts from the cycle result
3. Returns an :class:`L1CycleResult` for MetaController consumption

The bridge is domain-neutral: bundle_path / agent_configs / report_dir are
all supplied by the caller; nothing here references any specific control
scenario.
"""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable, Mapping

from tm.control.meta.controller import L1CycleResult


def make_l1_runner(
    bundle_path: Path,
    agent_configs: Mapping[str, Mapping[str, Any]],
    report_dir: Path,
) -> Callable[[int], L1CycleResult]:
    """Return a callable ``(cycle_number) -> L1CycleResult``."""

    def _runner(cycle_num: int) -> L1CycleResult:
        from tm.controllers.cycle import ControllerCycle

        report_path = report_dir / f"cycle_{cycle_num}_report.yaml"

        cycle = ControllerCycle(
            bundle_path=bundle_path,
            mode="live",
            dry_run=False,
            report_path=report_path,
            artifact_output_dir=report_dir / f"cycle_{cycle_num}_artifacts",
            approval_token="approved",
        )
        cycle._build_agent_configs = lambda: dict(agent_configs)  # noqa: E731

        result = cycle.run()

        snapshot_dict = _body_to_dict(result.env_snapshot.body)
        report_dict = _body_to_dict(result.execution_report.body)
        plan_dict = _body_to_dict(result.planned_change.body)

        return L1CycleResult(
            cycle_id=f"cycle-{cycle_num}",
            snapshot=snapshot_dict,
            report=report_dict,
            policy_result=plan_dict,
        )

    return _runner


def _body_to_dict(body: Any) -> dict[str, Any]:
    try:
        return asdict(body)
    except TypeError:
        return {}


__all__ = ["make_l1_runner"]
