from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Mapping, Sequence, Tuple

from tm.pipeline import selectors
from tm.pipeline.engine import Plan, StepSpec

from .state import State

Path = Tuple[object, ...]


def _selector_to_path(expr: str) -> Path:
    toks = selectors.parse(expr)
    path: List[object] = []
    for tk in toks:
        if tk == "[]":
            path.append(0)
        elif tk == "*":
            path.append("*")
        elif tk.startswith("[") and tk.endswith("]"):
            inner = tk[1:-1]
            try:
                path.append(int(inner))
            except ValueError:
                path.append(inner)
        else:
            path.append(tk)
    return tuple(path)


def _rule_triggered(triggers: Sequence[str], changed_paths: Sequence[Path]) -> bool:
    for trig in triggers:
        for path in changed_paths:
            if selectors.match(trig, path):
                return True
    return False


@dataclass
class TraceMindAdapter:
    plan: Plan
    initial_store: Mapping[str, object]
    changed_paths: Sequence[Path]
    initial_pending: Sequence[str]
    initial_done: Sequence[str]

    @classmethod
    def from_plan(
        cls,
        plan: Plan,
        *,
        initial_store: Mapping[str, object] | None = None,
        changed_paths: Sequence[str] | None = None,
        initial_pending: Sequence[str] | None = None,
        initial_done: Sequence[str] | None = None,
    ) -> "TraceMindAdapter":
        return cls(
            plan=plan,
            initial_store=dict(initial_store or {}),
            changed_paths=[_selector_to_path(p) for p in (changed_paths or [])],
            initial_pending=tuple(initial_pending or ()),
            initial_done=tuple(initial_done or ()),
        )

    def initial_state(self) -> State:
        pending: List[str] = list(self.initial_pending)
        pending.extend(self._collect_steps(self.changed_paths))
        # keep ordering stable but avoid duplicating already pending entries
        normalized: List[str] = []
        seen: set[str] = set()
        for step in pending:
            if step not in seen:
                normalized.append(step)
                seen.add(step)
        return State(
            store=dict(self.initial_store),
            pending=tuple(normalized),
            done=tuple(self.initial_done),
            events=(),
        )

    def enabled_steps(self, state: State) -> List[str]:
        enabled: List[str] = []
        for step_name in state.pending:
            spec = self.plan.steps.get(step_name)
            if spec is None:
                continue
            if self._reads_available(spec, state.store):
                if step_name not in enabled:
                    enabled.append(step_name)
        return enabled

    def successors(self, state: State) -> List[Tuple[str, State]]:
        enabled = self.enabled_steps(state)
        succ: List[Tuple[str, State]] = []
        for step_name in enabled:
            spec = self.plan.steps.get(step_name)
            if spec is None:
                continue
            succ.append((step_name, self._run_step(state, step_name, spec)))
        return succ

    def is_deadlocked(self, state: State) -> bool:
        return len(state.pending) > 0 and not self.enabled_steps(state)

    def _collect_steps(self, changed_paths: Sequence[Path]) -> List[str]:
        steps: List[str] = []
        for rule in self.plan.rules:
            if _rule_triggered(rule.triggers, changed_paths):
                steps.extend(rule.steps)
        return steps

    def _reads_available(self, spec: StepSpec, store: Mapping[str, object]) -> bool:
        for read in spec.reads:
            if read not in store:
                return False
        return True

    def _run_step(self, state: State, step_name: str, spec: StepSpec) -> State:
        pending = list(state.pending)
        try:
            pending.remove(step_name)
        except ValueError:
            pass

        store: Dict[str, object] = dict(state.store)
        for w in spec.writes:
            store[w] = True
        # Non-monotone clear (ISSUE-FORMLANG P1): remove cleared facts so the
        # store can move *down* the powerset lattice. ``has(k)`` is membership,
        # so deletion is exactly "k becomes false". Store still ranges over the
        # finite 2^keys, so exploration stays decidable/terminating. Applied
        # after writes, so a key listed in both ends up cleared.
        for c in getattr(spec, "clears", ()) or ():
            store.pop(c, None)
        triggered = self._collect_steps([_selector_to_path(w) for w in spec.writes])
        for step in triggered:
            if step not in pending:
                pending.append(step)

        done = list(state.done)
        if step_name not in done:
            done.append(step_name)

        events = tuple(list(state.events) + [f"step:{step_name}"])
        return State(store=store, pending=tuple(pending), done=tuple(done), events=events)
