from __future__ import annotations
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Any, Tuple
import json
import time
from tm.obs.recorder import Recorder

Path = Tuple[Any, ...]


@dataclass(frozen=True)
class StepSpec:
    name: str
    reads: List[str]
    writes: List[str]
    fn: Callable[[Dict[str, Any]], Dict[str, Any]]  # pure function: ctx -> ctx
    # Verification-model facts this step *removes* (non-monotone clear). Lets the
    # Kripke abstraction express release/recover/toggle (lock release, throttle
    # off, un-quarantine). Runtime execution ignores this; it only affects the
    # ``meta.verify`` transition system. See ISSUE-FORMLANG P1.
    clears: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class Rule:
    name: str
    triggers: List[str]
    steps: List[str]


@dataclass
class Plan:
    steps: Dict[str, StepSpec]
    rules: List[Rule]


@dataclass
class TraceSpan:
    rule: str
    step: str
    t0: float
    t1: float
    inputs: Dict[str, Any]
    outputs: Dict[str, Any]
    reads: List[str]
    writes: List[str]
    error: str | None = None


class Pipeline:
    def __init__(self, plan: Plan, trace_sink: Callable[[TraceSpan], None]):
        self.plan = plan
        self.trace_sink = trace_sink

    def _match_any(self, exprs: List[str], changed: List[Path], matcher: Callable[[str, Path], bool]) -> bool:
        for e in exprs:
            for p in changed:
                if matcher(e, p):
                    return True
        return False

    def run(
        self, ctx: Dict[str, Any], changed_paths: List[Path], matcher: Callable[[str, Path], bool]
    ) -> Dict[str, Any]:
        for rule in self.plan.rules:
            if not self._match_any(rule.triggers, changed_paths, matcher):
                continue
            for step_name in rule.steps:
                spec = self.plan.steps[step_name]
                t0 = time.time()
                err = None
                # deep copy via JSON for determinism
                before = json.loads(json.dumps(ctx))
                try:
                    ctx = spec.fn(ctx)
                except Exception as ex:
                    err = f"{type(ex).__name__}: {ex}"
                t1 = time.time()
                self.trace_sink(
                    TraceSpan(
                        rule=rule.name,
                        step=spec.name,
                        t0=t0,
                        t1=t1,
                        inputs=before,
                        outputs=ctx,
                        reads=spec.reads,
                        writes=spec.writes,
                        error=err,
                    )
                )
                Recorder.default().on_pipeline_step(rule.name, spec.name, "error" if err else "ok")
                if err:
                    break  # stop current rule on error
        return ctx
