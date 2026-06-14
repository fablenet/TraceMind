"""Joint Kripke verification — N-component compositional model checking.

Given N adapters (each implementing the TraceMindAdapter protocol), build a
joint Kripke structure via **asynchronous interleaving**: at each transition,
exactly one component fires while the others remain unchanged. Then evaluate
CTL formulas over the joint model.

This is the foundation for Phase 6 AgentNetwork verification. The API is
deliberately **N-arity** (not hardcoded 2-arity): a star topology with 1
center + M leaves needs ``joint_verify`` over N = M + 1 components, and
later phases may need DAG topologies with even more.

## Naming convention for joint predicates

The joint state projects each component's facts under a stable id prefix
(default ``agent0``, ``agent1``, …). CTL formulas reference component-local
facts using the namespaced form, e.g.::

    AG !(has(agent0.locked) && has(agent1.locked))

So predicates with side ``has(<id>.<key>)`` / ``pending(<id>.<step>)`` /
``done(<id>.<step>)`` work directly with the existing CTL evaluator —
``tm.verify.ctl`` is not modified by this module.

## Limitations (v0)

- Asynchronous interleaving only. Synchronous / locked-step composition can
  be added later via a constraint predicate.
- Counterexample extraction is a BFS path from initial state to the first
  state where the formula fails; not a minimal witness.
- Caller must keep the same ``JointAdapter`` instance across explorer +
  CTL evaluation so the joint-state memo stays consistent.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any, Dict, List, Protocol, Sequence, Tuple

from .ctl import Ctl, check_ctl, eval_state_expr, has_ctl_nodes, parse_expr
from .explorer import Explorer, ExplorationResult
from .state import State


class ComponentAdapter(Protocol):
    """Duck-typed protocol matching :class:`TraceMindAdapter`'s surface.

    Any object exposing these four methods can be composed via JointAdapter.
    The existing ``TraceMindAdapter`` satisfies this trivially.
    """

    def initial_state(self) -> State: ...
    def successors(self, state: State) -> List[Tuple[str, State]]: ...
    def is_deadlocked(self, state: State) -> bool: ...
    def enabled_steps(self, state: State) -> List[str]: ...


@dataclass(frozen=True)
class JointState:
    """Immutable joint state — a tuple of component states.

    Not used by the explorer directly (it works on the projected ``State``);
    exposed so tests and downstream code can hold typed references.
    """

    components: Tuple[State, ...]
    component_ids: Tuple[str, ...]

    def __post_init__(self) -> None:
        if len(self.components) != len(self.component_ids):
            raise ValueError("components and component_ids must have the same length")


def _project(components: Tuple[State, ...], component_ids: Tuple[str, ...]) -> State:
    """Project N component states to a single namespaced ``State``.

    Each component's ``store`` / ``pending`` / ``done`` / ``events`` is keyed
    by ``{component_id}.{key}`` so the existing CTL predicates work directly.
    """
    store: Dict[str, object] = {}
    pending: List[str] = []
    done: List[str] = []
    events: List[str] = []
    for cid, comp in zip(component_ids, components):
        prefix = f"{cid}."
        for k, v in comp.store.items():
            store[prefix + str(k)] = v
        pending.extend(prefix + step for step in comp.pending)
        done.extend(prefix + step for step in comp.done)
        events.extend(prefix + evt for evt in comp.events)
    return State(
        store=store,
        pending=tuple(pending),
        done=tuple(done),
        events=tuple(events),
    )


@dataclass
class JointAdapter:
    """Asynchronous interleaving product of N component adapters.

    Implements the same surface as ``TraceMindAdapter``, so it works directly
    with :class:`tm.verify.explorer.Explorer` and the CTL evaluator. Maintains
    a memo from projected-state hash → underlying component-state tuple so
    ``successors()`` can reconstruct component states on each query.

    The adapter is **single-use** in the sense that you should pass the same
    instance to ``Explorer`` and any subsequent CTL evaluation. Re-creating
    it loses the memo and would require re-exploration.
    """

    components: Tuple[ComponentAdapter, ...]
    component_ids: Tuple[str, ...]
    _memo: Dict[str, Tuple[State, ...]] = field(default_factory=dict, init=False, repr=False)

    @classmethod
    def from_components(
        cls,
        components: Sequence[ComponentAdapter],
        *,
        component_ids: Sequence[str] | None = None,
    ) -> "JointAdapter":
        if not components:
            raise ValueError("JointAdapter requires at least one component")
        if component_ids is None:
            ids = tuple(f"agent{i}" for i in range(len(components)))
        else:
            ids = tuple(component_ids)
            if len(ids) != len(components):
                raise ValueError(f"component_ids length {len(ids)} != component count {len(components)}")
            if len(set(ids)) != len(ids):
                raise ValueError("component_ids must be unique")
            for cid in ids:
                if not cid:
                    raise ValueError(f"component_id must be non-empty, got '{cid}'")
        return cls(components=tuple(components), component_ids=ids)

    def initial_state(self) -> State:
        joint = tuple(c.initial_state() for c in self.components)
        projected = _project(joint, self.component_ids)
        self._memo[projected.stable_hash("full")] = joint
        return projected

    def successors(self, state: State) -> List[Tuple[str, State]]:
        joint = self._lookup_joint(state)
        out: List[Tuple[str, State]] = []
        for i, (comp_adapter, comp_id, comp_state) in enumerate(zip(self.components, self.component_ids, joint)):
            for label, next_comp_state in comp_adapter.successors(comp_state):
                new_joint = tuple(next_comp_state if j == i else st for j, st in enumerate(joint))
                projected = _project(new_joint, self.component_ids)
                self._memo[projected.stable_hash("full")] = new_joint
                out.append((f"{comp_id}:{label}", projected))
        return out

    def is_deadlocked(self, state: State) -> bool:
        return len(state.pending) > 0 and not self.enabled_steps(state)

    def enabled_steps(self, state: State) -> List[str]:
        try:
            joint = self._lookup_joint(state)
        except KeyError:
            return []
        out: List[str] = []
        for comp, cid, comp_state in zip(self.components, self.component_ids, joint):
            for step in comp.enabled_steps(comp_state):
                out.append(f"{cid}.{step}")
        return out

    def joint_state(self, state: State) -> JointState:
        """Recover the underlying ``JointState`` for a projected state."""
        joint = self._lookup_joint(state)
        return JointState(components=joint, component_ids=self.component_ids)

    def _lookup_joint(self, state: State) -> Tuple[State, ...]:
        h = state.stable_hash("full")
        if h not in self._memo:
            raise KeyError(
                f"JointAdapter cannot reconstruct joint state from hash {h[:12]}…; "
                "successors() must only be called on states returned from this adapter"
            )
        return self._memo[h]


@dataclass
class JointVerdict:
    """Per-formula verdict from a joint verification run."""

    formula: str
    satisfied: bool
    violation_path: List[int] = field(default_factory=list)
    counterexample: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class JointReport:
    """Aggregate outcome of ``joint_verify`` over N components and M formulas."""

    verified: bool
    component_count: int
    component_ids: List[str]
    formulas: List[str]
    verdicts: List[JointVerdict]
    state_count: int
    edge_count: int
    deadlock_count: int
    # 7-V.4: which verification mode produced this report. Additive; the
    # monolithic product (the default) always reports "monolithic".
    mode: str = "monolithic"

    def failed_formulas(self) -> List[str]:
        return [v.formula for v in self.verdicts if not v.satisfied]


def _bfs_path_outside(model_edges: Dict[int, List[int]], sat: set[int]) -> List[int]:
    """Return a BFS path from 0 to the first state not in ``sat`` (or [])."""
    if 0 not in sat and sat:
        return [0]
    visited = {0}
    queue: deque[tuple[int, List[int]]] = deque()
    queue.append((0, [0]))
    while queue:
        sid, path = queue.popleft()
        for nxt in model_edges.get(sid, []):
            if nxt in visited:
                continue
            visited.add(nxt)
            new_path = path + [nxt]
            if nxt not in sat:
                return new_path
            queue.append((nxt, new_path))
    return []


def _witness_violation_path(
    model: ExplorationResult,
    adapter: JointAdapter,
    expr: Any,
    sat: set[int],
) -> List[int]:
    """Pick a counterexample path that reaches a meaningful violating state."""
    if 0 in sat:
        return []
    if sat:
        path = _bfs_path_outside(model.edges, sat)
        if path:
            return path
    probe = expr
    if isinstance(probe, Ctl) and probe.op == "AG":
        probe = probe.child
    if not has_ctl_nodes(probe):
        for sid in range(len(model.states)):
            if not eval_state_expr(probe, model.states[sid], adapter):
                path = model.path_to(sid)
                if path:
                    return path
    path = _bfs_path_outside(model.edges, sat)
    return path if path else [0]


def project_counterexample(
    adapter: JointAdapter,
    model: ExplorationResult,
    violation_path: Sequence[int],
) -> List[Dict[str, Any]]:
    """Map a violation path to per-node snapshots and transition labels."""
    steps: List[Dict[str, Any]] = []
    for i, sid in enumerate(violation_path):
        if sid >= len(model.states):
            continue
        st = model.states[sid]
        joint = adapter.joint_state(st)
        nodes: Dict[str, Dict[str, Any]] = {}
        for cid, comp in zip(joint.component_ids, joint.components):
            nodes[cid] = {
                "store": dict(comp.store),
                "pending": list(comp.pending),
                "done": list(comp.done),
            }
        transition: str | None = None
        if i > 0:
            prev_sid = violation_path[i - 1]
            pred = model.predecessors.get(sid)
            if pred is not None and pred[0] == prev_sid:
                transition = pred[1]
        steps.append(
            {
                "state_index": sid,
                "transition": transition,
                "nodes": nodes,
            }
        )
    return steps


def joint_verify(
    components: Sequence[ComponentAdapter],
    formulas: Sequence[str],
    *,
    component_ids: Sequence[str] | None = None,
    max_depth: int = 16,
    hash_mode: str = "full",
    project_counterexamples: bool = True,
) -> JointReport:
    """Verify a list of CTL formulas on the joint product of N components.

    Args:
        components: Sequence of N component adapters (N >= 1). Each must
            satisfy the :class:`ComponentAdapter` protocol.
        formulas: CTL formula strings to evaluate. Predicates may reference
            component-local facts using ``has(<component_id>.<key>)`` /
            ``pending(<component_id>.<step>)`` / ``done(<component_id>.<step>)``.
        component_ids: Optional explicit ids for the N components. Defaults
            to ``agent0``, ``agent1``, …. Ids must be unique. Bundle artifact
            refs such as ``bundle.center`` are valid (Phase 6 AgentNetwork).
        max_depth: Maximum BFS depth for state exploration.
        hash_mode: ``"full"`` (default) or ``"store"`` — passed through to the
            underlying state hasher.

    Returns:
        :class:`JointReport` with per-formula verdicts and exploration stats.

    Raises:
        ValueError: if ``components`` is empty, or ``component_ids`` is
            malformed.
    """
    adapter = JointAdapter.from_components(components, component_ids=component_ids)
    explorer = Explorer(adapter)
    model = explorer.run(max_depth=max_depth, hash_mode=hash_mode)
    edge_count = sum(len(succs) for succs in model.edges.values())

    verdicts: List[JointVerdict] = []
    all_satisfied = True
    for formula_str in formulas:
        expr = parse_expr(formula_str)
        sat = check_ctl(expr, model, adapter)
        is_sat = 0 in sat
        if is_sat:
            verdicts.append(JointVerdict(formula=formula_str, satisfied=True))
        else:
            all_satisfied = False
            path = _witness_violation_path(model, adapter, expr, sat)
            counterexample = project_counterexample(adapter, model, path) if project_counterexamples else []
            verdicts.append(
                JointVerdict(
                    formula=formula_str,
                    satisfied=False,
                    violation_path=path,
                    counterexample=counterexample,
                )
            )

    return JointReport(
        verified=all_satisfied,
        component_count=len(components),
        component_ids=list(adapter.component_ids),
        formulas=list(formulas),
        verdicts=verdicts,
        state_count=len(model.states),
        edge_count=edge_count,
        deadlock_count=len(model.deadlocks),
    )


__all__ = [
    "ComponentAdapter",
    "JointAdapter",
    "JointState",
    "JointReport",
    "JointVerdict",
    "joint_verify",
    "project_counterexample",
]
